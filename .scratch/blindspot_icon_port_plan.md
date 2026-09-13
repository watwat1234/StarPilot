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

## Files changed
- `selfdrive/ui/onroad/starpilot/blind_spot_indicators.py` (new)
- `common/params_keys.h`
- `selfdrive/ui/layouts/settings/starpilot/appearance.py`
- `selfdrive/ui/mici/onroad/hud_renderer.py`
- `selfdrive/ui/tests/test_blind_spot_indicators.py` (new)
