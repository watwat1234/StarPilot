# Implement the Ioniq 6 friction-curve follow-up (static, git-committed)

## Relationship to the umbrella plan

This is a sub-plan under `.scratch/ioniq-detailed-tuning.md` (the main Ioniq 6 lateral-tuning
investigation), not a replacement for it. **Results go into this sub-plan file itself** (a
"Results" section below), not the umbrella doc. `.scratch/ioniq-detailed-tuning.md` stays
untouched by this thread except for two small additions, done as their own step, not bundled
into the shipping diff:
1. A one-line pointer to this file from the "Handoff status" section (same pattern as
   `.scratch/ioniq_baseline_vs_cleanup_check_plan.md`'s own pointer line).
2. Once shipped, the existing "Friction-curve follow-up — investigated, deferred (not shipped)"
   subsection's heading/status line gets updated to reflect it's now shipped (mirroring how the
   original chatter fix's own status line was updated) — do this as part of the final commit
   description, not as a separate silent edit.

## Context

The investigation (`.scratch/ioniq-detailed-tuning.md`, "Friction-curve follow-up" section) already
found and measured this candidate; nothing new needs deriving. Recap, so a fresh session doesn't
need to re-read the whole umbrella doc to start:

- The shipped chatter fix (`IONIQ_6_FRICTION_CENTER_FADE_MAX: 0.50 → 0.80`, commit `ca218d4fd`) cut
  full-route chatter (highway, `|setpoint|≤0.05`, 100-frame windows) from a baseline of **2.251
  mean sign-changes/window to 1.112**.
- FLM's own `center_chatter` suggestions for this route (report `flm-1789201386`) are 4 independent
  per-speed-band curves over `FLM_FRICTION_SPEED_KNOTS = [0, 5, 10, 15, 25]` m/s, elementwise-max
  merged into a single candidate curve: **`[0.4044, 0.414, 0.414, 0.414, 0.42]`**.
- A/B'd via `.scratch/ab_tests/ab_test_friction_curve.py` (already built, already correct — see
  "Technical confirmation" below): applying this curve **on top of** the shipped fix took chatter
  to **1.015 mean/window (~9% further reduction, ~55% total vs. the original 2.251 baseline)**,
  with **zero measured hug-metric effect** in every variant tested (real, additive, non-redundant
  with the shipped fix).
- This was deferred at the time only because it's a genuinely new tuning *surface* for this knob
  (a curve replacing a flat scalar), not a one-line constant change — flagged as needing
  `/code-review medium`, not `low`, and needing an explicit go-ahead before implementing. That
  go-ahead is what this plan executes.

**Why a static, git-committed constant and not FLM's own live-apply mechanism** (this was
explicitly re-litigated and settled in conversation just before this plan was written — don't
re-open it without new information):
- FLM's live path (`apply_trial_profile()` → `FLMActiveOverrides["baseFrictionThresholds"]["hkg_canfd"]`,
  consumed by `get_hkg_canfd_base_friction_threshold()` in `latcontrol_vehicle_tunes.py`) is real,
  single-shot, reviewable machinery — not an autonomous online tuner — and would technically carry
  this exact curve.
- But its own "make this permanent" function, `accept_trial_as_baseline()` (`flm_workspace.py:3549`),
  clears `FLMTrialApplied`/`FLMActiveProfileId`, which flips `flm_profile_active` false in
  `latcontrol_torque.py`'s `update()` and causes the *next control cycle* to call
  `set_flm_runtime_overrides(None)` — silently reverting the friction curve back to the hardcoded
  default despite the function's name implying persistence. There is no clean way to use FLM's own
  delivery mechanism to make this override durable without leaving it flagged as an active,
  revertible trial forever (which also blocks other FLM workspace actions).
- Given that gap, a static constant in `latcontrol_vehicle_tunes.py` — git-reviewable, permanent,
  no dependency on trial-state bookkeeping behaving as its name implies — is the right call here.
  This has nothing to do with fleet blast radius (this is a private fork); it's about FLM's live
  path not actually delivering "permanent" for this override type.

