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
- [ ] **Could not run the unit test in this session.** This Windows environment can't run
      the UI test suite: `pyray`/raylib isn't installed, and `uv sync` fails building
      `xattr` (a Linux-only native dependency of openpilot). Verified only via
      `python -m py_compile` (all changed/new files compile) and manual tracing against
      `test_traffic_border.py`'s established mocking pattern. **Run
      `pytest selfdrive/ui/tests/test_blind_spot_indicators.py -v` on the WSL machine or
      on-device before merging.**
- [x] Code review (low effort) — run. One real finding, fixed: `.update()` was called
      unconditionally every frame in `_update_state()` while `.render()` was properly
      gated on `BlindSpotIcon`, so a disabled-then-re-enabled icon would pop in at full
      alpha instead of fading. Now `.update()` is gated on the same
      `ui_state.ui_params.get_bool("BlindSpotIcon")` check. Review also flagged the
      pre-existing deleted `frpc_darwin_amd64`/`arm64` binaries (see note below) — not
      part of this change, left alone.
- [ ] On-device visual check — not done (needs comma 4 hardware, per plan).

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
