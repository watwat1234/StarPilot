# Fix the `unwind_detected` sign inversion in `latcontrol_torque.py`

Successor to `update_analyzer.md`, which was diagnosis only. This plan is the controller fix it deferred.

## Context

`update_analyzer.md` ran five diagnostic chunks against a recorded 2022/2023 Bolt route and concluded that one defect
explains the driver's reported symptom: a right 90° turn where the wheel **slams right**, followed by an
**overcorrection to the left**, both requiring intervention.

Those chunks were *analyzed* against an older checkout (the routes themselves were driven on the latest branch), and
`latcontrol_torque.py` has since been restructured from ~2200 lines to 495, with the per-vehicle tune layer extracted
into `latcontrol_vehicle_tunes.py` and a live FLM override system added. Every load-bearing claim was therefore
re-verified against current source (`8640f0605`) before committing to this fix.

**Result of that reassessment: the premise holds, FLM cannot fix it, and it is a fork-original defect roughly five
months old.** Details in "What the reassessment established" below.

**Intended outcome:** correct the sign, globally, as a single-variable change, and validate it against the existing
route — which is a valid baseline because it was driven on current code.

Decisions taken with the user: **fix globally**, not gated to the Bolt (private fork; the condition is wrong on every
platform). **Defer all analyzer work** until there is a post-fix drive to measure. The Chunk 5b table-formatting bug
stays out of scope.

---

## What the reassessment established

### The defect, re-verified on current code

[latcontrol_torque.py:217](../selfdrive/controls/lib/latcontrol_torque.py) tests a **signed** rate where it means to
test whether *magnitude* is decreasing:

```python
unwind_detected = (desired_lateral_accel_rate < UNWIND_D_DES_THRESHOLD and   # -1.0
                   abs(setpoint) < UNWIND_LAT_ACCEL_NEAR_ZERO)               # 0.3
```

Entering a right turn from straight, setpoint falls 0 → −2, so the rate is negative and the freeze fires while
`|setpoint|` is still small. Exiting a right turn, setpoint rises −2 → 0, so it never fires. Left turns get the
intended behaviour on both. It feeds `freeze_integrator` at
[latcontrol_torque.py:397-398](../selfdrive/controls/lib/latcontrol_torque.py) — a claim `update_analyzer.md` asserted
but never demonstrated; now confirmed.

Chunk 2 measured the inversion as near-perfect: `entering_right` fires at 0.0777 (n=12425), `exiting_right` at 0.0000
(n=13781), mirrored on the left.

### Provenance: fork-original, ~5 months old, every branch

| fact | evidence |
|---|---|
| Introduced **2026-03-12** | commit `d0e1db676`, author `firestar5683`, message "StarPilot" |
| Never modified since | `git log -S"UNWIND_D_DES_THRESHOLD"` returns exactly one commit |
| **Not upstream** | openpilot v0.10.3 vendor drop (`33d5cfc39`) has zero occurrences |
| Fork-wide | present on `StarPilot`, `Dom`, `Dom_wat`, `StarPilot_wat`, `custom_github/Dom` |
| Ungated by car | sits in the common `update()` above every car-specific branch |

### FLM cannot fix it — only compensate

FLM is wired into the controller at runtime ([latcontrol_torque.py:188](../selfdrive/controls/lib/latcontrol_torque.py))
and — unlike `ForceAutoTune` — **its params are live on this device**: level 2, read via direct `params.get()` at
[starpilot_variables.py:724-729](../starpilot/common/starpilot_variables.py), bypassing `get_value`'s tuning-level
gate. This corrects the standing assumption that tune-side levers are all blocked at level 2.

All 31 generic suffixes in `FLM_FULL_SURFACE_SUFFIX_METADATA` plus every per-vehicle entry in
`FLM_SUPPORTED_VEHICLE_KNOBS` are ff-scale, friction-threshold, center-deadband, or angle-assist. Grepping the tune
module for `_k_i`, `pid.i`, `integrator`, `freeze` returns **zero hits**. Note the fork contains a *correct*,
FLM-tunable unwind classifier (`tanh(la × jerk)` → `unwind_weight`) alongside the broken one gating the integrator.

