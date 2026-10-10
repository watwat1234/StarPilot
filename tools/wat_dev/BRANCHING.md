# wat branch scheme

```
master/Dom (upstream) ──> Dom-wat ──> wat-bolt      (== Dom-wat + rebuilt firmware)
                              │
                              ├────> wat-ioniq      (Dom-wat + Ioniq tune + Ioniq torqued + rebuilt firmware)
                              └────> wat-analysis ──> wat-analysis-bolt   (+ wat-bolt)
                                                 └──> wat-analysis-ioniq  (+ wat-ioniq)
```

| Branch | Contents | Deployed to |
|---|---|---|
| `Dom` | fast-forward mirror of upstream `master/Dom` | nothing |
| `Dom-wat` | upstream Dom + common wat work: lateral controller unwind fix, `Paths.log_root` fix, blind-spot / PiP, Sentry, comma four CAN/SBU wake + GPIOC11 bootkick (source only; carries upstream's stock firmware objects), dev-container env + CI | nothing directly |
| `wat-bolt` | `Dom-wat` (the Bolt 2022/2023 tune is upstream's own) + a `panda: regenerate firmware for wat-bolt` commit (see the rule below). | Bolt (devices track `github` `wat-bolt`), only when the rule below is met |
| `wat-ioniq` | `Dom-wat` + custom Ioniq 6 tune, Ioniq 6 torqued changes + a `panda: regenerate firmware for wat-ioniq` commit (see the rule below). | Ioniq (`github` `wat-ioniq`), only when the rule below is met |
| `wat-analysis*` | analyzers, notes, plans, route scripts. Never deployed. | nothing |

## Rule: car branches are deployed only with firmware built from their own panda source

`Dom-wat` carries the CAN/SBU wake *source* but upstream's stock firmware objects, so both car branches rebuild panda
firmware. Never push `wat-<car>` to `github` or flash it unless its tip contains a
`panda: regenerate firmware for wat-<car>` commit that is newer than the last change to
`panda/board/{main.c,power_saving.h,boards/cuatro.h}`. A tip without that carries the wake source but stale (upstream
stock) firmware objects and is not deployable. The rebuild runs in the `starpilot-dev` container (`sp-panda-build` in
the car's worktree; verify the stamp matches HEAD and only `panda/board/obj/**` is dirty, then commit only that), and
afterwards `git diff github/wat-<car> -- panda/board/obj` must be non-empty.

## Flows

- **Upstream ingest:** `Dom` -> `Dom-wat` -> `wat-bolt` / `wat-ioniq`. Merge, never rebase. Before merging, review
  `git log --oneline <old-Dom-tip>..<new-Dom-tip>` and summarize what's incoming and its likely impact. Call out any
  tuning changes to the Bolt or Ioniq 6 specifically (both deployed cars) — e.g. `opendbc_repo/opendbc/car/gm/carcontroller.py`
  (`BOLT_*` constants), `opendbc_repo/opendbc/car/hyundai/carcontroller.py` (`IONIQ_6_*` constants),
  `opendbc_repo/opendbc/car/{gm,hyundai}/values.py` (platform specs), and `opendbc_repo/opendbc/car/torque_data/*.toml`
  (`CHEVROLET_BOLT_*` / `HYUNDAI_IONIQ_6` rows). A prior upstream Ioniq 6 tune landed badly; don't take one blind.
- **Common feature:** short-lived branch off `Dom-wat`, merge it back, then merge `Dom-wat` into both car branches.
  Exemption: doc-only changes (this file, READMEs, etc.) don't need their own branch; commit them directly on `Dom-wat`.
- **Car work:** short-lived branch off `wat-<car>`, merge it back, named `wat-<car>-<class>-<name>` (see naming below).
- **Merge messages describe the change.** A merge into `Dom-wat`, `wat-bolt`, `wat-ioniq`, or any `test/*` branch needs
  a brief description of what is being merged in, not just `Merge branch 'x' into y`. One or two lines in the body is
  enough (e.g. what the feature/fix does, or for an upstream ingest, the notable incoming changes). Use `git merge -e`
  (or `--no-ff -m`) so the default auto-message isn't accepted as-is.
- **One mechanism per feature.** Sentry and blind-spot are not long-lived branches. They landed in `Dom-wat` once;
  further work is a short-lived branch off `Dom-wat`. Do not keep cherry-picked copies of the same feature on several
  branches (that is the duplicate-commit mess this scheme replaced).
- **Analysis:** nothing flows from an analysis branch back into `Dom-wat` or a car branch. New common tools start on a
  branch off `wat-analysis`. Cherry-pick (never merge) from the car analysis branches.
- **Upstream PRs:** two branches off `Dom` per PR. `pr/<name>-dev` collects the work by cherry-picking commits (not
  merges) from `Dom-wat` or the feature branch. `pr/<name>` is the staging branch: one squashed commit for upstream.
  Rebuild staging from scratch whenever the dev branch changes (reset to `Dom`, `git merge --squash pr/<name>-dev`);
  when `Dom` moves, rebase the dev branch first. wat-only paths to exclude from any PR:
  `tools/wat_dev`, `Dockerfile.wat_*`, `.github/workflows/base-image.yml`, `.forgejo`.
- **Backup tags are temporary undo points.** Tag `backup/<date>/<name>` only before an action that would leave
  commits with no other reference: a history rewrite, a force-push, or deleting a branch whose work isn't on any
  other branch. Delete the tag once the result is verified (and pushed, if the action was a push). Don't tag a
  branch whose work is already merged elsewhere; for local-only undo, the reflog keeps old tips for 90 days.

## Car-work branch naming: `wat-<car>-<class>-<name>`

Short-lived car-work branches (the "Car work" flow above) carry a `<class>` tag so intent is visible before
anyone opens the diff:

- **`fix`** — restores or corrects known-bad behavior; usually small, usually pinned by a test. Merge as soon as
  it's verified; don't let it linger.
- **`tune`** — deliberate parameter/behavior tradeoff, backed by data (e.g. an offline A/B replay with a before/after
  metric). Expect a commit message with the numbers, not just the change.
- **`experiment`** — open-ended, may be abandoned, not merged until it proves out. Parking a dead-end experiment
  is fine; don't invent a `fix` or `tune` story to justify it.

Example: `wat-ioniq-fix-restore-torque-clear` (class `fix`) reverted an undocumented Ioniq 6 torque-zeroing removal
that traded a cosmetic turn-blip for reintroducing the steer-fault condition the zeroing existed to prevent.

`feature/*` stays reserved for common work landing on `Dom-wat` (per the "Common feature" flow above) — it is not
part of this car-work class tag.

## Worktrees

Sibling worktrees under `workspace/`. **Rule: the directory name is the branch name with `/` replaced by `-`**
(no `StarPilot-` prefix). A worktree stays on the branch it is named for.

| Worktree | Branch |
|---|---|
| `StarPilot` (main checkout) | `Dom` (keep on `Dom`; no feature work here) |
| `StarPilot-Dom-wat` | `Dom-wat` (upstream ingest and common-feature merges happen here) |
| `wat-bolt`, `wat-ioniq` | same-named car branches (never switch to a `test/*` branch) |
| `wat-dev-notes` | `wat-dev-notes` (orphan notes branch, `<class>/<name>/progress.md`; never merged into code) |
| `test-<name>` | a temporary `test/<name>` branch, only while a combined on-device test needs one; delete it (no backup tag) once its work is on the feature/fix branches or the car branches |
| `feature-<name>`, `fix-<name>`, ... | `feature/<name>`, `fix/<name>`: one worktree per in-flight branch |

Exemptions from the naming rule:

- `StarPilot`: the main checkout; owns the shared `.git` (which `sp-build` mounts).
- `StarPilot-Dom-wat`: **the `starpilot-dev` container is launched from this worktree**
  (`tools/wat_dev/docker-compose.yml`, build context `../..`). It is not renamed so the container never has to be
  restarted for a rename. When debugging environment issues, the live Dockerfile/compose/entrypoint is whatever
  `Dom-wat` has checked out here. The container bind-mounts all of `DEV_ROOT`, so it sees every other worktree.

A branch can only be checked out in one worktree, so parking another branch in one of these worktrees blocks
that branch elsewhere and leaves the worktree off the branch it is named for. Give each branch its own worktree:
`git worktree add ../test-<name> test/<name>` (dir = branch with `/` -> `-`).

## Firmware and generated files

`panda/board/obj/**` (built firmware) and `selfdrive/locationd/models/generated/**` are tracked upstream on purpose;
upstream `build` commits regenerate them wholesale. A local build embeds the git hash (`obj/version`), so it dirties
tracked files and binaries conflict on every merge. Policy:

- On merge conflicts in those paths, take upstream's side; never hand-merge binaries.
- Firmware is per car. Both car branches rebuild panda firmware (both carry the wake), only in the dev container.
- `git-hooks/pre-commit` rejects staged files under those paths, except while finishing an upstream merge and on
  `wat-ioniq` (not yet `wat-bolt`). Override deliberately with `WAT_ALLOW_BUILD_ARTIFACTS=1`. It also always rejects a staged
  `panda/board/obj/version` containing `unknown` (only `--no-verify` skips that).
- **Build through `sp-build` (or `sp-panda-build` for firmware only), as the checkout's owner.** In a worktree the build
  container cannot see git, so a direct `./build` or `scripts/laptop_device_build.sh` stamps firmware
  `DEV-unknown-DEBUG` (compiled in and signed, so it cannot be repaired afterwards). `sp-build` patches temporary copies
  of those scripts to mount the main repo's git dir, aborts if upstream's versions no longer match the patch, and fails
  on an `unknown` stamp. Run it as the user that owns the checkout (`docker exec -u batman`), not root, or git rejects the
  repo as dubious-ownership. The dev image puts both commands on `PATH`; outside it use `tools/wat_dev/bin/`.

## Setup

Hooks and config are per clone. Run once in each clone: `./tools/wat_dev/bin/wat-setup` (or `git wat-setup`).
It sets `core.hooksPath=tools/wat_dev/git-hooks` and `rerere.enabled=true`. The dev container's image registers the
`git wat-setup` alias and its entrypoint runs it for every clone under `REPOS_DIR`.

## Open items and known issues

Not tracked here, so this file stays policy. See `_general/open-items/progress.md` on `wat-dev-notes`.
