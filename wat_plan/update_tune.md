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

The FLM report disagrees, twice, and both are declined on the record so this does not get re-litigated off that
report. It wants (a) the `gm` friction **threshold curve** raised to quiet center chatter — a different quantity from
the coefficient, covered by none of the four sources above, and downstream of the same wrong base; and (b)
`SteerFriction` lowered 0.130 → 0.112 to speed up unwind — the opposite direction from every measurement. Both are
symptom patches on a base that T3 is about to move. Revisit only if something survives T3–T4.

**Intended outcome:** correct the base parameters so feedforward does the work it is supposed to, then remove the
compensating hacks layered on top of the wrong base.

**A fifth source, added 2026-08-08.** The on-device FLM report `flm-1786229999.html` / `.json` covers the same
post-fix drive C5 analyzed (route `00000101--2f56dfdd5a` from segment 10, branch `Dom_wat_analyzer_tuning`, commit
`d55b679f5`). It reaches the same conclusion from a separate pipeline — see *Why not the fallback levers* — and its
symptom breakdown is cited in T3 and T4. It does not contradict anything below.

---

## Chunk ordering

| chunk | scope | drives | status |
|---|---|---|---|
| T1 — analyzer completeness | provenance, `center_output_scale`, `force_auto_tune_off` mirror fix | 0 | **next** |
| T2 — benchmark loop | fixed route, new baseline | 1 | after T1 |
| T2.5 — live-params probe | drop `ForceAutoTune` to level 2, one-drive go/no-go | 1 | after T2 |
| T3 — `LAT_ACCEL_FACTOR` | `SteerLatAccel` gate drop, value informed by T2.5 | 1–3 | after T2.5 |
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
3. **Fix the `force_auto_tune_off` mirror — defect, not polish.**
   [analyze_bolt_lateral.py:363](../tools/tuning/analyze_bolt_lateral.py) reads

   ```
   force_auto_tune_off = alt and has_auto_tune and _param_bool(log_params, "ForceAutoTuneOff", tuning_level)
   ```

   The source ([starpilot_variables.py:724](../starpilot/common/starpilot_variables.py)) has **no `has_auto_tune`
   term**. On GM `has_auto_tune` is always 0, so the analyzer's copy is pinned `False` forever. Because
   [lines 376-381](../tools/tuning/analyze_bolt_lateral.py) end in `or force_auto_tune_off`, the post-fix report
   printed `useCustomLatAccel=0 useCustomFriction=0` where the source resolves both to **1**.

   Control-path impact today is nil — both custom values are level-3-gated to the static value, so `effective=` is
   2.0 / 0.13 either way. It stops being nil the moment T3 lands: the flag would read 0 while the car ran the
   override. Same failure class as the stale level table in T2.5.
4. **Route identity.** The analyzer prints no route ID anywhere. The FLM report does
   (`00000101--2f56dfdd5a`, segment range). A multi-drive sweep needs every report to name its own route.
5. **Label the delay.** The analyzer's `steerActuatorDelay=0.2000` is `CP.steerActuatorDelay`; FLM's `SteerDelay=0.40`
   is `full_lateral_delay(...)` ([starpilot_variables.py:672](../starpilot/common/starpilot_variables.py)). Two
   similarly-named numbers differing 2× — say which one is printed.
6. **FLM state assertion (was C4 item 1, now cheap).** Print `FLMActiveProfileId`, `FLMTrialApplied`,
   `FLMActiveOverrides` — read by the controller at
   [starpilot_variables.py:725-732](../starpilot/common/starpilot_variables.py), read by the analyzer nowhere. One
   line, warn if non-empty. Full FLM awareness stays deferred; this is only the tripwire.

**Why first:** T3 is a multi-drive sweep. Without provenance, no report can tell you which commit drove which route —
that ambiguity already cost a clean answer in C5.

