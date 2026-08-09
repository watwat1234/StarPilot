# Correct the Bolt's base torque parameters

Successor to `update_controller.md`, which fixed a **defect** (the `unwind_detected` sign inversion). This plan
addresses **tune values** — a different class of change, and one that must be staged.

## Context

`update_controller.md` C5 recorded a provisional pass on the unwind fix: right-side turn-in overshoot fell (`peak_opp`
p90 0.6932 → 0.2815) while the left control arm held. The driver confirms right turns feel less "slammy."

But both routes surfaced a larger, untouched problem. **The Bolt's configured `LAT_ACCEL_FACTOR` is roughly 1.7×
too high**, and four independent sources agree on it:

| source | latAccelFactor |
|---|---|
| configured — [override.toml:55](../opendbc_repo/opendbc/car/torque_data/override.toml) | **2.0** |
| live estimator, pre-fix route (converged) | 1.2916 |
| offline `TorqueEstimator` fit, pre-fix route | 1.1087 |
| offline `TorqueEstimator` fit, post-fix route | 0.9865 |
| sibling `CHEVROLET_BOLT_EUV` — [override.toml:60](../opendbc_repo/opendbc/car/torque_data/override.toml) | **1.0** |

The EUV row is the strongest tell: mechanically the same steering rack, tuned to 1.0, while every other
`CHEVROLET_BOLT_*` variant sits at a flat `[2.0, 2.0, 0.13]` that reads like an unmeasured default. This car's own
data lands on the EUV's number.

**Mechanism:** feedforward under-delivers, so the integrator carries the load — `mean|i|` runs 0.21–0.29 across all
phases, high for a car that should be predominantly feedforward-driven. Integrator-led steering is laggy, and lag
produces overshoot. This is a plausible source of the slamminess remaining after the unwind fix.

Secondary finding: **`latAccelOffset` is configured at 0.0 but four sources say ≈ −0.33** (live −0.3182 / −0.3330,
offline fit −0.2909 / −0.3454). A uniform lateral bias is exactly what produces left/right asymmetry.

**Friction needs no change** — configured 0.13, fits at 0.149 / 0.162, live 0.137. Leave it alone.

**Intended outcome:** correct the base parameters so feedforward does the work it is supposed to, then remove the
compensating hacks layered on top of the wrong base.

---

## Chunk ordering

| chunk | scope | drives | status |
|---|---|---|---|
| T1 — analyzer completeness | build provenance + `center_output_scale` | 0 | **next** |
| T2 — benchmark loop | fixed route, new baseline | 1 | after T1 |
| T2.5 — live-params probe | drop `ForceAutoTune` to level 2, one-drive go/no-go | 1 | after T2 |
| T3 — `LAT_ACCEL_FACTOR` mult | static value, informed by T2.5 | 1–3 | after T2.5 |
| T4 — `latAccelOffset` | ≈ −0.33 | 1–2 | after T3 |
| T5 — revisit `ffScaleNeg` | remove the +11.2% right bias | 1 | after T4 |

T1 and T2 are prerequisites, not optional. T2.5 de-risks T3 by testing the hypothesis in one drive instead of three.
T3 is the substantive change.

---

## T1 — Analyzer completeness (no driving)

Carried over from `update_controller.md` C4, items 2 and 3. Item 1 (FLM awareness) stays deferred — no FLM trial has
been active on any route so far.

1. **Build provenance.** `initData` carries `gitCommit`, `gitCommitDate`, `gitBranch`. The analyzer already parses
   `initData` but pulls only `params`. Print all three.
2. **`center_output_scale` column.** `get_bolt_2022_2023_center_output_scale(setpoint, vEgo)` multiplies output torque
   near center at low speed — the slam region — and appears in no measurement so far. Add as a fourth column beside
   `ff_scale` / `friction_scale` / `thresh_ratio`.

**Why first:** T3 is a multi-drive sweep. Without provenance, no report can tell you which commit drove which route —
that ambiguity already cost a clean answer in C5.

**Verify:** provenance line present and matching the device's `git log -1`; `center_output_scale` column populated.

## T2 — Establish a benchmark loop (1 drive)

Pick **one** route and drive it for every subsequent measurement. Ideally the original cruise-around-town loop, which
reflects everyday driving better than the block-circling post-fix route.

C5's route mismatch (2.2× `low_speed_sharp`, +13–49% `mean|d_des|`) is why that chunk is only a provisional pass. A
staged sweep across unmatched routes is uninterpretable no matter how good the analyzer gets.

**Verify:** report header matches the pre-fix baseline on `staticTune=`, `effective=`, `useLiveParams=0`,
`tuningLevel=2`; roll exposure within ~10% of `abs_p95_deg=3.895`; this becomes the new reference table.

