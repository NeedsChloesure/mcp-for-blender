# AGENTS.md

Guidance for anyone — human or agent — changing this repository. Read it before touching
`src/blender_mcp/`, `addon.py`, or the upstream-sync workflow.

**TL;DR**
- This is a personal fork of `ahujasid/blender-mcp` (`mcp-for-blender`) installed from this
  checkout instead of PyPI.
- It exists because Freebuff cannot display inline MCP image content, so images are written
  to disk and the model is given a file path instead.
- Fork policy: no telemetry, no Premium, no Tripo, and no AI 3D generation. Do not
  reintroduce them.
- `addon.py` and `src/blender_mcp/bundled/addon.py` must stay byte-identical.
- The addon's update check reads this fork's `main` and only links to the file; it never
  installs anything.

---

## What this repository is

A fork of [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp), the project
published as **`mcp-for-blender`**. The package name, the `mcp-for-blender` command and the
Blender addon name are unchanged, so this remains a drop-in replacement for the official
build; only where `uv` reads the code from changes (this checkout, not PyPI). The tool
surface differs by the telemetry, Premium/Tripo and 3D-generator removals listed below.

- Remotes: `origin` = `git@github.com:NeedsChloesure/mcp-for-blender.git` (this fork),
  `upstream` = `https://github.com/ahujasid/blender-mcp.git`.
- Version follows upstream: `pyproject.toml` is `2.2.0` and the addon protocol is `14` at the
  time of writing. Keep the version equal to the upstream release merged into `main`; the
  sync workflow compares the two to decide when to open a PR.
- Not published to PyPI. Installation is covered in the README section "Installing this fork".

---

## Why this fork exists

### 1. Freebuff cannot display MCP image content (the original reason)

The Freebuff desktop harness does not render an `image` content block returned by an MCP
tool. The base64 payload arrives as JSON text the model never gets to look at, and results
above the host's size cap are dropped outright. That breaks visual verification, most
importantly `look`.

The workaround is to **not deliver the image over MCP at all**: write the bytes to a file and
return that file's absolute path, with an instruction to open it using the model's own
file-reading tool (`read_files` in Freebuff), which does work there.

`src/blender_mcp/image_files.py` (186 lines) implements it:

| Piece | Behaviour |
|---|---|
| `image_output_mode()` | `file` (default) or `inline`, from `BLENDER_MCP_IMAGE_OUTPUT` |
| `image_dir()` | where files are written; `BLENDER_MCP_IMAGE_DIR`, default `<tmp>/mcp-for-blender-images` |
| `save_image()` | unique `<tool>-<timestamp>-<pid>` name, 700 KiB cap, ImageMagick (`magick`/`convert`) downscale attempts 1600→700 px, keeps the newest 40 files |
| `deliver_image()` | saves and returns the note: absolute path, human-readable size, "open it with your file-reading tool" |

The 700 KiB cap is deliberate: Freebuff's `read_files` refuses images above roughly 768 KiB.
If ImageMagick is absent and the file is still too large, the note includes the exact
`magick` command to downscale it.

It is wired in at exactly one place today: `server._viewport_screenshot()`, the native path
for `look(mode="viewport")` without `shading`. That path returns a `TextContent` block by
default; `BLENDER_MCP_IMAGE_OUTPUT=inline` restores the old inline `ImageContent`. The
viewport state in `_meta` is untouched, so the Viewport app's state path is unchanged.

Both behaviours are pinned in `tests/test_openai_apps.py`, and `tests/test_compat_matrix.py`
counts a delivered file as "showing something".

**Known gap:** the other image results still return inline content and therefore hit the same
bug. `look` modes served by `_look_via_script()` (`camera`, `angles`, `frames`, `image=`, and
any non-default `shading=`) return `ImageContent`, and `search_assets(previews=...)` returns
inline thumbnails via `_preview_images()`. If you touch those paths, use
`deliver_image()`/`image_output_mode()`.

### 2. Fork policy: no telemetry, no Premium, no Tripo