**Scoping decision carried over from the shipped chatter fix**: implement as an **Ioniq-6-scoped**
change (new curve constant read only from `get_ioniq_6_friction_threshold`), not a change to the
shared `HKG_CANFD_BASE_FRICTION_THRESHOLD` every Hyundai CANFD car in the fork reads via
`get_hkg_canfd_base_friction_threshold()`. No reason to widen blast radius even on a private fork
when the fix is Ioniq-6-specific data.

## Scope

**In scope:**
1. Add a new module-level constant near the other Ioniq-6 constants (`latcontrol_vehicle_tunes.py`,
   next to `IONIQ_6_BASE_FRICTION_THRESHOLD` at line ~810):
   ```python
   IONIQ_6_FRICTION_THRESHOLD_CURVE = [0.4044, 0.414, 0.414, 0.414, 0.42]
   ```
   Keep `IONIQ_6_BASE_FRICTION_THRESHOLD` itself untouched (still used as the flat floor's fallback
   comparison point isn't quite right — see step 2, it's actually being *replaced* in the `max()`
   call, not kept alongside; don't leave a dead unused constant — check callers first, see
   "Technical confirmation").
2. In `get_ioniq_6_friction_threshold()` (line ~3659), replace:
   ```python
   base_threshold = max(get_hkg_canfd_base_friction_threshold(v_ego), IONIQ_6_BASE_FRICTION_THRESHOLD)
   ```
   with:
   ```python
   base_threshold = max(
     get_hkg_canfd_base_friction_threshold(v_ego),
     float(np.interp(v_ego, FLM_FRICTION_SPEED_KNOTS, IONIQ_6_FRICTION_THRESHOLD_CURVE)),
   )
   ```
   (Match the umbrella doc's spec at `.scratch/ioniq-detailed-tuning.md:123` — this plan just names
   the interpolated values as a proper module constant instead of an inline list literal, so the
   values are named and greppable like every other Ioniq-6 tuning constant in this file.)
3. Confirm `IONIQ_6_BASE_FRICTION_THRESHOLD` has no other callers before removing/leaving it — if
   it's now unused, remove it rather than leaving dead code (per this repo's own no-dead-code
   convention); if anything else reads it, keep it and say why in the commit description.
4. Re-run `.scratch/ab_tests/ab_test_friction_curve.py` against the **literal edited file** (not
   just re-confirming the old in-memory monkeypatch numbers) to verify the change reproduces the
   already-measured table before treating it as correct. Fix the script's stale hardcoded
   `data_dir` first — see "Environment" below, this is a known, already-hit gotcha.
5. `/code-review medium` on the diff (function-logic change, not a pure constant swap — matches the
   umbrella doc's own stated review-level recommendation for this exact change).
6. Update `.scratch/ioniq-detailed-tuning.md`'s "Friction-curve follow-up" section status line and
   "Handoff status" section (see "Relationship to the umbrella plan" above) once shipped.
7. Ask before every `git commit`, including this plan file itself and the final constant-change
   commit — standing convention on this branch (see [[feedback_commit_permission]]).

**Explicitly out of scope for this pass:**
- Phase 4 on-device validation drive — this ships as a code change validated by offline replay
  only; an actual drive to confirm real-world effect is separate, tracked work (already an open
  item on the shipped chatter fix too — see umbrella doc's "Open items" section).
- Any change to the shared `HKG_CANFD_BASE_FRICTION_THRESHOLD` / `get_hkg_canfd_base_friction_threshold`
  function itself — Ioniq-6-scoped only, per the scoping decision above.
- Revisiting FLM's live-apply path or its `accept_trial_as_baseline()` gap — that's a framework
  observation worth remembering, not something to patch here.
- The paused left-hug/unwind investigation and its untested candidates — separate, deprioritized
  thread, unaffected by this change (the friction-curve candidate was already confirmed to have
  zero hug-metric effect).
- Re-deriving or re-validating FLM's per-band severity/delta math itself — out of scope; we're
  implementing an already-measured candidate, not re-deriving why FLM suggested it.

## Review gates

**Automated:**
- `.scratch/ab_tests/ab_test_friction_curve.py` run against the literal edited file must reproduce
  the already-measured table (see "Verification" below) within noise — this is the hard gate before
  treating the change as correct. Specifically: combined config (center_fade_max=0.80 + curve)
  chatter mean/window should land at **≈1.015** (vs. the shipped-fix-alone baseline of 1.112); hug
  metrics in all four rows should be unchanged from the umbrella doc's existing table (this harness
  is known-incapable of showing a *new* hug effect either way — see
  [[project_ioniq6_tuning_investigation]] item 9 — so "unchanged" is the expected and only possible
  outcome, not itself a finding).
- Reuse the fidelity check baked into `replay_latcontrol_torque.py`/the ab_test harness pattern
  (trusted-field diff against logged values) on this run — don't skip it because the harness is
  "already validated" from prior runs; it validates the fidelity of *this* run's data, not the code.
- `/code-review medium` on the diff before it's treated as trustworthy — per the umbrella doc's own
  stated review-level call for this change (function-logic change, not a pure constant swap).

**Manual:**
- Before editing `.scratch/ioniq-detailed-tuning.md`'s status lines: show the user the re-verified
  A/B numbers (same 4-row table format as the umbrella doc) and get a read before calling the
  re-verification complete — same discipline as every other result in this investigation.
- Confirm `IONIQ_6_BASE_FRICTION_THRESHOLD`'s other-caller check (scope item 3) manually — a grep,
  not an assumption — before deciding to remove or keep the old constant.
- Ask before every `git commit` — standing convention, applies here too (plan file and the shipping
  diff are separate commits per the existing pattern on this branch, e.g. `057fc8b82` then
  `7cfdb8d5a`).
- No on-device deployment or drive scheduling happens as a side effect of this pass — that's
  tracked separately (see "Out of scope" above); this pass ends at a committed, offline-verified
  code change.

## Escalation criteria — stop and report, don't push through

Extends the umbrella doc's "Escalation discipline" (one focused diagnostic pass, then stop and
report — already proven productive twice in this investigation: the fidelity gap and the
friction-curve monkeypatch bug). Escalate immediately, before further diagnostic work, if:

1. **The re-verified numbers against the literal edited file don't match the already-measured
   table** (combined ≈1.015, not just "some improvement") — beyond ordinary run-to-run noise
   (compare against the ~2% noise band already characterized in the baseline-vs-cleanup check).
   Don't quietly accept a different number as "close enough" without flagging the discrepancy.
2. **The fidelity check on this run shows new mismatches** beyond the already-characterized 8/954k
   baseline (the known `active`-boolean edge-transition gap, see umbrella doc "Fidelity summary") —
   don't trust this run's metrics until understood.
3. **`IONIQ_6_BASE_FRICTION_THRESHOLD` turns out to have other callers** not anticipated by this
   plan — stop and report rather than guessing whether it's safe to leave both the old flat
   constant and the new curve constant coexisting, or silently picking one.
4. **`/code-review medium` surfaces a correctness issue** (not just a style/cleanup nit) in the
   `get_ioniq_6_friction_threshold` edit — stop and resolve with the user before committing, don't
   patch-and-ship in the same pass without a second look.
5. **More than one diagnostic pass is needed** to explain an anomalous or unexpected result — stop
   after the first focused pass and report, rather than iterating further solo.

## Files involved

- `selfdrive/controls/lib/latcontrol_vehicle_tunes.py` — the actual change: new
  `IONIQ_6_FRICTION_THRESHOLD_CURVE` constant (~line 810) and the `get_ioniq_6_friction_threshold`
  edit (~line 3660).
- `.scratch/ab_tests/ab_test_friction_curve.py` — re-run for verification; **needs its hardcoded
  `data_dir`/`route` block in `__main__` fixed first** (see "Environment" below) — same stale-path
  gotcha already hit and fixed in `ab_test_baseline_ff_gain.py`.
