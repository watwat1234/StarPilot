# wat branch scheme

```
master/Dom (upstream) ──> Dom-wat ──> wat-bolt      (== Dom-wat, no rebuild)
                              │
                              ├────> wat-ioniq      (Dom-wat + CAN/SBU wake + Ioniq tune + Ioniq torqued; only branch that rebuilds firmware)
                              └────> wat-analysis ──> wat-analysis-bolt   (+ wat-bolt)
                                                 └──> wat-analysis-ioniq  (+ wat-ioniq)
```

| Branch | Contents | Deployed to |
|---|---|---|
| `Dom` | fast-forward mirror of upstream `master/Dom` | nothing |
| `Dom-wat` | upstream Dom + common wat work: lateral controller unwind fix, `Paths.log_root` fix, blind-spot / PiP, Sentry, dev-container env + CI | nothing directly |
| `wat-bolt` | exactly `Dom-wat` (the Bolt 2022/2023 tune is upstream's own). Carries upstream's stock firmware. | Bolt (devices track `github` `wat-bolt`) |
| `wat-ioniq` | `Dom-wat` + CAN/SBU wake + GPIOC11 bootkick, custom Ioniq 6 tune, Ioniq 6 torqued changes. It is the only branch that regenerates panda firmware, and that commit is added separately (see the rule below). | Ioniq (`github` `wat-ioniq`), only when the rule below is met |
| `wat-analysis*` | analyzers, notes, plans, route scripts. Never deployed. | nothing |
| `bolt-ff-experiments` | parked Bolt FF/ringdown tuning work (2026-09-16..18); not canonical, merged into nothing | nothing |
| `wat-reorg-notes` | orphan branch: runbook and working files from the 2026-09-20 reorganization, history only | nothing |
| `wat-reorg-open-items` | orphan branch: follow-ups from the reorganization (pending work, open decisions, known issues); living, never merged | nothing |

## Rule: `wat-ioniq` is deployed only with firmware built from its own panda source

`wat-ioniq` is the only branch that rebuilds panda firmware (only the Ioniq has the CAN/SBU wake). Never push it to
`github` or flash it unless its tip contains a `panda: regenerate firmware for wat-ioniq` commit that is newer than the last
change to `panda/board/{main.c,power_saving.h,boards/cuatro.h}`. A tip without that carries the wake *source* but stale
(upstream stock) firmware objects and is not deployable. The rebuild runs in the `starpilot-dev` container
(`git switch wat-ioniq && sp-build`, verify the panda targets built, commit only `panda/board/obj/**`), and afterwards
`git diff origin/wat-ioniq -- panda/board/obj` must be non-empty while the source files still match the previous Ioniq build.

## Flows

- **Upstream ingest:** `Dom` -> `Dom-wat` -> `wat-bolt` / `wat-ioniq`. Merge, never rebase. Before merging, review
  `git log --oneline <old-Dom-tip>..<new-Dom-tip>` and summarize what's incoming and its likely impact. Call out any
  tuning changes to the Bolt or Ioniq 6 specifically (both deployed cars) — e.g. `opendbc_repo/opendbc/car/gm/carcontroller.py`
  (`BOLT_*` constants), `opendbc_repo/opendbc/car/hyundai/carcontroller.py` (`IONIQ_6_*` constants),
  `opendbc_repo/opendbc/car/{gm,hyundai}/values.py` (platform specs), and `opendbc_repo/opendbc/car/torque_data/*.toml`
  (`CHEVROLET_BOLT_*` / `HYUNDAI_IONIQ_6` rows). A prior upstream Ioniq 6 tune landed badly; don't take one blind.
- **Common feature:** short-lived branch off `Dom-wat`, merge it back, then merge `Dom-wat` into both car branches.
- **Car work:** short-lived branch off `wat-<car>`, merge it back, named `wat-<car>-<class>-<name>` (see naming below).
- **One mechanism per feature.** Sentry and blind-spot are not long-lived branches. They landed in `Dom-wat` once;
  further work is a short-lived branch off `Dom-wat`. Do not keep cherry-picked copies of the same feature on several
  branches (that is the duplicate-commit mess this scheme replaced).
- **Analysis:** nothing flows from an analysis branch back into `Dom-wat` or a car branch. New common tools start on a
  branch off `wat-analysis`. Cherry-pick (never merge) from the car analysis branches.
- **Upstream PRs:** opportunistic; staging strategy is deferred. wat-only paths to exclude from any PR:
  `tools/wat_dev`, `Dockerfile.wat_*`, `.github/workflows/base-image.yml`, `.forgejo`.

## Car-work branch naming: `wat-<car>-<class>-<name>`

Short-lived car-work branches (the "Car work" flow above) carry a `<class>` tag so intent is visible before
anyone opens the diff:

- **`fix`** — restores or corrects known-bad behavior; usually small, usually pinned by a test. Merge as soon as
  it's verified; don't let it linger.
- **`tune`** — deliberate parameter/behavior tradeoff, backed by data (e.g. an offline A/B replay with a before/after
  metric). Expect a commit message with the numbers, not just the change.
- **`experiment`** — open-ended, may be abandoned, not merged until it proves out. Parking a dead-end experiment
  (like `bolt-ff-experiments`) is fine; don't invent a `fix` or `tune` story to justify it.

Example: `wat-ioniq-fix-restore-torque-clear` (class `fix`) reverted an undocumented Ioniq 6 torque-zeroing removal
that traded a cosmetic turn-blip for reintroducing the steer-fault condition the zeroing existed to prevent.

`feature/*` stays reserved for common work landing on `Dom-wat` (per the "Common feature" flow above) — it is not
part of this car-work class tag.

## Worktrees

Sibling worktrees next to the main checkout. The main and car-branch worktrees stay on their own branch:

| Worktree | Branch | Rule |
|---|---|---|
| `StarPilot` (main checkout) | `Dom` | Keep on `Dom`. Don't do feature work here; use a `StarPilot-<feature>` worktree. |
| `StarPilot-wat-bolt` | `wat-bolt` | Keep on `wat-bolt`. Don't switch it to a `test/*` branch. |
| `StarPilot-wat-ioniq` | `wat-ioniq` | Keep on `wat-ioniq`. Don't switch it to a `test/*` branch. |
| `StarPilot-wat-bolt-test` | `test/wat-lead-departing-alert` | Bolt test merges happen here. |
| `StarPilot-wat-ioniq-test` | `test/wat-ioniq-lead-departing-alert` | Ioniq test merges happen here. |
| `StarPilot-<feature>` | `feature/<feature>` (or `fix/*`) | One worktree per in-flight feature branch, e.g. `StarPilot-wheel-tint-fade`. |

A branch can only be checked out in one worktree, so parking another branch in one of these worktrees blocks
that branch elsewhere and leaves the worktree off the branch it is named for. Give each `test/*` branch its own
`<car>-test` worktree (`git worktree add ../StarPilot-wat-<car>-test test/<branch>`) and each feature branch its own
`StarPilot-<feature>` worktree.

## Firmware and generated files

`panda/board/obj/**` (built firmware) and `selfdrive/locationd/models/generated/**` are tracked upstream on purpose;
upstream `build` commits regenerate them wholesale. A local build embeds the git hash (`obj/version`), so it dirties
tracked files and binaries conflict on every merge. Policy:

- On merge conflicts in those paths, take upstream's side; never hand-merge binaries.
- Firmware is per car. Only `wat-ioniq` rebuilds panda firmware (only the Ioniq has the wake), and only in the dev container.
- `git-hooks/pre-commit` rejects staged files under those paths, except while finishing an upstream merge and on
  `wat-ioniq`. Override deliberately with `WAT_ALLOW_BUILD_ARTIFACTS=1`. It also always rejects a staged
  `panda/board/obj/version` containing `unknown` (only `--no-verify` skips that).
- **Build through `sp-build` (or `sp-panda-build` for firmware only), as the checkout's owner.** In a worktree the build
  container cannot see git, so a direct `./build` or `scripts/laptop_device_build.sh` stamps firmware
  `DEV-unknown-DEBUG` (compiled in and signed, so it cannot be repaired afterwards). `sp-build` patches temporary copies
  of those scripts to mount the main repo's git dir, aborts if upstream's versions no longer match the patch, and fails
  on an `unknown` stamp. Run it as the user that owns the checkout (`docker exec -u batman`), not root, or git rejects the
  repo as dubious-ownership. A rebuilt dev image puts both commands on `PATH`; until then use `tools/wat_dev/bin/`.

## Setup

Hooks and config are per clone. Run once in each clone: `./tools/wat_dev/bin/wat-setup` (or `git wat-setup` once the
alias exists). It sets `core.hooksPath=tools/wat_dev/git-hooks` and `rerere.enabled=true`. The dev container's image
registers the `git wat-setup` alias and its entrypoint runs it for every clone under `REPOS_DIR` (needs an image rebuild
to take effect).

## Open items and known issues

Not tracked here, so this file stays policy. See `OPEN_ITEMS.md` on branch `wat-reorg-open-items`.