## T2.5 — Live-params probe (1 drive)

### Why this exists

None of T3–T5 are reachable from the galaxy UI. The complete FLM tunable surface is 31 suffixes at
[latcontrol_vehicle_tunes.py:2899](../selfdrive/controls/lib/latcontrol_vehicle_tunes.py) — all ff-gain, taper,
deadband, threshold, angle-assist and curvy trims. There is no `lat_accel_factor`, no `lat_accel_offset`, and no global
ff scale. `ffScalePos`/`ffScaleNeg` are static tune constants, also absent. Galaxy only *reads* tuning levels
([the_galaxy.py:497](../starpilot/system/the_galaxy/the_galaxy.py)); it grants no new knobs.

But the live estimator **already computes the values this plan is trying to reach** — 1.2916 / −0.3182 on the pre-fix
route. It is simply not consumed (`useParams=False`). Enabling it tests T3 and T4 together in a single drive.

### The gate, and why level 2 is the right unlock

`use_live_params = has_auto_tune or force_auto_tune`. GM is absent from `torqued`'s `ALLOWED_CARS`, so
`has_auto_tune = 0` and this requires `ForceAutoTune`.

A **"Force Auto-Tune On"** toggle already exists at
[lateral.py:255](../selfdrive/ui/layouts/settings/starpilot/lateral.py) and is already `enabled` for this car
(`not hasAutoTune and isTorqueCar and not isAngleCar` — all true). It does nothing because `ForceAutoTune` is gated at
**level 3** ([params_keys.h:323](../common/params_keys.h)) and this device is at level 2. The toggle writes `True`, the
UI shows it on, and the controller ignores it — a silent no-op.

`ForceAutoTuneOff` is already **level 2** ([params_keys.h:324](../common/params_keys.h)). The "off" direction is
reachable and the "on" direction is not, with no evident justification. **Dropping `ForceAutoTune` to level 2 corrects
that asymmetry.**

Rejected alternative: raising `TuningLevel` to 3. `TuningLevel` is itself ungated (level 0,
[params_keys.h:650](../common/params_keys.h)) so it is settable over SSH, but it would unlock *every* level-3 param
(`SteerKP`, …) device-wide — far larger blast radius than one gate.

### Purpose: go/no-go, not a permanent config

One drive answers whether factor ≈1.29 + offset ≈−0.33 improves the car.

- **Improves** → hardcode a static mult in T3 with confidence, skipping the blind staged sweep.
- **Does not improve** → four drives saved, and the diagnosis needs re-examining before touching the base.

Live params are a poor permanent configuration — continuous adaptation means every drive has a different tune, which
destroys the reproducibility this plan depends on. Revert after the probe.

### Two-file change — both required

1. [params_keys.h:323](../common/params_keys.h) — `{"ForceAutoTune", {PERSISTENT, BOOL, "0", "0", 3}}` → level **2**.
2. [analyze_bolt_lateral.py:310](../tools/tuning/analyze_bolt_lateral.py) — the analyzer's mirrored level table,
   `"ForceAutoTune": 3` → **2**.

Leaving item 2 stale makes the analyzer report `useLiveParams=0` while the car actually ran live params — a silent
misreport of exactly the class of failure C4 exists to prevent.

Consumed via the level-gated `get_value` at
[starpilot_variables.py:721](../starpilot/common/starpilot_variables.py), whose condition includes `not has_auto_tune`.
**This is therefore a no-op on the Ioniq 6**, which has native auto-tune.

### Hazards — check before trusting the route

- [manager.py:549](../system/manager/manager.py) seeds `ForceAutoTuneOff=True`, and the UI makes the two mutually
  exclusive ([lateral.py:259-270](../selfdrive/ui/layouts/settings/starpilot/lateral.py)). Confirm the seeding is
  one-time-if-unset, not every boot. *This also explains the C5 route delta — the rebuild between drives reseeded it.*
- [safe_mode.py:28](../starpilot/common/safe_mode.py) clears `ForceAutoTune`. A mid-drive safe-mode trip silently
  reverts to the static tune.
- T3 and T4 are **inseparable** in this drive — the estimator supplies both factor and offset. Acceptable for go/no-go,
  useless for attribution.

**Verify:** report shows `ForceAutoTune=1`, `resolved=useLiveParams=1`, and `effective=` matching
`liveTorqueFiltered` rather than `static=`. If `effective=` still matches `static=`, the gate change did not take.

## T3 — `LAT_ACCEL_FACTOR` mult (1–3 drives)