**Verify:** provenance line present and matching the device's `git log -1`; `center_output_scale` column populated;
route ID printed. For item 3, re-run on the post-fix route — it has `ForceAutoTuneOff=1`, so the corrected mirror must
now print `useCustomLatAccel=1 useCustomFriction=1` where it previously printed 0, with `effective=` unchanged at
2.0 / 0.13.

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
- **Applying any FLM trial profile kills the probe.** All six profiles in `flm-1786229999.json` write
  `ForceAutoTuneOff: True` and `ForceAutoTune: False` into `genericParams`, and each carries
  `requiresForceAutoTuneOff`. Do not apply an FLM profile during T2.5 — or during any drive in this plan. T1 item 6
  is the tripwire that catches it after the fact.
- T3 and T4 are **inseparable** in this drive — the estimator supplies both factor and offset. Acceptable for go/no-go,
  useless for attribution.

**Verify:** report shows `ForceAutoTune=1`, `resolved=useLiveParams=1`, and `effective=` matching
`liveTorqueFiltered` rather than `static=`. If `effective=` still matches `static=`, the gate change did not take.

## T3 — correct `LAT_ACCEL_FACTOR` (1–3 drives)

### Primary mechanism: `SteerLatAccel`, not a new constant

**A static override for `latAccelFactor` already exists.**
[starpilot_variables.py:738](../starpilot/common/starpilot_variables.py):

```
toggle.latAccelFactor = self.get_value("SteerLatAccel", cast=float, condition=advanced_lateral_tuning,
                                       default=latAccelFactor, min=latAccelFactor * 0.5, max=latAccelFactor * 1.5)
```

Clamped to `[0.5×, 1.5×]` of the platform value → **[1.0, 3.0]** with static 2.0. The ≈1.29 target sits inside it,
and the EUV's 1.0 is exactly the floor. `AdvancedLateralTune` is already 1 on this device.

It is inert for the same reason `ForceAutoTune` is: **level 3**, and this device is at level 2. That is the gate T2.5
already proposes dropping. Drop `SteerLatAccel` in the same edit and the entire T3 sweep becomes a **param write
between drives — no source edit, no rebuild.**

That property matters more than the convenience: the rebuild between C5's two drives is what reseeded
`ForceAutoTuneOff` and cost the single-variable comparison. A param-only sweep has no rebuild.

**Three-file change**, extending T2.5's two:

1. [params_keys.h](../common/params_keys.h) — `SteerLatAccel` level 3 → **2** (alongside `ForceAutoTune`).
2. [analyze_bolt_lateral.py:310](../tools/tuning/analyze_bolt_lateral.py) — mirrored table, `"SteerLatAccel": 3` → **2**.
3. T1 item 3 must already be in — otherwise `useCustomLatAccel` misreports exactly when it starts mattering.

**Caveats to hold:**

- Clamp floor is 1.0. The sweep cannot go below it. That is enough for the ≈1.29 target and for the EUV's 1.0, but
  not for anything lower.
- `SteerLatAccel` resolves from `CP.lateralTuning.torque.latAccelFactor`, so it is per-device rather than Bolt-gated
  in source. The Ioniq 6 is a different device and is untouched in practice — but this is a weaker isolation
  guarantee than the T4 constant has, so do not carry the param into a shared branch.
- `use_custom_latAccelFactor` ([line 739](../starpilot/common/starpilot_variables.py)) parses as
  `(A and B and not C) or force_auto_tune_off`. With `ForceAutoTuneOff=1` it is True regardless of the value —
  harmless while the param equals static, and the reason T1 item 3 has to land first.

### Fallback: the per-car mult

If the gate drop is rejected, add `BOLT_2022_2023_BASE_LAT_ACCEL_FACTOR_MULT` to `latcontrol_vehicle_tunes.py`,
following the established pattern and applied at the existing dispatch in
[latcontrol_torque.py:123-142](../selfdrive/controls/lib/latcontrol_torque.py) (static) and
[153-175](../selfdrive/controls/lib/latcontrol_torque.py) (live). This costs a rebuild per sweep step.

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