Upstream ships a Supabase telemetry/trajectory stack and a paid "Premium" generation path
(license key, activation, quotas, fal routing; Tripo is Premium-only). This fork deletes all
of it rather than feature-flagging it. The upstream-sync workflow does **not** strip these
automatically — the PR body tells the reviewer to do it while resolving conflicts.

New work must not reintroduce their modules, decorators, tools, preferences, imports or
environment variables.

### 3. Fork policy: no AI 3D generation

Upstream generates 3D models from text or images through three providers — Hyper3D Rodin
(with fal.ai as an alternate backend), Hunyuan3D (Tencent Cloud, including its TC3 request
signing, or a local server) and Meshy. Each is an optional, own-key integration, and users
can also pay upstream for hosted generation.

This fork had already dropped the paid path. It now drops the generators entirely: there is
no `generate_3d` tool, no `generation.py`, and no `#region Hyper3D` / `#region Hunyuan3D` /
`#region Meshy` in the addon. The reason is that they were by far the largest and most
failure-prone integration surface for no gain here: three vendor APIs, job submission and
polling, a pending-job store on disk that survives restarts, provider auto-selection rules,
signed requests, and roughly half the addon. Everything they touched is reachable through
`execute_blender_code`, and models can be imported from the asset libraries below.

The three asset libraries — Poly Haven, Sketchfab, Poly Pizza — are **kept**. They are
catalogues of existing CC0/CC-BY models and assets, not a service that bills per generation.

When a provider is removed, the MCP surface shrinks with it: the tool disappears, not just
its dispatch branch. `tests/test_consolidated_tools.py` pins the exact model-facing tool set.

---

## Change inventory

### Server — `src/blender_mcp/`

| File | Change |
|---|---|
| `image_files.py` | **new** (+186): file-based image delivery described above |
| `server.py` | viewport screenshot returns a path; telemetry imports, lifespan hooks, per-tool decorators, the observe step and screenshot upload are gone; `disable_telemetry` is a documented no-op; the `record_trajectory_feedback` tool was removed; `_premium_generators`, premium hints/ordering, `TRIPO_UNAVAILABLE` and the Tripo branch are gone; `_integrations()` no longer takes a premium list or reports generators; `generate_3d` and every generation helper are gone (−159/+11) |
| `generation.py` | **deleted** (415 lines): provider table and `choose_provider`, the job runner and polling, the pending-job store, `process_bbox`, Meshy's preview→refine bookkeeping |
| `addon_manager.py` | `AddonHandshake.premium_generators` and its handshake read removed |
| `openai_apps.py` | `import_generated_asset_tripo` removed from the scene-changing command set |
| `consent_prompt.py`, `premium_hint.py`, `telemetry.py`, `telemetry_decorator.py`, `trajectory.py` | **deleted** (228 / 64 / 385 / 489 / 1455 lines) |
| `uv.lock` | the fork's own resolution of the same dependency set; regenerate only if dependencies change |

### Blender addon

| File | Change |
|---|---|
| `addon.py` + `src/blender_mcp/bundled/addon.py` | identical copies. Upstream's 679-line `#region Premium` is gone: license/activation/account UI, generation-source preference, premium request/download/import helpers, upsell text. Tripo status, handlers, sidebar and property registrations are gone. The three generator regions are gone too (see below). `ADDON_PROTOCOL_VERSION` is `14` |
| telemetry | the addon still has a local `telemetry_consent` preference and uses it to gate manual-edit capture, but the server never calls `get/set_telemetry_consent` and nothing leaves the machine |

The root file is the source; the bundled copy is what `install-addon` and the updater ship.
Keep them identical:

```bash
cp addon.py src/blender_mcp/bundled/addon.py
```

### Docs, CI and packaging