- `.scratch/ioniq-detailed-tuning.md` — two small status-line edits, once shipped (see "Relationship
  to the umbrella plan" above).
- Memory: no new memory file needed for this pass — the live-vs-static FLM decision is now recorded
  in this plan file itself; if the shipped result differs meaningfully from the pre-measured
  numbers, update [[project_ioniq6_tuning_investigation]] with the outcome, same as every other
  shipped fix in this investigation.

## Technical confirmation (already checked, don't re-derive)

- `np` is already imported at the top of `latcontrol_vehicle_tunes.py` (`import numpy as np`,
  line 4) — no new import needed.
- `FLM_FRICTION_SPEED_KNOTS` is already a module-level constant in the same file (line 18) and
  already used elsewhere in the same file for other speed-interpolated knobs (e.g. line 4241) — the
  `np.interp(v_ego, FLM_FRICTION_SPEED_KNOTS, values)` pattern being added here is not new to the
  codebase, just new to this specific constant. (This is the same point already made to the user in
  conversation — recorded here so a fresh session doesn't need to re-derive or re-explain it.)
- `.scratch/ab_tests/ab_test_friction_curve.py` already exists, already implements exactly this
  change as an in-memory monkeypatch (confirmed working — it produced the 1.015 number already
  cited above), and already has a `/code-review low` pass behind it from when it was first built
  (see umbrella doc, "Chatter investigation" section: "`/code-review low` found and fixed one
  dead-variable cleanup"). No need to rebuild or re-review the harness itself — only the actual
  source-file change needs `/code-review medium`.
- **Not yet confirmed, must check before removing it (scope item 3)**: whether
  `IONIQ_6_BASE_FRICTION_THRESHOLD` is read anywhere other than the one `max()` call being replaced.
  A quick grep should settle this in seconds; don't skip it and don't guess.

## Environment (self-contained — a fresh session needs all of this, don't assume prior context)

- Worktree: `/home/kirin/starpilot/starpilot-ioniq-analysis`, branch
  `wat-ioniq-torque-analysis-tools`. `git checkout` there first if not already on it. `git status`
  before starting — as of 2026-09-12 there were untracked scratch files
  (`.scratch/check_logged_laf.py`, a `routes` symlink) unrelated to this thread; don't clobber them.
- Python env: `source /home/kirin/starpilot/StarPilot/.venv/bin/activate` (a different worktree's
  venv, reused here). Always run tools with `PYTHONPATH="$PWD"` set to this worktree's root.
- **Route data has moved since this A/B script was written — known, already-hit gotcha.**
  `ab_test_friction_curve.py`'s `__main__` block still hardcodes
  `data_dir = "/mnt/c/Users/Kirin/Documents/ioniq_20260911"`, which **no longer exists**. The route
  is now reachable at `routes/00000031--d305a9f4bd` (worktree-relative; `routes` is a symlink to
  `/mnt/c/Users/Kirin/Documents/ioniq_routes`, confirmed present, 169 segments). Per the pattern
  already applied in `ab_test_baseline_ff_gain.py`
  (see `.scratch/ioniq_baseline_vs_cleanup_check_plan.md`'s own "Results" section note): the route's
  own directory is the `data_dir` to pass, not its parent — i.e. `routes/00000031--d305a9f4bd`, not
  `routes`. **Fix this hardcoded path in `ab_test_friction_curve.py` before running it** — same
  one-line fix already made once in a sibling script.
- Commit discipline: never `git commit` without asking first, even when a step completes cleanly.

## Verification

After the source edit and the `data_dir` fix above:
```
cd /home/kirin/starpilot/starpilot-ioniq-analysis
source /home/kirin/starpilot/StarPilot/.venv/bin/activate
PYTHONPATH="$PWD" python .scratch/ab_tests/ab_test_friction_curve.py
```
This reproduces all 4 rows in one run (~15 min, full 169-segment route × 4 passes). Compare the
printed numbers against the already-measured table:

| config | chatter mean sign-changes/window |
|---|---|
| Pre-fix baseline (center_fade_max=0.50, flat floor=0.39) | 2.251 |
| Curve alone (center_fade_max=0.50 + curve, no center-fade fix) | 2.153 |
| Shipped baseline (center_fade_max=0.80, flat floor=0.39) | 1.112 |
| **Combined (center_fade_max=0.80 + curve) — this is the shipping config** | **1.015** |

The first three rows are re-confirmation of already-shipped/already-measured behavior (should be
unchanged from before); only the fourth row's config becomes the new permanent default from this
pass — but running all four in one pass is the existing script's structure and is cheap insurance
against a stale/wrong constant silently changing one of the other three rows too.

Record the actual rerun numbers in the "Results" section below — don't just report pass/fail
verbally, same convention as every other sub-plan in this investigation.

## Results

**Dead-code check (scope item 3):** repo-wide grep for `IONIQ_6_BASE_FRICTION_THRESHOLD` found
exactly one caller — the `max()` call being replaced — plus one docstring mention in
`ab_test_friction_curve.py`. Removed the constant; nothing else reads it.

**Re-verification run (against the literal edited `latcontrol_vehicle_tunes.py`):**

| config | expected | got |
|---|---|---|
| Pre-fix baseline (curve off, fade=0.50) | 2.251 | **2.251** |
| Curve alone (curve on, fade=0.50) | 2.153 | **2.153** |
| Shipped baseline (curve off, fade=0.80) | 1.112 | **1.112** |
| Combined (curve on, fade=0.80) — shipping config | **1.015** | **1.015** |

All four rows match exactly; hug metrics unchanged across all rows in all runs, as expected.

**Gotcha hit and fixed along the way:** after editing the source, `ab_test_friction_curve.py`'s
"curve off" branch pointed at `tunes.get_ioniq_6_friction_threshold` as its unpatched reference —
but that function now has the curve baked in permanently (that's the shipped change), so both
branches were silently running the curve. First rerun attempt produced 2.153/2.153 and 1.015/1.015
(the "curve on" values duplicated into the "curve off" rows) instead of the expected table —
caught by escalation criterion 1, not pushed through. Fix: added a frozen
`flat_floor_get_ioniq_6_friction_threshold` to the test script (copy of the pre-edit function body,
flat `HKG_CANFD_BASE_FRICTION_THRESHOLD=0.39` floor) and pointed the "curve off" branch at that
instead. Test-script-only change; no effect on the shipped source edit. Also separately hit and
fixed one background-run OOM kill (system-wide memory pressure, unrelated to the script; a bare
retry with more free memory succeeded).

**`/code-review medium` outcome:** clean, no findings (`[]`). Reviewer confirmed: matches the
documented FLM curve data exactly, doesn't break `test_ioniq_6_friction_threshold_curve`'s existing
assertions (verified numerically), and the dead-constant removal has exactly one caller.

**Final commit SHA:** `8fdaa8e6a` — bundles the source change, the AB-script fixes, this Results
section, and the umbrella doc's status-line updates, matching the prior sub-plan commit pattern
(e.g. `7cfdb8d5a`).

**Deployed by cherry-picking `8fdaa8e6a` onto `wat-ioniq-tuning`** (`/home/kirin/starpilot/starpilot-wat-ioniq-merge`,
commit `bbfb9a647`, pushed to `origin/wat-ioniq-tuning`) — same pattern as the bucket-excusal fix's
own deployment (see `.scratch/ioniq_torqued_bucket_excusal_fix_plan.md`). Only the
`latcontrol_vehicle_tunes.py` change carried over: `.scratch/ab_tests/ab_test_friction_curve.py` and
`.scratch/ioniq-detailed-tuning.md` don't exist on `wat-ioniq-tuning` and were dropped from the
cherry-pick, noted in that branch's own commit message. This file is copied over separately (see
below) since it's self-contained and doesn't depend on the umbrella doc existing on this branch.

**Post-ship spot-check, unrelated finding:** while independently re-verifying (rather than trusting
a review subagent's claim) that this change doesn't break `test_latcontrol.py`'s friction tests, ran
`pytest selfdrive/controls/tests/test_latcontrol.py -k friction` — 35 passed, 1 failed
(`test_ioniq_6_friction_center_fade_curve`, asserts
`get_ioniq_6_friction_center_fade_scale(0.0, 30.0) >= 0.5`). Confirmed via a throwaway worktree at
the immediately-prior commit (`0f459b127`) that this fails identically there too — **pre-existing,
not caused by this change**. Root cause: the assertion's `>= 0.5` floor was only ever true when
`IONIQ_6_FRICTION_CENTER_FADE_MAX = 0.50`; the earlier chatter fix (`ca218d4fd`, this same
investigation) deliberately raised it to `0.80` specifically to allow *more* fade near center at
highway speed (down toward a ~0.2 floor) to cut chatter — a real, intended, already-shipped and
A/B-validated behavior change — but never updated this test's stale assertion to match. **Not fixed
here** (out of scope for this pass, belongs to the `ca218d4fd` commit); left as a flagged loose end
for whoever picks it up next. Fix is a one-line test-threshold update in
`selfdrive/controls/tests/test_latcontrol.py`, not a source change.
