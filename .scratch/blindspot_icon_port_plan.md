# Port sunnypilot's blind-spot icon indicator to starpilot (comma 4 / mici UI)

Working log for this task. Canonical plan (with full context/upstream source) is at
`C:\Users\pancake\.claude\plans\see-blind-spot-plan-drifting-deer.md` (outside the repo).

## Branching note (deviation from original plan)

Original plan assumed branching from `wat-shutdown`. That branch doesn't exist in this
repo at all — it belongs to a separate, unrelated outer "notes" repo one level up
(`C:\Users\pancake\Documents\python\projects\starpilot`, its own git history). The actual
openpilot/starpilot code lives in this nested repo (`StarPilot/`), which was on
`wat-ioniq-tuning` (with two unrelated pending deletions of `frpc_darwin_amd64`/`arm64`
already in the working tree — untouched, pre-existing, not part of this change).

User confirmed: branch `wat-blindspot` off `wat-ioniq-tuning` (current). Done.

## Status: implementation done, verification partially blocked by environment

- [x] Branch `wat-blindspot` created off `wat-ioniq-tuning`.
- [x] New file `selfdrive/ui/onroad/starpilot/blind_spot_indicators.py` — ported
      `BlindSpotIndicators` from upstream sunnypilot (fetched fresh from GitHub raw,
      matched the plan's snippet exactly). Adapted per plan: dropped `ui_state.blindspot`
      gating from `render()`/`detected` (starpilot has no such global flag); `detected`
      now based purely on the alpha filters.
- [x] New param `BlindSpotIcon` added to `common/params_keys.h` (next to `BlindSpotMetrics`/
      `BlindSpotPath`, same `SETTINGS_SIMPLE` bool pattern, default on).
- [x] Settings row added in `selfdrive/ui/layouts/settings/starpilot/appearance.py`,
      right after `BlindSpotMetrics` — same `enabled=bsm, visible=hud_on` gating (bsm =
      car has BSM hardware).
- [x] Wired into `selfdrive/ui/mici/onroad/hud_renderer.py::HudRenderer`:
      import, `self._blind_spot_indicators = BlindSpotIndicators()` in `__init__`,
      `.update()` in `_update_state()` (right after `rivian_lateral_mode.update()`),
      `.render(self._rect)` in `render_foreground()` guarded by
      `ui_state.ui_params.get_bool("BlindSpotIcon")`.
- [x] Unit test added: `selfdrive/ui/tests/test_blind_spot_indicators.py` — covers alpha
      filter following `leftBlindspot`/`rightBlindspot`, `detected` property, and that
      `render()` no-ops when both filters are at 0.
- [x] **Ran the unit test on WSL** (new worktree `starpilot-wat-blindspot`, `uv sync` +
      `scons -j$(nproc)` to build native extensions incl. `msgq`/`cereal`). Initial run
      failed: all 3 tests in `test_blind_spot_indicators.py` errored in the shared
      `_make_indicators()` helper — `monkeypatch.setattr(gui_app, "target_fps", 60)`
      raised `AttributeError` because `GuiApplication.target_fps` is a read-only property
      (getter only, backed by `_target_fps`; that property predates this branch, added
      2026-04-11 in `d43b7d0d3`). Fixed by monkeypatching the backing `_target_fps` field
      instead. `selfdrive/selfdrived/tests/test_blindspot_alerts.py` (10 tests) passed
      untouched. All 13 tests green after the fix. Fix committed (`a0af28ea8`,
      test-file-only change) and pushed to `custom_waffle`/`wat-blindspot`.
      `selfdrive/ui/tests/test_blind_spot_indicators.py:17` is the changed line.
- [x] Code review (low effort) — run. One real finding, fixed: `.update()` was called
      unconditionally every frame in `_update_state()` while `.render()` was properly
      gated on `BlindSpotIcon`, so a disabled-then-re-enabled icon would pop in at full
      alpha instead of fading. Now `.update()` is gated on the same
      `ui_state.ui_params.get_bool("BlindSpotIcon")` check. Review also flagged the
      pre-existing deleted `frpc_darwin_amd64`/`arm64` binaries (see note below) — not
      part of this change, left alone.
- [ ] On-device visual check — not done (needs comma 4 hardware, per plan).
- [x] Committed (`fab1f5060`, files listed below only — the pre-existing frpc deletions
      were deliberately left unstaged/uncommitted, see note below) and pushed to
      **`custom_waffle` only** (`http://git.waffle/waffle/StarPilot.git`, branch
      `wat-blindspot`) — explicitly NOT to `origin` (`git.waffle/comma`) or
      `custom_github` (GitHub), per user instruction. Branch tracks
      `custom_waffle/wat-blindspot`.

**What's actually left**: on-device visual check only (needs comma 4 hardware). Unit test
now verified on WSL and green.

## Follow-up: settings toggle was unreachable on comma 4 (found 2026-09-14)

User checked the device and couldn't find the `BlindSpotIcon` toggle anywhere. Root cause:
the settings row from the "implementation done" section above was only added to the
**legacy (non-mici) settings screen** (`selfdrive/ui/layouts/settings/starpilot/appearance.py`,
used by `selfdrive/ui/layouts/main.py`). Comma 4 runs the separate `selfdrive/ui/mici/`
tree (`selfdrive/ui/mici/layouts/main.py` → `selfdrive/ui/mici/layouts/settings/settings.py`),
which never got a `BlindSpotIcon` control — its pre-existing sibling `BlindSpotMetrics`
("Blind Spot Borders") wasn't there either, so this gap predates this branch.

- [x] Added `self._blind_spot_icon_btn = BigParamControl("blind spot icon", "BlindSpotIcon")`
      to `selfdrive/ui/mici/layouts/settings/visuals.py`, same pattern as the other
      `BigParamControl` HUD toggles in that file, gated via
      `set_enabled(starpilot_state.car_state.hasBSM)` in `_refresh()` (mirrors the legacy
      UI's `enabled=bsm`). Committed `f8de7a6fc`.
- [x] Also checked and added to galaxy (the web/phone settings app, `system/the_galaxy`):
      added a `BlindSpotIcon` entry to `starpilot/common/assets/device_settings_layout.json`
      (the single JSON catalog that drives galaxy's settings page — no JS/HTML changes
      needed, it's fully data-driven). First pass added a `requires_capability: "HasBSM"`
      gate plus a new `_get_has_bsm()` helper/endpoint wiring in `the_galaxy.py`, but that
      was inconsistent with the pre-existing sibling `BlindSpotPath` entry in the same
      catalog, which has no such gating (its BSM gating lives only in
      `starpilot/common/starpilot_variables.py:985`'s `has_bsm and ...` check, not the
      catalog). Backed out the capability/helper addition per user instruction so
      `BlindSpotIcon`'s galaxy entry now matches `BlindSpotPath` exactly. Committed
      `f8de7a6fc` (mici + galaxy icon toggle together).
- [x] User also asked to add the pre-existing `BlindSpotMetrics` ("Blind Spot Borders")
      toggle to galaxy (mici still doesn't have it — left alone, out of scope for this
      ask). Added a matching entry to the same JSON catalog. Committed `f63e56fd7`.
- [x] Verified after each JSON/mici change: JSON parses (`python3 -c "import json; ..."`),
      `the_galaxy.py` compiles (`py_compile`), and
      `starpilot/system/the_galaxy/tests/test_device_settings_layout.py` +
      `test_device_settings_frontend.py` (32 tests total) pass. Needed `uv sync --extra
      testing` plus an ad hoc `uv pip install python-dateutil` to get pytest running in
      this worktree's venv (pre-existing gap, not committed/tracked). Two other galaxy
      test files (`test_navigation_params.py`, `test_tesla_can_wake.py`) still fail to
      even import here — missing `PIL`/full Flask app deps not in the `testing` extras
      group; pre-existing environment gap, unrelated to these changes, not investigated
      further.
- [ ] Not pushed yet — `f8de7a6fc` and `f63e56fd7` are local to this worktree on
      `wat-blindspot`, need to push to `custom_waffle` (same target as `fab1f5060`) when
      ready.
- [ ] mici still lacks a `BlindSpotMetrics` ("Blind Spot Borders") settings row — same
      class of gap as the icon toggle had, not fixed (out of scope, not asked for yet).

## Aside: broader UI test suite is flaky on WSL (unrelated to this change)

While verifying, also tried running the wider `selfdrive/ui/tests/`+`system/ui/` suite in
the new WSL worktree, out of caution. Not clean, but unrelated to blind-spot code:
- `selfdrive/ui/mici/tests/test_widget_leaks.py` reliably hangs — it opens a real
  `GuiApplication`/raylib window (`init_window`) and never returns, even with
  `OFFSCREEN=1` (that env var only disables FPS limiting, it doesn't skip window
  creation). WSLg provides a real `DISPLAY`/`WAYLAND_DISPLAY` so a window does open; the
  hang is something else. Killing the process tree shows the window flash briefly.
- With that file excluded, `selfdrive/ui/tests/test_aethergrid.py::TestAethergridContracts::test_custom_icon_uses_completed_cache_without_redrawing_geometry`
  is flaky under the repo's default `pytest-xdist --dist=loadgroup` config: segfaulted in
  `pyray` once (xdist auto-restarted the worker and the suite finished), then hung with
  zero output on a rerun. Likely a shared-GL-context race between xdist workers,
  exacerbated by real-display + CPU contention in this environment.
Left untouched — pre-existing test-infra issue, not introduced by this branch. Worth a
separate investigation if the broader UI suite needs to run reliably on WSL.

## Unrelated pre-existing dirty state — do not commit as part of this change

`starpilot/system/galaxy/bin/frpc_darwin_amd64` and `frpc_darwin_arm64` show as deleted
in `git status` on this branch, inherited from `wat-ioniq-tuning`'s working tree (present
before this task started, not touched by any of the edits above). Code review flagged
that `starpilot/system/galaxy/galaxy.py:127,129` still references these paths, so if that
deletion is ever committed it needs a matching code fix — but that's a separate,
unrelated task. When staging/committing the blind-spot changes, stage only the files
listed below; do not `git add -A`.

## Follow-up: PIP draw-order fix + L/R side badge (2026-09-14)

User asked whether blind-spot icons would actually appear on the blind-spot PIP screens on
mici, since the PIP looked like it might be its own canvas.

- Investigated (subagent): everything on mici is immediate-mode raylib drawing into the
  same GL framebuffer — no separate canvas/texture between icons and PIP, so it's pure
  z-order by call order. Found `HudRenderer.render_foreground()` (which drew the icons)
  ran *before* `pip_sidecam.render(preview_rect)` in
  `selfdrive/ui/mici/onroad/augmented_road_view.py`, and the curved (mici) PIP fills
  almost the entire content rect, so whenever the PIP was active it painted over the
  icons drawn moments earlier.
- [x] Fix: split blind-spot icon drawing out of `render_foreground()` into its own
  `HudRenderer.render_blind_spot_icons()` method (leaves the other `render_foreground()`
  overlays — torque bar, set speed, steering wheel, etc. — untouched, in their original
  z-order), and call it in `augmented_road_view.py` right after
  `self._pip_sidecam.render(preview_rect)` instead of before. Committed `34c5b524f`.
- [x] Follow-up ask: the curved PIP doesn't indicate which vehicle side it's showing,
  which could confuse the driver if both BSM icons fire at once. Added an L/R text badge
  in `selfdrive/ui/onroad/starpilot/pip_sidecam.py`, drawn only for the `"curved"` shape
  (mici) right after `_draw_curved()`, in the matching top corner (`side` param is already
  vehicle-side terminology). Bubble shape (Raybig) left untouched — its per-side corner
  position already conveys which side it is. New constants `SIDE_LABEL_MARGIN=16`,
  `SIDE_LABEL_FONT_SIZE=32`; styled via existing `draw_text_with_shadow`/
  `measure_text_cached`/`gui_app.font(FontWeight.BOLD)` helpers. Committed `cff0aebe2`.
- [x] `_pick_side()` side-selection algorithm (pre-existing, not changed): sticky
  preference for whichever side most recently had a rising edge (inactive → active); once
  shown, a side keeps displaying while both stay active (no per-frame flapping). A true
  simultaneous first-activation tie breaks toward vehicle-right, since `active_sides()`
  appends "right" before "left". Icons are fully independent of PIP — both can show
  regardless of PIP settings/config; PIP+badge require `PIPPreviewEnabled` +
  `GalaxyDeveloperMode` + `PIPPreviewShowOnBSM` + a valid mask crop for that side, so icon
  and PIP+badge are not guaranteed to always appear together.
- [x] `/code-review low` run on the three commits above. One real finding: extracting
  `render_blind_spot_icons()` out of `render_foreground()` left the generic
  `HudRenderer._render()` helper (an unused Widget-API convenience method — nothing
  currently calls `.render()` on the mici `HudRenderer`) silently missing the icon draw.
  Fixed by adding the call there too, to keep it in sync with the real
  prepare/render_background/render_foreground/render_blind_spot_icons sequence used by
  `augmented_road_view.py`. Committed `e32fa22ef`. Two other findings from the review
  were confirmed non-issues (guard moved correctly with the extracted call; `self._font`
  is never `None` where `_draw_side_label` runs).
- [x] Pushed `wat-blindspot` to `origin` (git.waffle) and to a newly-added `github` remote
  (`https://github.com/watwat1234/StarPilot.git`; needed installing `gh` CLI from apt into
  `~/.local/bin` — no sudo available — then `gh auth login` + `gh auth setup-git`).
- [x] Merged forward through the existing branch chain, each hop conflict-free:
  `wat-blindspot` → `wat-ioniq-tuning` (in worktree `starpilot-wat-ioniq-merge`, after
  fast-forwarding that worktree's stale local branch ~50 commits to `origin/wat-ioniq-tuning`
  first — it included a large unrelated upstream `Dom` merge) → `wat-bolt-tuning` (new
  worktree `starpilot-wat-bolt-merge`, created for this; kept per user request). Verified
  the Bolt-specific CAN/SBU-wake revert commits on `wat-bolt-tuning` were untouched by the
  merge. All three pushed to both `origin` and `github`.

## Follow-up: Galaxy "Parameter 'BlindSpotIcon' is not editable" on real hardware (2026-09-14)

User flashed/tested on their Ioniq 6's comma device; toggling the icon on in Galaxy failed
with `Parameter 'BlindSpotIcon' is not editable.`

- Root cause (subagent investigation): `common/params_keys.h` has always had the correct
  `BlindSpotIcon` entry (since `fab1f5060`), but the **compiled** native params extension
  checked into git (`common/params_pyx.so`, backed by `common/libcommon.a`) predates that
  change and has zero occurrences of the string. Galaxy's editability check
  (`starpilot/system/the_galaxy/the_galaxy.py:6078`) derives its allowlist from the
  compiled extension's key map at runtime (`_get_param_type_info()` →
  `_params_raw.all_keys()`), not from the header — so the key silently isn't in the
  allowlist and any write 403s. Confirmed identical on all three branches
  (`wat-blindspot`, `wat-ioniq-tuning`, `wat-bolt-tuning`) — same stale binaries
  everywhere, not branch-specific.
- Found the repo ships a tracked, empty `prebuilt` marker file at repo root
  (`system/version.py:is_prebuilt()`); when present, `system/manager/build.py` skips
  running scons entirely on-device and trusts the checked-in binaries as-is. That's why
  this only surfaced now — every prior change on this branch was Python-only and needed
  no recompile.
- This fork's actual release process (`release/build_release.sh`) builds once on a
  dedicated build box/device (deps installed via `tools/install_ubuntu_dependencies.sh`),
  commits the compiled binaries into a release branch, `touch prebuilt`, and pushes.
  End-user devices just pull the prebuilt branch and never run scons — so building on a
  daily-driver device is off the normal path, not something the user had done before
  (matches their own recollection — panda firmware rebuilds are unrelated: separate
  `arm-none-eabi-gcc` bare-metal toolchain, not gated by `prebuilt` at all, never touches
  `eigen3`/`libcommon.a`).
- Considered `tools/laptop_device_build.sh` (documented in
  `docs/how-to/laptop-device-build.md`) — a proper Dockerized `larch64` cross-build flow.
  Blocked: Docker isn't reachable from this WSL shell (only Windows-side `docker.exe`, no
  WSL integration enabled). Deferred by user ("stop here for now").