| File | Change |
|---|---|
| `README.md` | top rewritten for the fork: personal fork, install from this checkout (`uv tool install --editable`, `uvx --from`, git remote, `uv run --directory`), and a substitution rule for `uvx mcp-for-blender`; badges/supporters removed; telemetry section rewritten as "removed"; Premium section, Tripo and generator rows and sections, and their credentials rows removed too |
| `.github/workflows/upstream-sync.yml` | **new**, fork-only: Mondays 06:17 UTC plus manual dispatch; compares `pyproject.toml` versions; creates `upstream-sync/<version>`; merges `upstream/main`, committing conflicts as-is; pushes and opens a PR. Optional `UPSTREAM_SYNC_TOKEN` secret |
| `.gitignore` | no longer ignores `src/blender_mcp/config.py` (the fork's config module was removed) |
| `pyproject.toml` | unchanged from upstream: same package name, `mcp-for-blender` entry point and version |
| `tests/` | consent-prompt and Premium tests removed; screenshots asserted as file paths; `test_premium_guidance.py`, `test_generation.py`, `test_meshy.py`, `test_hunyuan_api_profile.py`, `test_hunyuan_import_security.py`, `test_addon_compat.py` and `test_process_bbox_validation.py` deleted (1,376 lines); `test_addon_updater.py` rewritten for the check-only updater; tool-set, instruction and compat-matrix expectations trimmed |

### 3D generators and the addon updater (removed 2026-10-25)

Net effect: 4,888 lines deleted against `64560a9`, the suite at 418 passed / 1 skipped.

**Server**

- `generation.py` deleted (415 lines), along with `tests/test_generation.py` (579) and
  `tests/test_process_bbox_validation.py` (14).
- `server.py`: `generate_3d` removed from the tool list; the `_get_hyper3d_api_key`,
  `_get_hunyuan3d_secret_id` / `_secret_key` / `_api_url`, `_get_meshy_api_key` and
  `_resolve_meshy_model` helpers removed (the Sketchfab and Poly Pizza key helpers stay);
  `_integrations()` no longer reports a generation section; the free-trial Hyper3D key
  constant is gone.
- Support files follow the tool: the "Getting assets" section of `guides/scene.md` loses its
  Generate step, `openai_apps.SCENE_CHANGING_COMMANDS` drops `import_generated_asset` and
  `import_generated_asset_hunyuan`, and the safe-mode and `setup_cli` wording stops naming
  Hyper3D or "model generation".

**Addon** (`addon.py`, 5,938 → 4,539 lines; the bundled copy is identical)

| Removed | Lines |
|---|---|
| `#region Hyper3D` | 355 |
| `#region Hunyuan3D` (incl. TC3 signing) | 441 |
| `#region Meshy` | 196 |
| Scene properties (mode/model/intl-pro/quality knobs) | 116 |
| Key helpers, Meshy constants, the `HUNYUAN_API_PROFILES` table and signing helper | 100 |
| Command dispatch entries and per-provider handler blocks | 31 |
| Sidebar **AI model generation** section | 50 |
| Preferences (keys, unlock codes) and credential-box rows | 34 |
| Free-trial Hyper3D operator | 20 |
| `register`/`unregister` lines for the above | 18 |

`ADDON_PROTOCOL_VERSION` is now `14`, and `EXPECTED_ADDON_PROTOCOL_VERSION` in
`addon_manager.py` with it: an addon that still advertises the removed commands is now
reported as ahead of the server rather than matching it.

**Addon updater** (`#region Addon updates`, 150 → 115 lines)

It no longer installs anything. Upstream's addon would have restored Premium/Tripo over this
one, so the fork removed `install_addon_update()`, the `blendermcp.update_addon` operator and
its `register`/`unregister` lines. What remains:

- `ADDON_UPDATE_URL` points at
  `raw.githubusercontent.com/NeedsChloesure/mcp-for-blender/main/addon.py`;
- the check still compares `(major, minor, patch, protocol)` and only offers a strictly newer
  release, so a local checkout ahead of `main` is never "updated" backwards;
- the sidebar shows "Update available: X" with **Get update** (the file on GitHub) and
  **What's new** (its commit history) buttons — the user installs it themselves;
- `BLENDERMCP_NO_UPDATE_CHECK` still skips the startup check.

`uvx mcp-for-blender update` / `install-addon` are unaffected and still copy this fork's
**bundled** addon into Blender, which is the supported way to update it.

**Tests and docs**

The five generator test files are gone along with `test_addon_compat.py` (51 lines, a
Hunyuan-quality argument round trip). `test_compat_matrix.py` no longer lists generator
commands in `RELEASED_SERVER_COMMANDS`, which deliberately breaks compatibility with a
2.2.0 server asking an addon to generate; `test_consolidated_tools.py` now pins seven
model-facing tools, and the fake scenes in `test_polypizza.py` / `test_sketchfab_status.py`
no longer set the deleted properties. The README lost the Premium section, the generator
rows, sections, examples, credentials and environment variables.

### Commit map (archaeology shortcut)

| Commit | Date | What |
|---|---|---|
| `610e77b` | 2026-09-20 | Patches the server to work around Freebuff; adds `image_files.py` |
| `74b3525` | 2026-09-24 | Removes the telemetry stack |
| `741497a` | 2026-10-02 | Reconciles the replay: restores `_addon_protocol()` for the Codex plugin, drops consent tests, asserts path-based screenshots |
| `daa574c` | 2026-10-02 | Adds the upstream-sync workflow |
| `69cc1fb` | 2026-10-08 | Resolves the upstream 2.1.9 merge (drops Premium/Tripo, keeps the server refactor) |
| `64560a9` | 2026-10-25 | Resolves the upstream 2.2.0 merge (keeps Meshy, no Premium/Tripo) |

---

## Third-party integrations

The core — scene access, `execute_blender_code`, `look`, addon management — has no vendor
dependency. The remaining integrations are the three asset libraries; each is optional and
removable independently. Runtime HTTP calls happen in the Blender addon; the server drives it
over the socket, except Poly Haven thumbnails, which the server fetches from
`cdn.polyhaven.com`.

| Integration | What it provides | Auth | Server | Addon | Tests |
|---|---|---|---|---|---|
| **Poly Haven** | CC0 HDRIs, textures and models (`search_assets`/`import_asset` source `polyhaven`) | none | ~240 lines | 601-line region + commands + sidebar | `test_polyhaven.py` (2,283 lines) + cross |
| **Sketchfab** | downloadable models (`source="sketchfab"`) | API key | ~180 lines | 433-line region + commands + sidebar | `test_sketchfab_status.py` + cross |
| **Poly Pizza** | low-poly CC0/CC-BY models (`source="polypizza"`) | API key | ~235 lines | 435 lines (114 helpers + 321 API) + commands | `test_polypizza.py` (525) + cross |

Credentials and their environment variables are listed in the README under "Persistent API
Credentials". There are no generator integrations left: Hyper3D/fal.ai, Hunyuan3D and Meshy
and their `generation.py` were removed on 2026-10-25 — see above.

Not vendor services, but similarly removable:

| Extension | What it is | Where |
|---|---|---|
| **OpenAI Apps / Codex UI** | inline viewport app, thumbnail asset pickers, `@`-mentions, app-only tools | `openai_apps.py` (337), `apps/viewport.html` (449), `integrations/codex/`, `search_mentions` and `viewport_*` tools, `test_openai_apps.py` |
| **Addon update check** | reads this fork's `main` addon.py from raw.githubusercontent.com, compares `(version, protocol)`, shows "Update available" and links to the file. Installs nothing; the sidebar link is the only action | addon `#region Addon updates` (115 lines), `BLENDERMCP_NO_UPDATE_CHECK` |

Notes:

- Removing every asset library makes `search_assets`, `import_asset`, `ASSET_SOURCES` and
  `_preview_images` dead. Remove those tools too rather than leaving empty dispatch tables,
  and bump the protocol because the command surface shrinks.
- `set_texture` is only registered while Poly Haven is enabled and is currently reached through
  Poly Haven texture import; it is otherwise a generic addon command.
- There are no vendor Python SDKs. The server uses `mcp` and `httpx`; the addon uses Blender's
  bundled `requests`. ImageMagick is optional and only used to shrink oversized images.

To remove an integration end to end:

1. **Server:** drop the dispatch branch in `search_assets`/`import_asset`; update
   `ASSET_SOURCES`, `_integrations` and `_preview_images`; delete the tool if that was the last
   source.
2. **Addon:** delete the `#region`, its commands, scene properties, preferences and sidebar
   entries, plus the `register`/`unregister` lines; copy the file over the bundled addon; bump
   the protocol if the command surface changed.
3. **Tests:** delete the integration's tests, update `RELEASED_SERVER_COMMANDS` in
   `test_compat_matrix.py`, and any schema or consolidated-tool expectations.
4. **README:** remove the section, the feature and table rows, and the credentials and
   environment-variable rows.
5. Run the suite and confirm no dangling imports or references. The 2026-10-25 generator
   removal in this file is the worked example.

---

## Working here

### Environment and tests

This checkout has a local `.venv` (ignored by git). pytest is not a project dependency:

```bash
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q     # 418 passed, 1 skipped after the generator removal
```

The addon cannot be imported without `bpy`; tests load it through stubs, which is why
`tests/conftest.py` exposes `ROOT_ADDON`.

### Rules of thumb

- **New image-returning tool:** return a path via `deliver_image()` (honouring
  `image_output_mode()`), not `ImageContent`. Inline images are invisible to the model in
  Freebuff.
- **No Premium/Tripo/telemetry/generators:**
  `grep -rin "premium\|tripo\|hyper3d\|hunyuan\|meshy\|generate_3d" addon.py src/blender_mcp/`
  should be empty before you commit. `tests/test_consolidated_tools.py` and the compat matrix
  also guard the tool surface.
- **Addon protocol:** bump `ADDON_PROTOCOL_VERSION` in `addon.py` (and the bundled copy)
  together with `EXPECTED_ADDON_PROTOCOL_VERSION` in `addon_manager.py` when the command
  surface changes. Both are `14` today.
- **Version:** keep `pyproject.toml` equal to the upstream release merged into `main`. Bumping
  it on its own makes the sync workflow think upstream has drifted.
- **`upstream` is read-only.** Do not push to it; `origin` is this fork.

---

## Syncing upstream

The workflow commits the merge **with conflict markers** on purpose, so resolution happens in
the PR instead of failing the job.

1. `git fetch origin && git fetch upstream`
2. Check out the PR branch (`upstream-sync/<version>`) and find markers:
   `git grep -n -E '^(<<<<<<<|=======$|>>>>>>>)'`
3. Resolve by taking upstream's feature work while dropping Premium/Tripo/telemetry **and
   every generator**. Upstream keeps adding provider work, so expect `generation.py`, the
   `#region Hyper3D` / `Hunyuan3D` / `Meshy` blocks, `generate_3d`, its tests and its README
   sections to come back in each sync: delete them again rather than resolving them. Keep the
   three asset libraries intact.
4. Delete `generation.py` and the generator tests if the merge brought them back; trim
   `RELEASED_SERVER_COMMANDS`, `MODEL_TOOLS` and `SERVER_INSTRUCTIONS` to match.
5. Copy `addon.py` over the bundled copy, then bump both protocol numbers — this fork's
   protocol is ahead of upstream's, so the merged value from upstream is always wrong here.
6. Run the suite and confirm no markers remain and the premium/tripo/generator grep is empty.
7. Commit on top of the sync branch (`fix: resolve the upstream X.Y.Z merge for this fork`),
   merge into `main`, push, close the PR.

---

## Known gaps

- File-based delivery covers only the native viewport screenshot; scripted `look` modes and
  `search_assets` previews still return inline images.
- The addon's telemetry-consent UI is dead weight from the server's perspective.
- The update check only knows about `main` and cannot install: a user who is told an update is
  available still has to download and install it, or run `uvx mcp-for-blender update`.
- Upstream syncs reintroduce the generators and Premium/Tripo; stripping them is manual and
  the workflow will not do it for you.
- `build/` is a stale local artifact directory (ignored by git; not source).
- Local branches `pr-3`, `pr-4`, `upstream-no-premium` and `upstream-sync/2.1.9` are
  historical; `main` is the only maintained branch.
