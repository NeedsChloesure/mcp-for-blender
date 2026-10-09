"""generate_3d drives every provider through one submit/poll/import flow.

A fake addon replays each provider's real reply shapes, so these pin the
shapes the flow depends on: fal request ids, Rodin main-site subscription
keys, and Tencent-style Hunyuan responses.
"""

import asyncio

import pytest

from blender_mcp import generation
from blender_mcp.generation import GenerationError, Job, choose_provider


class FakeAddon:
    def __init__(self, replies):
        # command -> reply, or a list of replies consumed in order
        self.replies = replies
        self.calls = []

    def __call__(self, command, params):
        self.calls.append((command, params))
        reply = self.replies[command]
        if isinstance(reply, list):
            return reply.pop(0) if len(reply) > 1 else reply[0]
        return reply


def _run(send, job, wait=100):
    async def no_sleep(_):
        pass

    ticks = iter(range(0, 10_000, 5))
    return asyncio.run(generation.wait_and_import(send, job, wait, sleep=no_sleep, clock=lambda: next(ticks)))


# -------------------------------------------------------------- provider choice

def test_auto_picks_the_first_enabled_generator():
    assert choose_provider("auto", {"hyper3d": True, "hunyuan3d": True}) == "hunyuan3d"
    assert choose_provider("auto", {"hunyuan3d": False, "hyper3d": True}) == "hyper3d"


def test_nothing_enabled_points_at_setup():
    with pytest.raises(GenerationError, match="No 3D generator"):
        choose_provider("auto", {"hunyuan3d": False, "hyper3d": False})


def test_unknown_provider_is_rejected():
    with pytest.raises(GenerationError, match="Unknown provider"):
        choose_provider("tripo", {"hunyuan3d": True})


def test_explicit_provider_must_be_enabled():
    with pytest.raises(GenerationError, match="not enabled"):
        choose_provider("hyper3d", {})


# ------------------------------------------------------------------ flows

def test_error_codes_are_relayed_not_retried():
    send = FakeAddon({"create_hunyuan_job": {"code": "QUOTA_EXHAUSTED", "message": "You've used all generations."}})
    with pytest.raises(GenerationError, match="used all generations.*don't retry"):
        generation.submit(send, "hunyuan3d", "X", "x", None, None, None, True)


def test_hunyuan_polls_tencent_shapes_and_imports_the_glb():
    send = FakeAddon({
        "create_hunyuan_job": {"Response": {"JobId": "42"}},
        "poll_hunyuan_job_status": [
            {"Response": {"Status": "RUN"}},
            {"Response": {"Status": "DONE", "ResultFile3Ds": [
                {"Type": "OBJ", "Url": "https://x/model.zip"}, {"Type": "GLB", "Url": "https://x/model.glb"}]}},
        ],
        "import_generated_asset_hunyuan": {"succeed": True},
    })
    job = generation.submit(send, "hunyuan3d", "Lamp", "a lamp", None, "high", None, supports_quality=False)
    assert job.handle == "hunyuan3d:job:job_42"
    assert "quality" not in send.calls[0][1]  # older addons reject unknown arguments
    assert _run(send, job) == (True, "Lamp")
    assert send.calls[-1] == ("import_generated_asset_hunyuan", {"name": "Lamp", "zip_file_url": "https://x/model.glb"})


def test_hunyuan_failure_says_why():
    send = FakeAddon({"poll_hunyuan_job_status": {"Response": {"Status": "FAIL", "ErrorMessage": "bad prompt"}}})
    with pytest.raises(GenerationError, match="bad prompt"):
        _run(send, Job("hunyuan3d", "job", "job_1", "X"))


def test_rodin_main_site_uses_subscription_key_then_task_uuid():
    send = FakeAddon({
        "create_rodin_job": {"submit_time": True, "uuid": "u1", "jobs": {"subscription_key": "s1"}},
        "poll_rodin_job_status": [{"status_list": ["Done", "Generating"]}, {"status_list": ["Done", "Done"]}],
        "import_generated_asset": {"succeed": True},
    })
    job = generation.submit(send, "hyper3d", "Car", "a car", None, None, [2.0, 1.0, 1.0], True)
    assert job.handle == "hyper3d:main:u1|s1"
    assert send.calls[0][1]["bbox_condition"] == [100, 50, 50]
    assert _run(send, job) == (True, "Car")
    assert ("poll_rodin_job_status", {"subscription_key": "s1"}) in send.calls
    assert send.calls[-1] == ("import_generated_asset", {"task_uuid": "u1", "name": "Car"})


def test_rodin_fal_uses_request_id():
    send = FakeAddon({
        "create_rodin_job": {"request_id": "q1"},
        "poll_rodin_job_status": {"status": "COMPLETED"},
        "import_generated_asset": {"succeed": True},
    })
    job = generation.submit(send, "hyper3d", "Car", None, "https://img/car.png", None, None, True)
    assert send.calls[0][1]["images"] == ["https://img/car.png"]
    assert send.calls[0][1]["text_prompt"] is None
    assert _run(send, job) == (True, "Car")
    assert send.calls[-1] == ("import_generated_asset", {"request_id": "q1", "name": "Car"})