Partial compensation is possible — the ff layer's onset (`|la| = 0.12`) overlaps the freeze window
(`|setpoint| < 0.3`), so `ff_gain_right` / `turn_in_boost_right` can inject feedforward exactly where the integrator
is frozen. Rejected as the primary route: it is open-loop propping of a backwards feedback path, covers only
`[0.12, 0.3]`, and over-applies once the freeze releases. Retained as fallback.

### The existing route is a valid baseline

The routes were driven on the latest branch; only the *analysis* used an old checkout.

- **Log-derived sections were never affected.** Chunk 2's unwind reconstruction reads logged `desiredLateralAccel`
  plus constants hardcoded locally in the analyzer
  ([analyze_bolt_lateral.py:47-48](../tools/tuning/analyze_bolt_lateral.py): `DT_CTRL 0.01`, `-1.0`, `0.3`) — all
  unchanged in current source. Chunk 5's event table and the tracking summary import no controller code either.
  **So Chunk 5's table is a valid "before."** No extra baseline drive is needed.
- **Reconstructed sections ran against stale definitions, but the values did not move.** Current source has
  `FF_GAIN_LEFT 0.11` / `RIGHT 0.06`, onset `0.12`, cutoff `1.35`, `FRICTION_MULT 1.09`, `UNWIND_TAPER 0.38/0.40` —
  identical to Chunk 4's report. Chunk 1b's four repurposed fields verify unchanged too. Both stand.
- **Two things are genuinely absent from the old analysis**, neither invalidating: the new `center_output_scale` term
  and FLM-override awareness.

---

# Chunk ordering

**C1 → C2** is the whole fix. **C3 → C5** are deferred and only run when there is a post-fix drive worth measuring.

| chunk | scope | status |
|---|---|---|
| C1 — the sign fix | one expression in `latcontrol_torque.py` | **done** — `d55b679f5` |
| C2 — validate and drive | test suite + subjective drive | **done** — driver reports right turns "less slammy" |
| C3 — analyzer repair | required before the analyzer runs at all | **done** — dual old/new unwind columns |
| C4 — analyzer completeness | FLM awareness, `center_output_scale`, build provenance | deferred |
| C5 — before/after measurement | re-run analyzer, diff against Chunk 5 | **done** — provisional pass, see results |

---

## Chunk C1 — The sign fix

`selfdrive/controls/lib/latcontrol_torque.py`, one expression at line 217:

```python
# current
unwind_detected = (desired_lateral_accel_rate < UNWIND_D_DES_THRESHOLD and
                   abs(setpoint) < UNWIND_LAT_ACCEL_NEAR_ZERO)

# fixed — test whether magnitude is decreasing, not whether the signed value is
unwind_detected = ((abs(setpoint) - abs(self.prev_desired_lateral_accel)) / self.dt < UNWIND_D_DES_THRESHOLD and
                   abs(setpoint) < UNWIND_LAT_ACCEL_NEAR_ZERO)
```

Applied **globally** on the shared path, per the scoping decision.

- Leave `UNWIND_D_DES_THRESHOLD` and `UNWIND_LAT_ACCEL_NEAR_ZERO` at current values. Changing a threshold in the same
  commit would forfeit the single-variable property.
- `desired_lateral_accel_rate` may still be referenced elsewhere (logging/analysis). Check before removing the
  assignment; leave it in place if so.
- The correct classifier already exists in the fork — `tanh(la × jerk)` feeding `unwind_weight` in
  `latcontrol_vehicle_tunes.py`. Not reused here: it is phase-shaped for gain scheduling, not a boolean freeze gate,
  and swapping gate semantics is a larger change than correcting the sign.

**Verify:** diff is one expression in one file.

## Chunk C2 — Validate and drive