Add `BOLT_2022_2023_BASE_LAT_ACCEL_FACTOR_MULT` to `latcontrol_vehicle_tunes.py`, following the established pattern
and applied at the existing dispatch in
[latcontrol_torque.py:123-142](../selfdrive/controls/lib/latcontrol_torque.py) (static) and
[153-175](../selfdrive/controls/lib/latcontrol_torque.py) (live).

**GM is the only brand missing from that list.** Existing entries:

| constant | value |
|---|---|
| `PALISADE_BASE_LAT_ACCEL_FACTOR_MULT` | 0.98 |
| `SONATA_HYBRID_…` / `KIA_FORTE_…` | 1.05 |
| `IONIQ_EV_OLD_…` | 1.16 |
| `RAM_1500_…` / `CIVIC_BOSCH_MODIFIED_B_…` | 1.20 |
| `IONIQ_5_…` / `IONIQ_6_…` | 1.22 |
| `CIVIC_BOSCH_MODIFIED_B_VARIANT_…` | 1.75 |

Use this mechanism rather than editing `override.toml` — that file is vendored from opendbc and would collide on the
next upstream drop. The mult is also Bolt-gated, so the Ioniq 6 is untouched.

**Target informed by T2.5.** If the probe improved the car, set the mult directly from the factor the estimator
converged on (≈1.29 → mult ≈0.65) and verify in **one** drive. If the probe was inconclusive, fall back to the blind
staged sweep: **0.85 → 0.75 → 0.65, one drive each.**

> ⚠️ **The target is outside the established range.** Existing mults span 0.98–1.75; reaching factor ≈1.2 needs ≈0.6.
> That is consistent with the diagnosis (a wrong base, not a trim) but it is why this is staged rather than applied in
> one step.
>
> ⚠️ **Single-variable in source, not in effect.** `ffScalePos` / `ffScaleNeg`, `ff_gain_left/right` and
> `turn_in_boost_*` were all calibrated on top of factor = 2.0. Lowering the base scales every one of them up
> proportionally. Do not adjust them mid-sweep — that is T5.

**Verify per stage:** `mean|i|` falls and `|f|` rises (feedforward taking over from the integrator); turn-in
`peak_opp` and `at_limit` do not regress; no new saturation (`all_non_sat` n stays close to `all` n). Abort the sweep
if steering feels twitchy or `peak_abs` p90 rises.

## T4 — `latAccelOffset` ≈ −0.33 (1–2 drives)

No static offset knob exists today, and the live path is blocked (`TuningLevel=3` + `TuningLevelConfirmed`, which this
device does not have). Add a new constant on the same per-car pattern as T3.

**Refit first.** Correcting the factor will move the offset estimate — re-read `TorqueEstimator fit` and
`liveTorqueFiltered` from the final T3 drive rather than reusing the −0.33 above.

**Verify:** left/right `bias` in `ControlsState tracking` converges; `Torque map residuals` `bias` (currently −0.17 to
−0.20 on both routes) moves toward zero.

## T5 — Revisit `ffScaleNeg` (1 drive)

`ffScaleNeg = 1.1449` vs `ffScalePos = 1.03` is a +11.2% right-side bias. **Hypothesis: it exists to compensate for
the uncorrected −0.33 offset.** If T4 lands, keeping both would over-correct the right side.

Test by reverting `ffScaleNeg` toward `ffScalePos` and measuring. If right-side tracking degrades, the asymmetry is
real and independent; if it improves or holds, it was a hack and should go.

**Verify:** right-side `mae` and `bias` no worse than the T4 result; turn-in event asymmetry unchanged or improved.

---

## Why not the fallback levers

`gm_bolt_2022_2023.ff_gain_right`, `turn_in_boost_right` and `unwind_taper_right` are FLM-live at this device's tuning
level ([latcontrol_vehicle_tunes.py:3172-3182](../selfdrive/controls/lib/latcontrol_vehicle_tunes.py)) and therefore
tempting as a first move — no source edit, no rebuild.

Skip them. They would paper over a wrong base value, and every one of them would need redoing after T3 anyway. T2.5
achieves the same "test before committing" goal without that problem: it probes the *actual* corrected base rather
than an additive, region-limited approximation of it.

## Out of scope

- The unwind fix itself — settled in `update_controller.md`, do not revisit mid-sweep.
- FLM awareness in the analyzer (C4 item 1) — no FLM trial has been active on any measured route.
- The Chunk 5b `medFF/medFric/n` table field-width bug.
- Oscillation diagnosis, parked in `update_analyzer.md`.
- Friction (0.13) — measured correct, leave it.
- The Ioniq 6. Every change here is Bolt-gated by construction.