def test_running_out_of_time_returns_a_resumable_handle():
    send = FakeAddon({"poll_rodin_job_status": {"status": "IN_PROGRESS"}})
    job = Job("hyper3d", "fal", "r9", "Robot")
    imported, detail = _run(send, job, wait=20)
    assert not imported and detail == "IN_PROGRESS"
    assert Job.parse(job.handle, "Robot") == job


def test_failed_import_is_an_error():
    send = FakeAddon({
        "poll_rodin_job_status": {"status": "COMPLETED"},
        "import_generated_asset": {"succeed": False, "error": "download failed"},
    })
    with pytest.raises(GenerationError, match="download failed"):
        _run(send, Job("hyper3d", "fal", "r1", "X"))


def test_bad_handles_are_rejected():
    for handle in ("", "tripo", "meshy:rid:1", "hyper3d:fal:"):
        with pytest.raises(GenerationError):
            Job.parse(handle, "X")


def test_default_name_comes_from_the_prompt():
    assert generation.default_name("a weathered wooden treasure chest") == "AWeatheredWooden"
    assert generation.default_name(None) == "Generated"


# -------------------------------------------------------------- stranded jobs

def test_errors_after_submit_keep_the_handle():
    class FlakyAddon(FakeAddon):
        def __call__(self, command, params):
            raise ConnectionError("Socket timeout while waiting for response from Blender")

    job = Job("hyper3d", "fal", "r7", "Chest")
    with pytest.raises(GenerationError) as e:
        _run(FlakyAddon({}), job)
    assert job.handle in str(e.value) and "Socket timeout" in str(e.value)


def test_pending_jobs_survive_on_disk_until_settled(tmp_path):
    path = tmp_path / "pending-generations.json"
    job = Job("hyper3d", "fal", "r1", "Robot")
    key = generation.request_key("hyper3d", "a robot", None, "high", None)
    generation.Pending(path).add(key, job)

    # A fresh instance, as after a restart, still finds it; a different request doesn't.
    assert generation.Pending(path).find(key) == job
    assert generation.Pending(path).find(generation.request_key("hyper3d", "a robot", None, None, None)) is None

    send = FakeAddon({"poll_rodin_job_status": {"status": "COMPLETED"},
                      "import_generated_asset": {"succeed": True}})
    asyncio.run(generation.wait_and_import(send, job, 100, sleep=lambda _: asyncio.sleep(0),
                                           pending=generation.Pending(path)))
    assert generation.Pending(path).find(key) is None


def test_failed_jobs_leave_pending_but_interrupted_ones_stay(tmp_path):
    path = tmp_path / "pending.json"
    failed, cut = Job("hyper3d", "fal", "f1", "A"), Job("hyper3d", "fal", "c1", "B")
    generation.Pending(path).add("kf", failed)
    generation.Pending(path).add("kc", cut)

    send = FakeAddon({"poll_rodin_job_status": {"status": "FAILED", "error": "bad input"}})
    with pytest.raises(GenerationError):
        asyncio.run(generation.wait_and_import(send, failed, 100, pending=generation.Pending(path)))
    send = FakeAddon({"poll_rodin_job_status": {"status": "IN_PROGRESS"}})
    asyncio.run(generation.wait_and_import(send, cut, 1, pending=generation.Pending(path)))

    assert generation.Pending(path).find("kf") is None
    assert generation.Pending(path).find("kc") == cut


def test_old_or_unreadable_pending_entries_are_ignored(tmp_path):
    path = tmp_path / "pending.json"
    now = [1_000_000.0]
    pending = generation.Pending(path, clock=lambda: now[0])
    pending.add("k", Job("hyper3d", "fal", "r1", "A"))
    now[0] += generation.PENDING_MAX_AGE_S + 1
    assert pending.find("k") is None

    path.write_text("not json")
    assert generation.Pending(path).find("k") is None
    generation.Pending(path).add("k2", Job("hyper3d", "fal", "r2", "B"))
    assert generation.Pending(path).find("k2") is not None
    assert generation.Pending(None).find("k2") is None


def test_repeating_an_unfinished_request_resumes_instead_of_paying_again(tmp_path, monkeypatch):
    from blender_mcp import server

    send = FakeAddon({"create_rodin_job": {"request_id": "r1"},
                      "poll_rodin_job_status": {"status": "IN_PROGRESS"}})
    path = tmp_path / "pending.json"
    monkeypatch.setattr(server, "get_blender_connection", lambda: None)
    monkeypatch.setattr(server, "_own_key_generators", lambda _: {"hunyuan3d": False, "hyper3d": True})
    monkeypatch.setattr(server, "_addon_protocol", lambda: 13)
    monkeypatch.setattr(server, "_generation_send", send)
    monkeypatch.setattr(server, "_pending_generations", lambda: generation.Pending(path))
    monkeypatch.setattr(generation, "POLL_INTERVAL_S", 0.0)

    class Ctx:
        async def report_progress(self, *_):
            pass

    async def call(**kw):
        return await server.generate_3d(Ctx(), image="/tmp/ref.png", quality="high", wait_seconds=10, **kw)

    monkeypatch.setattr(generation, "submit", lambda send_, *a, **k: Job("hyper3d", "fal", send_("create_rodin_job", {})["request_id"], "Ref"))
    first = asyncio.run(call())
    second = asyncio.run(call())
    assert 'job="hyper3d:fal:r1"' in first and 'job="hyper3d:fal:r1"' in second
    assert "resumed" in second
    assert [c for c, _ in send.calls].count("create_rodin_job") == 1