- User opted to attempt an on-device rebuild instead, walked through step by step (no
  precanned setup script exists for this — `tools/agnos/validate_agnos_runtime.sh` is a
  golden-image drift *validator*, not a dependency installer, and doesn't check `eigen3`
  at all since it's compile-time-only):
  1. First scons error: `eigen3/Eigen/Dense` header not found (`libeigen3-dev` genuinely
     missing on-device, confirmed via `tools/install_ubuntu_dependencies.sh:49`).
  2. `apt-get install --dry-run` → `E: Unable to locate package libeigen3-dev` even
     though `/etc/apt/sources.list.d/ubuntu.sources` correctly has `universe` enabled
     against `ports.ubuntu.com` (device is genuinely Ubuntu 24.04 noble, aarch64 ports).
  3. `apt-get update` → `E: List directory /var/lib/apt/lists/partial is missing` — a
     stock apt runtime dir never existed on this AGNOS image (apt was seemingly never used
     on it before). Fixed: `sudo mkdir -p /var/lib/apt/lists/partial
     /var/cache/apt/archives/partial`, then `apt-get update` succeeded.
  4. `sudo apt-get install -y libeigen3-dev` succeeded for real (confirmed via
     `dpkg -s`/header file present).
  5. Rebooted to trigger the build (`prebuilt` was already absent on this device).
     Second scons error, different file: `selfdrive/controls/lib/lateral_mpc_lib` codegen
     failed importing `acados_template` with `SyntaxError: unknown encoding:
     future_fstrings` — the managed venv (`/usr/local/venv`) is missing the
     `future-fstrings` package despite it being declared in `pyproject.toml`/`uv.lock`.
     This matches the drift `tools/agnos/validate_agnos_runtime.sh` is designed to catch
     (it asserts an exact site-packages count of 253) — this device's managed venv has
     drifted from the expected golden image in more than one place. No on-device venv
     resync mechanism was found (appears baked in at AGNOS image build time, not
     dynamically synced by the manager).
  6. At this point user judged this was going too deep for a UI toggle fix ("this is too
     much") and decided to hard-reset the on-device repo instead of continuing to chase
     venv drift.
- [x] Pragmatic fix instead: default `BlindSpotIcon` to enabled in code so no Galaxy write
  is ever needed to see it. `common/params.py:234-241`'s `get_bool()` already catches
  `UnknownKeyName` (exactly what happens against the stale/unregistered-key binary) and
  falls back to whatever `default=` is passed — same pattern already used one line above
  for `EnableTorqueBarWidget`. Changed
  `selfdrive/ui/mici/onroad/hud_renderer.py::render_blind_spot_icons()` to
  `get_bool("BlindSpotIcon", default=True)`. Safe in both states: today (unregistered key)
  it evaluates true via the exception fallback; once binaries are eventually rebuilt for
  real, the key's own declared default in `params_keys.h` is also `"1"`, so behavior is
  unchanged either way. Committed `b3196d7a9` on `wat-blindspot`, merged forward through
  the same chain to `wat-ioniq-tuning` (`7efe91a96`) and `wat-bolt-tuning` (`2038c9af9`),
  all pushed to both `origin` and `github`.
- [ ] **Not resolved**: Galaxy's `BlindSpotIcon` toggle write path (`the_galaxy.py:6078`
  "not editable" 403) still needs a real aarch64 binary rebuild to actually fix — deferred
  by user. Proper path is the Dockerized `laptop_device_build.sh` flow (needs Docker
  Desktop WSL integration enabled first) or a dedicated build device following
  `release/build_release.sh`'s normal flow — not a daily-driver on-device rebuild, which
  turned up unrelated venv drift (`future-fstrings` missing, likely more given the
  site-packages count mismatch) beyond just `eigen3`.

## Files changed
- `selfdrive/ui/onroad/starpilot/blind_spot_indicators.py` (new)
- `common/params_keys.h`
- `selfdrive/ui/layouts/settings/starpilot/appearance.py`
- `selfdrive/ui/mici/onroad/hud_renderer.py` (render_foreground/render_blind_spot_icons
  split, PIP-order fix, `_render()` sync fix, `BlindSpotIcon` default=True)
- `selfdrive/ui/tests/test_blind_spot_indicators.py` (new)
- `selfdrive/ui/mici/layouts/settings/visuals.py` (follow-up, `f8de7a6fc`)
- `starpilot/common/assets/device_settings_layout.json` (follow-up, `f8de7a6fc` + `f63e56fd7`)
- `selfdrive/ui/mici/onroad/augmented_road_view.py` (PIP draw-order fix, `34c5b524f`)
- `selfdrive/ui/onroad/starpilot/pip_sidecam.py` (L/R side badge, `cff0aebe2`)
