# wat branch scheme

```
master/Dom (upstream) ──> Dom-wat ──> wat-bolt      (== Dom-wat, no rebuild)
                              │
                              ├────> wat-ioniq      (Dom-wat + CAN/SBU wake + Ioniq tune + Ioniq torqued + Ioniq firmware)
                              └────> wat-analysis ──> wat-analysis-bolt   (+ wat-bolt)
                                                 └──> wat-analysis-ioniq  (+ wat-ioniq)
```

| Branch | Contents | Deployed to |
|---|---|---|
| `Dom` | fast-forward mirror of upstream `master/Dom` | nothing |
| `Dom-wat` | upstream Dom + common wat work: lateral controller unwind fix, `Paths.log_root` fix, blind-spot / PiP, Sentry, dev-container env + CI | nothing directly |
| `wat-bolt` | exactly `Dom-wat` (the Bolt 2022/2023 tune is upstream's own). Carries upstream's stock firmware. | Bolt (`github` `wat-bolt-tuning` alias) |
| `wat-ioniq` | `Dom-wat` + CAN/SBU wake + GPIOC11 bootkick, custom Ioniq 6 tune, Ioniq 6 torqued changes, regenerated panda firmware | Ioniq (`github` `wat-ioniq-tuning` alias) |
| `wat-analysis*` | analyzers, notes, plans, route scripts. Never deployed. | nothing |

## WARNING: `wat-ioniq` is not deployable until its firmware is rebuilt

Until the firmware rebuild commit (`panda: regenerate firmware for wat-ioniq`) exists, `wat-ioniq`
carries the wake **source** but upstream's stock firmware objects under `panda/board/obj/`. Do not push it to
`github` or flash it before that commit exists. The Ioniq keeps running the old `wat-ioniq-tuning` build (which has the
wake firmware) until then.

Rebuild procedure (in the `starpilot-dev` container): `git switch wat-ioniq && sp-build`, verify the panda targets
were built (`arm-none-eabi-gcc` present), commit only `panda/board/obj/**` as
`panda: regenerate firmware for wat-ioniq`, then confirm `git diff origin/wat-ioniq -- panda/board/obj` is non-empty and
`panda/board/{main.c,power_saving.h,boards/cuatro.h}` still match the old `wat-ioniq-tuning`.

## Flows

- **Upstream ingest:** `Dom` -> `Dom-wat` -> `wat-bolt` / `wat-ioniq`. Merge, never rebase.
- **Common feature:** short-lived branch off `Dom-wat`, merge it back, then merge `Dom-wat` into both car branches.
- **Car work:** short-lived branch off `wat-<car>`, merge it back.
- **One mechanism per feature.** Sentry and blind-spot are not long-lived branches. They landed in `Dom-wat` once;
  further work is a short-lived branch off `Dom-wat`. Do not keep cherry-picked copies of the same feature on several
  branches (that is the duplicate-commit mess this scheme replaced).
- **Analysis:** nothing flows from an analysis branch back into `Dom-wat` or a car branch. New common tools start on a
  branch off `wat-analysis`. Cherry-pick (never merge) from the car analysis branches.
- **Upstream PRs:** opportunistic; staging strategy is deferred. wat-only paths to exclude from any PR:
  `tools/wat_dev`, `Dockerfile.wat_*`, `.github/workflows/base-image.yml`, `.forgejo`.

## Firmware and generated files

`panda/board/obj/**` (built firmware) and `selfdrive/locationd/models/generated/**` are tracked upstream on purpose;
upstream `build` commits regenerate them wholesale. A local build embeds the git hash (`obj/version`), so it dirties
tracked files and binaries conflict on every merge. Policy:

- On merge conflicts in those paths, take upstream's side; never hand-merge binaries.
- Firmware is per car. Only `wat-ioniq` rebuilds panda firmware (only the Ioniq has the wake), and only in the dev container.
- `git-hooks/pre-commit` rejects staged files under those paths, except while finishing an upstream merge and on
  `wat-ioniq`. Override deliberately with `WAT_ALLOW_BUILD_ARTIFACTS=1`.

## Setup

Hooks and config are per clone. Run once in each clone: `./tools/wat_dev/bin/wat-setup` (or `git wat-setup` once the
alias exists). It sets `core.hooksPath=tools/wat_dev/git-hooks` and `rerere.enabled=true`. The dev container's image
registers the `git wat-setup` alias and its entrypoint runs it for every clone under `REPOS_DIR` (needs an image rebuild
to take effect).

## Known issues (as of Dom-wat creation, 2026-09-20)

- **Sentry tests pollute blind-spot tests when run in one pytest process.**
  `starpilot/system/the_galaxy/tests/test_sentry_*.py` replace `cloudlog` with a `types.SimpleNamespace` in
  `sys.modules`. Anything imported afterwards that calls `cloudlog.debug` at import time (e.g.
  `openpilot/system/ui/lib/multilang.py` via `gui_app`) fails with
  `AttributeError: 'types.SimpleNamespace' object has no attribute 'debug'`, so
  `selfdrive/ui/tests/test_blind_spot_indicators.py` errors if it runs after them. Each suite passes alone.
  Run them in separate pytest invocations until the Sentry tests restore `sys.modules` (or stub `debug`). Not fixed on purpose.
- **Two upstream test failures in `selfdrive/controls/tests/test_latcontrol.py`** (they fail identically on plain
  `master/Dom`, not caused by wat changes): `test_bolt_2022_2023_low_speed_center_output_limit` and
  `test_palisade_center_output_taper_curve`.
- **Running tests on x86 needs local builds.** The `.so`/`.a` files tracked upstream are aarch64, so pytest needs a local
  `./build` (which dirties tracked files; the pre-commit hook keeps them out of commits).
- **`wat-ioniq` only: `test_ioniq_6_friction_center_fade_curve` fails.** Commit `f3391a0b4` raised
  `IONIQ_6_FRICTION_CENTER_FADE_MAX` from 0.50 to 0.80 but the test still asserts
  `get_ioniq_6_friction_center_fade_scale(0.0, 30.0) >= 0.5`; the function now returns ~0.267. Stale assertion, not a
  controller bug; the old deployed `wat-ioniq-tuning` has the same mismatch. Fix when convenient by asserting
  `>= 1 - IONIQ_6_FRICTION_CENTER_FADE_MAX`. Left unfixed on purpose.