1. `pytest selfdrive/controls/tests/test_latcontrol.py` — 112 tests. None currently pin `unwind_detected` or
   `freeze_integrator` (every `unwind` reference in that file is to the `tanh` phase classifier), so this should pass
   unchanged. **A failure means something depends on the broken behaviour and must be understood before driving.**
2. Drive the same roads. The subjective test is the reported symptom: does the right 90° turn still slam and
   overcorrect?

### Behavioural notes for the drive

- **It changes both sides.** It stops freezing at right turn-in (removing the slam mechanism) and starts freezing at
  right turn-exit, which this car has never done. Inherent to correcting the sign, not something to engineer around.
- **Right entry from a left curve still freezes.** Setpoint +0.5 → −2 passes through a stretch where `|setpoint|` is
  genuinely decreasing while still `< 0.3`. That is semantically correct — it really is unwinding the left turn — so
  only right entries from straight are fully fixed.
- **The Ioniq 6 is affected too**, and being Hyundai it *is* in `torqued`'s `ALLOWED_CARS`, so it runs live torque
  params rather than the Bolt's static tune. Worth a short sanity drive on that car as well.

**Verify:** test suite green; drive completed on the same roads with the symptom noted subjectively.

## Chunk C3 — Analyzer repair — **deferred, required before any post-fix run**

The analyzer currently fails at import. `latcontrol_torque` does `from latcontrol_vehicle_tunes import *` with a
permissive `__all__`, so eight of the nine imported names still resolve; only `get_friction_threshold` is gone.

- Drop `get_friction_threshold` from the import block at
  [analyze_bolt_lateral.py:11-20](../tools/tuning/analyze_bolt_lateral.py) and replace its use at line 469 with
  `get_gm_base_friction_threshold(v)`, mirroring the brand dispatch at
  [latcontrol_torque.py:328-333](../selfdrive/controls/lib/latcontrol_torque.py) (`is_gm` branch).
- Fix the stale comment: the inactive branch now seeds `prev_desired_lateral_accel = future_desired_lateral_accel`,
  not `0.0`.

**Verify:** `analyze_bolt_lateral.py <route>` runs to completion; two consecutive runs byte-identical (the Chunk 1b
RNG seed still holds); pre-existing sections unchanged against the last recorded output.

## Chunk C4 — Analyzer completeness — **deferred**

1. **FLM awareness.** Chunk 4 calls the real gain functions, which now read a module-global override dict via
   `_flm_vehicle_knob`. Offline that dict is empty, so the analyzer silently uses *defaults* — if a drive ran with an
   FLM trial applied, the reconstruction is wrong with no indication. `FLMActiveOverrides` / `FLMTrialApplied` /
   `FLMActiveProfileId` are level 2 with no `DONT_LOG` ([params_keys.h:332-336](../common/params_keys.h)), so read
   them from `initData.params` (Chunk 1 already built this plumbing) and apply via `set_flm_runtime_overrides()`,
   reproducing the gate at [latcontrol_torque.py:186-189](../selfdrive/controls/lib/latcontrol_torque.py). Print the
   resolved state, including an explicit "no FLM trial applied" — silence is what makes this failure mode dangerous.
2. **New `get_bolt_2022_2023_center_output_scale(setpoint, vEgo)`**
   ([latcontrol_torque.py:425](../selfdrive/controls/lib/latcontrol_torque.py)) multiplies output torque near center
   at low speed — the slam region — and is in no chunk's model. Add it as a fourth column alongside `ff_scale` /
   `friction_scale` / `thresh_ratio`. Direction-agnostic, so not a new asymmetry source, but it must be visible in the
   baseline or it will later be misread as a fix effect.
3. **Record the build that drove each route.** `initData` carries `gitCommit`, `gitCommitDate` and `gitBranch`
   ([log.capnp:154-156](../cereal/log.capnp)). The analyzer already handles `initData` at
   [line 793](../tools/tuning/analyze_bolt_lateral.py) but pulls only `params`. Capture and print those three fields
   so "which code drove this?" is answered by the report rather than by archaeology — the exact ambiguity that
   prompted this reassessment.