**Target informed by T2.5.** If the probe improved the car, set the value directly from the factor the estimator
converged on — **`SteerLatAccel` ≈ 1.29**, equivalently mult ≈0.65 — and verify in **one** drive. If the probe was
inconclusive, fall back to the blind staged sweep: **1.70 → 1.50 → 1.30** (mult 0.85 → 0.75 → 0.65), one drive each.

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

**Also read center chatter from the FLM report for the same drive.** On the post-fix baseline it is 2,926 events
(1470 mid / 1129 fast / 327 low) against 8–99 for every other finding — roughly 97% of the observed signal, and by far
the highest-n outcome available. The mid-band evidence is `desiredReversals: 0` against `outputReversals: 18`, with
`outputP2P` 0.19 versus `desiredP2P` 0.06: the plan requests a calm path and the controller reverses 18 times. That is
the integrator-led signature this plan already diagnoses (`mean|i|` 0.21–0.29), so if the correction is right,
`outputReversals` should fall toward `desiredReversals`.

Use it as a **verify metric only** — n=2,926 has far better power than the 15–52-event turn-in buckets. It is not a
new workstream; oscillation diagnosis stays parked in `update_analyzer.md`.

## T4 — `latAccelOffset` ≈ −0.33 (1–2 drives)

No static offset knob exists today — there is no `SteerLatAccelOffset` to match T3's `SteerLatAccel` — and the live
path is blocked (`TuningLevel=3` + `TuningLevelConfirmed`, which this device does not have). Add a new constant on the
per-car pattern of T3's fallback mult. This one **is** Bolt-gated by construction.

**The FLM findings independently point here, and more strongly than they point at T3.** Its 20 raw findings split by
speed band in a way no single per-side gain can satisfy:

| side | under-responsive | over-responsive |
|---|---|---|
| left | `late_turn_in` + `understeer` + `saturation` @ **fast** (→0.135–0.141) | `early_turn_in` + `oversteer` @ **mid** (→0.089–0.094) |
| right | `late_turn_in` + `understeer` @ **mid** (→0.084–0.090) | `early_turn_in` + `oversteer` @ **fast** (→0.042–0.046) |

Each side needs its gain moved in *opposite* directions depending on band, and the two sides are crossed. A scalar
gain error cannot produce that. A constant lateral offset can: it flips sign with direction, and its relative effect
scales inversely with demand magnitude. It also explains why FLM's blended recommendation nets out to a near-toothless
+0.013 / +0.011 — the conflicting findings largely cancel.

**Refit first.** Correcting the factor will move the offset estimate — re-read `TorqueEstimator fit` and
`liveTorqueFiltered` from the final T3 drive rather than reusing the −0.33 above.

**Verify:** left/right `bias` in `ControlsState tracking` converges; `Torque map residuals` `bias` (currently −0.17 to
−0.20 on both routes) moves toward zero.

## T5 — Revisit `ffScaleNeg` (1 drive)

`ffScaleNeg = 1.1449` vs `ffScalePos = 1.03` is a +11.2% right-side bias. **Hypothesis: it exists to compensate for
the uncorrected −0.33 offset.** If T4 lands, keeping both would over-correct the right side.

Test by reverting `ffScaleNeg` toward `ffScalePos` and measuring. If right-side tracking degrades, the asymmetry is
real and independent; if it improves or holds, it was a hack and should go.

**Scope is wider than `ffScaleNeg` alone.** The FLM stock-knob dump shows the left/right split is baked in at four
more places, every one calibrated on top of factor = 2.0:

| knob | left | right |
|---|---|---|
| `ff_gain` | 0.110 | 0.060 |
| `turn_in_boost` | 0.18 | 0.13 |
| `turn_in_threshold_reduction` | 0.16 | 0.12 |
| `unwind_taper` | 0.38 | 0.40 |
| siglin map | `[2.653, 1.1, 0.192, 0]` | `[2.703, 1.0, 0.147, 0]` |

