"""The addon's update check, which reads addon.py from *this fork's* main branch.

What matters: it compares against this fork rather than upstream, never offers a
downgrade, and cannot install anything - the fork removed the installer because
upstream's addon.py would put Premium/Tripo/telemetry back.
"""
import types

import pytest

from conftest import ROOT_ADDON
from test_polypizza import FakeResponse, _load_addon, _scene

SOURCE = ROOT_ADDON.read_text(encoding="utf-8")


def _with_version(version, protocol):
    """The real addon.py with its version and protocol swapped."""
    import re

    text = re.sub(r'"version": \(\d+, \d+\)', f'"version": {version}', SOURCE, count=1)
    return re.sub(r"^ADDON_PROTOCOL_VERSION = \d+", f"ADDON_PROTOCOL_VERSION = {protocol}", text, count=1, flags=re.M)


@pytest.fixture
def addon(monkeypatch):
    module = _load_addon(monkeypatch, _scene())
    module._addon_update.update(status="idle", version=None, error=None)
    return module


def _serve(monkeypatch, addon, response):
    monkeypatch.setattr(addon.requests, "get", lambda url, timeout=None: response, raising=False)


def test_release_key_reads_version_and_protocol(addon):
    key = addon.addon_release_key(SOURCE)
    assert key == tuple(addon.bl_info["version"]) + (0, addon.ADDON_PROTOCOL_VERSION)
    assert addon.addon_release_key(_with_version((2, 0, 3), 40)) == (2, 0, 3, 40)
    assert addon.addon_version_label((2, 0, 3, 40)) == "2.0.3"
    assert addon.addon_version_label((1, 8, 0, 12)) == "1.8"


@pytest.mark.parametrize("text", [
    "print('hello')",
    SOURCE.replace('"name": "MCP for Blender"', '"name": "Something Else"', 1),
    SOURCE.replace("ADDON_PROTOCOL_VERSION = ", "PROTOCOL = ", 1),
], ids=["not-the-addon", "renamed-addon", "no-protocol"])
def test_release_key_rejects_files_that_are_not_the_addon(addon, text):
    assert addon.addon_release_key(text) is None


def test_the_check_reads_this_fork_not_upstream(addon):
    assert "raw.githubusercontent.com/NeedsChloesure/mcp-for-blender/" in addon.ADDON_UPDATE_URL
    assert "ahujasid" not in addon.ADDON_UPDATE_URL + addon.ADDON_CHANGES_URL
    # No installer: upstream's addon.py must never be written over this one.
    assert not hasattr(addon, "install_addon_update")
    assert "blendermcp.update_addon" not in SOURCE


def test_newer_main_is_offered(monkeypatch, addon):
    major, minor = addon.bl_info["version"][:2]
    newer = _with_version((major, minor + 1), addon.ADDON_PROTOCOL_VERSION)
    _serve(monkeypatch, addon, types.SimpleNamespace(status_code=200, text=newer))
    addon.check_for_addon_update()
    assert addon._addon_update["status"] == "available"
    assert addon._addon_update["version"][:2] == (major, minor + 1)


@pytest.mark.parametrize("delta", [0, -1])
def test_same_or_older_main_is_never_offered(monkeypatch, addon, delta):
    major, minor = addon.bl_info["version"][:2]
    _serve(monkeypatch, addon, types.SimpleNamespace(
        status_code=200, text=_with_version((major, minor + delta), addon.ADDON_PROTOCOL_VERSION)))
    addon.check_for_addon_update()
    assert addon._addon_update["status"] == "current"


def test_a_protocol_bump_alone_counts_as_newer(monkeypatch, addon):
    _serve(monkeypatch, addon, types.SimpleNamespace(
        status_code=200, text=_with_version(tuple(addon.bl_info["version"]), addon.ADDON_PROTOCOL_VERSION + 1)))
    addon.check_for_addon_update()
    assert addon._addon_update["status"] == "available"


@pytest.mark.parametrize("response", [
    FakeResponse(status_code=404),
    types.SimpleNamespace(status_code=200, text="<html>rate limited</html>"),
    types.SimpleNamespace(status_code=200, text=_with_version((99, 0), 99) + "\ndef broken(:\n"),
])
def test_bad_downloads_are_errors_not_updates(monkeypatch, addon, response):
    _serve(monkeypatch, addon, response)
    addon.check_for_addon_update()
    assert addon._addon_update["status"] == "error"


def test_offline_is_an_error_not_a_crash(monkeypatch, addon):
    def offline(url, timeout=None):
        raise ConnectionError("no network")
    monkeypatch.setattr(addon.requests, "get", offline, raising=False)
    addon.check_for_addon_update()
    assert addon._addon_update["status"] == "error"
    assert "no network" in addon._addon_update["error"]