**Verify:** FLM state line present and correct against the log; `center_output_scale` column populated; build
provenance printed for each route.

## Chunk C5 — Before/after measurement — **deferred**

Re-run the analyzer unchanged on the post-fix drive and diff against the Chunk 5 table in `update_analyzer.md`.

**Primary criterion — categorical, needs no baseline.** The unwind reconstruction should flip:

| phase | before | expected after |
|---|---|---|
| `entering_right` `unwind_frac` | 0.0777 (n=12425) | ~0 |
| `exiting_right` `unwind_frac` | 0.0000 (n=13781) | nonzero |

That directly verifies the code does what it says, independent of sample size.

**Secondary criterion — the Chunk 5 event table**, valid as a before because the drive was on current code. Right-side
p90 `peak_abs` (1.1682), `at_limit` (0.7355) and `peak_opp` (0.6932) should fall toward the left figures (0.7054 /
0.3915 / 0.4334). Still n=15 per direction, so suggestive rather than decisive.

---

## Chunk C5 — results (2026-08-08) — **provisional pass**

Post-fix drive analyzed with the C3 analyzer. Both routes on `CHEVROLET_BOLT_ACC_2022_2023`, `tuningLevel=2`,
`useLiveParams=0`, identical `effective=` tune (`latAccelFactor 2.0 / latAccelOffset 0.0 / friction 0.13`), both
`verdict: consistent with static tune`. No FLM trial on either, so the C4 FLM blind spot does not contaminate this.

### The stated primary criterion was invalid — do not reuse it

C5 above claims the unwind flip "directly verifies the code does what it says." **It does not.** The analyzer
reconstructs both conditions from logged `desiredLateralAccel`, so the columns are computed identically regardless of
which build recorded the route. `entering_right unwind_new = 0.0000` is guaranteed by construction on *any* route —
`|setpoint|` growing cannot yield a negative magnitude rate. The post-fix table reproduces the pre-fix table almost
exactly, as it must:

| phase | pre `old` | pre `new` | post `old` | post `new` |
|---|---|---|---|---|
| entering_left | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| entering_right | 0.0777 | 0.0000 | 0.0786 | 0.0000 |
| exiting_left | 0.0579 | 0.0579 | 0.0643 | 0.0643 |
| exiting_right | 0.0000 | 0.0539 | 0.0000 | 0.0649 |

What this table *does* give is **exposure**: the two conditions disagree on 7.9% of `entering_right` and 6.5% of
`exiting_right` samples. That bounds how much of the drive the fix could have changed.

It also served its real purpose as a **known-answer control** on the pre-fix route: `unwind_old` reproduced the
Chunk 2 baseline (0.0777 / 0.0000) exactly, confirming the reconstruction is faithful.

**Nothing in the report verifies which commit drove the route.** That is exactly the gap C4 item 3
(`initData.gitCommit`) exists to close, and it is now the highest-value remaining analyzer work.

### Primary evidence: right-side overshoot fell, left held

`Turn-in events`. Left is the control arm — the fix cannot affect left turns.

| metric | right pre | right post | left pre | left post |
|---|---|---|---|---|
| `peak_opp` med | +0.0049 | **−0.0360** | +0.1656 | +0.1391 |
| `peak_opp` p90 | 0.6932 | **0.2815** (−59%) | 0.4334 | 0.3644 (−16%) |
| `at_limit` p90 | 0.7355 | **0.4267** (−42%) | 0.3915 | 0.3767 (−4%) |
| `peak_abs` p90 | 1.1682 | 0.8869 (−24%) | 0.7054 | 1.0033 |
| n | 15 | 31 | 16 | 31 |

The asymmetry inverted: right `peak_opp` p90 went from 1.6× left to 0.77× left. The median right turn-in now shows
*negative* opposite-direction excursion — no overcorrection at all. Left moved far less on both diagnostic metrics.
Left `peak_abs` rising is one outlier (2.0681 at 37:45.6, `pressed=0.5033` — driver on the wheel).