Do not sweep all of these. Start with `ffScaleNeg` as written, and treat the table as the list to re-examine if
left/right asymmetry survives T4 — the near-2× `ff_gain` split is the next most likely compensator.

**Verify:** right-side `mae` and `bias` no worse than the T4 result; turn-in event asymmetry unchanged or improved.

---

## Division of labor — analyzer vs FLM

Settled once here so it is not re-argued each drive.

**`analyze_bolt_lateral.py` stays the instrument of record.** FLM structurally cannot see what T3 and T4 change: its
entire tunable surface is the 31 suffixes at
[latcontrol_vehicle_tunes.py:2899](../selfdrive/controls/lib/latcontrol_vehicle_tunes.py) — ff-gain, taper, deadband,
threshold, angle-assist and curvy trims. No `lat_accel_factor`, no `lat_accel_offset`. The four-source table, the
`TorqueEstimator` fit, `static=` vs `effective=` resolution, and the `mean|i|` / `|f|` split exist only in the
analyzer. Every T2–T5 drive gets an analyzer report.

**Read the FLM report alongside it** — it is generated per route at no cost, and it is the better source for center
chatter, speed-band × direction symptom classification, route ID and segment ranges, and current FLM param state.

**Do not duplicate that in the analyzer.** Symptom classification and chatter detection are covered; building them a
second time is wasted effort. What the analyzer still must do itself is self-describe — its own commit and route ID
(T1 items 1 and 4) — because a report you cannot attribute is precisely the C5 failure.

**FLM findings are data; FLM profiles are not instructions.** Applying any profile writes `ForceAutoTuneOff: True` and
moves `ff_gain_left/right` and the `gm` friction curve out from under the sweep. Read the findings, ignore the
profiles, for the duration of this plan.

## Why not the fallback levers

`gm_bolt_2022_2023.ff_gain_right`, `turn_in_boost_right` and `unwind_taper_right` are FLM-live at this device's tuning
level ([latcontrol_vehicle_tunes.py:3172-3182](../selfdrive/controls/lib/latcontrol_vehicle_tunes.py)) and therefore
tempting as a first move — no source edit, no rebuild.

Skip them. They would paper over a wrong base value, and every one of them would need redoing after T3 anyway. T2.5
achieves the same "test before committing" goal without that problem: it probes the *actual* corrected base rather
than an additive, region-limited approximation of it.

**FLM already ran this experiment, and the result is the argument.** On the post-fix route it independently chose
`baseline_fix` over `cleanup_pass` — *"broadly wrong across enough bands"* — which is agreement with this plan's
diagnosis from a completely separate pipeline. Its prescription was to raise `ff_gain_left` 0.110 → 0.123 and
`ff_gain_right` 0.060 → 0.071: the **same direction** as lowering `latAccelFactor`, at ~+11% / +18% against the ~+55%
the factor correction implies.

That is not caution. `latAccelFactor` and `latAccelOffset` do not exist anywhere in FLM's 31-knob surface, so a
bounded per-side ff trim is the largest move it can express. FLM is climbing toward the right fix with a lever that
cannot reach — which is exactly what using these knobs as a first move would buy.

## Out of scope

- The unwind fix itself — settled in `update_controller.md`, do not revisit mid-sweep.
- Full FLM awareness in the analyzer (C4 item 1). Now **confirmed** rather than assumed for the post-fix route:
  `flm-1786229999.json` records `FLMTrialApplied: false` and `FLMActiveOverrides: {}`, so no FLM override was in the
  control path. Note the stale `FLMActiveProfileId: "flm-1784682467:baseline_fix:recommended"` pointing at an older
  report with nothing applied. T1 item 6 adds the one-line tripwire; the full parse stays out of scope.
- The Chunk 5b `medFF/medFric/n` table field-width bug.
- Oscillation diagnosis, parked in `update_analyzer.md`.
- Friction (0.13) — measured correct, leave it.
- The Ioniq 6. Every change here is Bolt-gated by construction.