### Corroboration: integrator moved where the mechanism predicts

Removing a spurious freeze during right turn-in should let the integrator wind specifically there. `mean|i|` by phase:

| phase | pre | post | change |
|---|---|---|---|
| entering_left | 0.2015 | 0.2567 | +27% |
| **entering_right** | 0.1639 | **0.3296** | **+101%** |
| exiting_left | 0.2060 | 0.2910 | +41% |
| exiting_right | 0.1743 | 0.3990 | +129% |

Both right phases roughly doubled while left rose about a third. The two phases the fix touches moved most.

### Confound, stated plainly

**The routes are not matched.** The post-fix drive was deliberately block-circling to maximize turn count, versus a
"cruise around town" pre-fix drive:

- `low_speed_sharp` n 11457 → 25243 (2.2×) while total samples *fell* 172463 → 145748
- `turn_in` n 5725 → 9049; `unwind` n 5732 → 16194 (2.8×)
- `mean|d_des|` up in every phase, +13% to +49% — higher steering demand throughout
- roll exposure down: `abs_p95_deg` 3.895 → 2.934

Enriching the turn-in sample is a feature (n per direction doubled, 15 → 31). But it inflates the left-arm baseline
and makes the across-the-board tracking degradation (`all` mae 0.0933 → 0.0997; `right |i|` 0.1903 → 0.3819)
**not cleanly attributable** to the fix.

**Second config delta:** `ForceAutoTuneOff` went 0 → 1 between drives and `liveTorqueFiltered` was reset
(`latAccelFactor` 1.2916 → 2.0000). Control-path impact is nil — `useLiveParams=0` and `effective=` identical in both
— but the single-variable property is no longer strictly intact.

### Verdict

Three independent signals agree: right-side overshoot down while left held, right-phase integrator up as the
mechanism requires, and the driver's subjective report. **Suggestive and consistent, not proof** — a 2.2× shift in
route character cannot be ruled out as a partial cause. Recorded as a provisional pass. A matched-route rerun (same
loop as the pre-fix drive) would settle it cheaply.

---

## What to expect, stated honestly

The freeze applies only while `|setpoint| < 0.3` — roughly 0.6 s per right turn-in (9.6 s ÷ 15 events). Chunk 5 found
no unwind-freeze signature in `mean_i_pre` at n = 15. Two other same-direction contributors are untouched here:

1. The uncorrected ~0.34 m/s² feedforward bias (GM absent from `torqued`'s `ALLOWED_CARS`, so the static tune runs).
2. The 11.2% right-biased static `ff_scale` (`ffScalePos 1.03` vs `ffScaleNeg 1.1449`).

So the mechanism is confirmed present and confirmed inverted, but its *share* of the observed excursion is unmeasured,
and a clean subjective improvement is not guaranteed.

It is still the right first move: it is the only one of the three that is a **defect** rather than a tune preference,
it has been wrong fork-wide for five months, and it is a single-line, single-variable change against an existing
baseline.

## Fallback levers, if the fix proves insufficient

Do not bundle these with C1 — they would destroy the single-variable property.

- `gm_bolt_2022_2023.ff_gain_right` / `turn_in_boost_right` via FLM. No source edit, live at this device's tuning
  level, and active in `[0.12, 0.3]` which overlaps the freeze window.
- `gm_bolt_2022_2023.unwind_taper_right` to damp the exit-side overcorrection.
- Contributor 1 (the ff bias) remains blocked in practice: reaching the live tune needs `TuningLevel=3` **and**
  `TuningLevelConfirmed=True`, which this device does not have.

## Out of scope

- The Chunk 5b table-formatting bug (`medFF/medFric/n` field width) — deferred by the user.
- Oscillation diagnosis — parked in `update_analyzer.md` and unchanged by this plan.
- Any change to tune values, the cereal schema, or `latcontrol_vehicle_tunes.py`.
