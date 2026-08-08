# Extend `analyze_bolt_lateral.py` for low-speed transient diagnosis

## Context

The current report says steady-state lateral tracking on the 2022/2023 Bolt is good (MAE ~0.09 m/s², center 0.052) but low-speed transients are 3–6× worse. The driver reports a specific, reproducible symptom the report cannot explain:

- A right 90° turn: wheel **slams right**, requiring intervention.
- Followed by an **overcorrection to the left**, requiring a second intervention.
- At **one particular rightward curvature**, the wheel **oscillates**.

The report weakly corroborates this — `unwind` is the only bucket with a positive bias (`+0.0512`, i.e. leftward overshoot) and the worst MAE (`0.3144`) — but cannot localize or diagnose it, for four structural reasons:

1. **No direction split on transients.** `turn_in`/`unwind` use `desired * jerk`, which is direction-agnostic ([analyze_bolt_lateral.py:86-87](../tools/tuning/analyze_bolt_lateral.py)). A right-only problem is averaged with lefts.
2. **MAE cannot see oscillation.** A limit cycle and a steady offset produce identical MAE. There is no zero-crossing or frequency metric in the script.
3. **`saturated` is the wrong at-limit signal.** `_check_saturation` requires `CS.vEgo > 10.0` ([latcontrol.py:28](../selfdrive/controls/lib/latcontrol.py)), so it is *always False* below 10 m/s — exactly where the slam happens.
4. ~~**The reported tune is not the tune that ran.**~~ **FALSIFIED by Chunk 1 (2026-08-07).** The original claim was that Force Auto-Tune On made the controller use live values (friction 0.137, `latAccelOffset` −0.318). It did not. The static tune *was* the effective tune — `latAccelFactor=2.0 latAccelOffset=0.0 friction=0.130`, friction amplitude 0.260 — for two independent reasons: `torqued` sets `useParams` from the car brand at construction and GM is not in `ALLOWED_CARS` ([torqued.py:77](../selfdrive/locationd/torqued.py)), and the `ForceAutoTune` param requires TuningLevel 3 while the device sits at 2, so `get_value` never reads it ([starpilot_variables.py:427](../starpilot/common/starpilot_variables.py)). The report was not misreporting the tune; the tune really was static. See "What Chunk 1 established" below.

The leading hypothesis comes from reading the controller, not the report. In [latcontrol_torque.py:1977](../selfdrive/controls/lib/latcontrol_torque.py):

```python
unwind_detected = (desired_lateral_accel_rate < UNWIND_D_DES_THRESHOLD and   # -1.0
                   abs(setpoint) < UNWIND_LAT_ACCEL_NEAR_ZERO)               # 0.3
```

The rate is **signed**, not absolute. Entering a right turn the setpoint falls (0 → −2), so the rate is negative and `unwind_detected` fires — freezing the integrator at right turn-*in*. Exiting a right turn the setpoint rises (−2 → 0), so it never fires — leaving the integrator live during right turn-*exit*. Left turns get the intended behavior on both. That inversion maps onto the reported symptom precisely.

**Intended outcome:** a report that splits transients by direction, reconstructs `unwind_detected` offline, locates the oscillation band in curvature × speed, and states the *effective* tune so results are interpretable.

Decisions taken with the user: extend the existing script in place; accept N routes side-by-side; locate oscillation via a 2D table plus dominant frequency. All work is in `tools/tuning/analyze_bolt_lateral.py` unless noted.

## Key enabler: no schema change, no re-drive required

`LateralTorqueState` ([log.capnp:917](../cereal/log.capnp)) does **not** contain `unwindDetected`. It does not need to. The controller derives it entirely from `setpoint`, which *is* logged as `desiredLateralAccel`, at a fixed `DT_CTRL = 0.01` ([controlsd.py:105](../selfdrive/controls/controlsd.py)):

```
rate = (desiredLateralAccel[k] - desiredLateralAccel[k-1]) / 0.01
unwind_detected = rate < -1.0 and abs(desiredLateralAccel[k]) < 0.3
```

Likewise the toggle state is recoverable: `initData.params` is a `Map(Text, Data)` ([log.capnp:177](../cereal/log.capnp)) and loggerd writes **all** param values except those flagged `DONT_LOG` ([logger.cc:57-66](../system/loggerd/logger.cc)). `ForceAutoTune` / `ForceAutoTuneOff` carry only `PERSISTENT` ([params_keys.h:295](../common/params_keys.h)), so their values are in the log.

---

# Status at a glance (2026-08-07)

- **Chunks 1, 1b and 2 are done.** All ran against the single already-recorded route; no second drive was needed or done.
- **The `unwind_detected` sign asymmetry is confirmed as real**, with a mechanism that accounts for both halves of the reported symptom. See Chunk 2's result section.
- **Chunk 1 falsified the plan's own premise** that Force Auto-Tune was active: the controller ran the static tune the whole time. Two separate reasons, either sufficient — GM is absent from torqued's `ALLOWED_CARS`, and `ForceAutoTune` requires TuningLevel 3 while the device sits at 2.
- **Two UI defects surfaced along the way** and are tracked outside this plan: the mici and galaxy UIs display settings the backend refuses to read (no tuning-level filtering), and neither has the Tuning Level selector that only the legacy Qt panel provides.
- ~~**Chunk 1 is incomplete.**~~ **Closed by Chunk 1b (2026-08-07).** The effective-tune section now reports the four repurposed tune fields under their real names and states the ~11% right-biased asymmetric ff gain explicitly — a *third* independent right-turn contributor, confirmed at 1.03 left vs 1.1449 right. See "What Chunk 1 missed" below for the mechanism.
- **Still unexplained:** the oscillation at one particular rightward curvature. Chunk 4's target — but the hypothesis has changed; see the re-scoped Chunk 4.
- ~~**Known measurement quirk:**~~ **RESOLVED by Chunk 1b (2026-08-07).** `TorqueEstimator fit` was not reproducible run-to-run — `get_points` subsamples via `np.random.choice` with no seed ([helpers.py:101](../selfdrive/locationd/helpers.py)), and two runs over the identical route gave 1.0936 and 1.1158. `main` now seeds the global RNG; two consecutive runs are byte-identical. The canonical seeded fit is **`latAccelFactor=1.1087 latAccelOffset=-0.3454 friction=0.1623`** (bucket_points 11522), and that line is admissible in every verification from here on.

# Chunk ordering

Originally six chunks (1b added later), each independently runnable and verifiable against the route already recorded. Ordered so the cheapest hypothesis-killing work lands first. **Superseded after Chunk 2 — see "Decision point resolved" below.** Order was **1b → 4 → 5 → 6 → controller fix → re-drive → compare**; with 1b done, **Chunk 4 is next**.

**Decision point after Chunk 2.** If the `unwind_detected` table comes back starkly asymmetric, the diagnosis is essentially done and the right next move may be to stop and address the controller rather than build Chunks 3–5. If it comes back symmetric, the hypothesis is dead, the P-gain/delay explanation moves to the front, and Chunks 3–4 become the priority. Re-decide there rather than committing now.

---

## Chunk 1 — Sample capture + effective tune reporting — **DONE (2026-08-07)**

Implemented and verified against the ON route; task list and review history in `update_analyzer_chunk1_task.md`.

### What Chunk 1 established

The effective tune is the **static** tune: `latAccelFactor=2.0 latAccelOffset=0.0 friction=0.130`, friction amplitude 0.260. Live values were computed by `torqued` and logged, but never reached the controller. This falsifies Context item 4 above and moots the second-drive prediction below.

Three consequences that reframe the remaining chunks — all hypotheses to test, not conclusions:

1. **An uncorrected feedforward bias.** Two independent estimators on this route put the plant offset near −0.33 (`liveTorqueFiltered` −0.3182, `TorqueEstimator fit` −0.3454 seeded; −0.3411 in the original unseeded draw). The controller applied 0.0, leaving ff ~0.34 m/s² rightward of what the plant wants — the direction of the reported slam.
2. **The integrator is absorbing it.** `|i| ≈ 0.19–0.22` in every steady-state bucket, against a steady-state MAE of 0.09. That is feedback doing feedforward's job — and it raises the stakes on the `unwind_detected` freeze, which releases exactly that compensation.
3. **Friction is over-applied.** Controller uses `0.130 × 2.0 = 0.260`; the estimator infers `0.1623 × 1.1087 ≈ 0.180`, i.e. **~1.44×** (was quoted as ~1.55× off the unseeded draw). Over-applied friction near center is the standard chatter mechanism — retained as the *fallback* explanation for the oscillation, not the leading one; see the re-scoped Chunk 4.
4. **The feedforward carries an ~11% right-biased asymmetric gain** that Chunk 1 never reported. See below.

The `unwind_detected` hypothesis is untouched by any of this: it derives from `desiredLateralAccel` alone and involves no tune value.

### What Chunk 1 missed (2026-08-07)

Chunk 1's goal was to state the effective tune "so results are interpretable." It reports `latAccelFactor` / `latAccelOffset` / `friction`, which is incomplete for a Bolt — and it *mislabels* four fields.

On Bolts, four torque-tune fields are repurposed at construction ([latcontrol_torque.py:1909-1917](../selfdrive/controls/lib/latcontrol_torque.py)):

| tune field | actual meaning | ACC 2022/2023 value |
|---|---|---|
| `kpDEPRECATED` | `torque_ff_scale_pos` — ff multiplier when `ff >= 0` (left) | 1.03 |
| `kiDEPRECATED` | `torque_ff_scale_neg` — ff multiplier when `ff < 0` (right) | 1.07 × 1.07 = **1.1449** |
| `kdDEPRECATED` | `torque_ki_mult` — scales `pid._k_i` | 0.93 × 0.93 = 0.8649 |
| `kfDEPRECATED` | `torque_deadzone_boost` — additive near-center ff | 0.02 × 0.75 = 0.015 |

Values from [interface.py:444-458](../opendbc_repo/opendbc/car/gm/interface.py).

Three consequences:

- **An asymmetric feedforward gain, right-biased by ~11%**, applied via `np.interp(ff, [-0.05, 0, 0.05], [neg, 1.0, pos])` ([latcontrol_torque.py:1997-1999](../selfdrive/controls/lib/latcontrol_torque.py)) — effectively a step at `ff = 0`. This is a **third** independent right-turn contributor, alongside the `unwind_detected` sign error (Chunk 2) and the uncorrected ~0.34 m/s² ff bias (consequence 1 above).
- **Ki is 13.5% below nominal**, while consequence 2 above established the integrator is already doing feedforward's job (`|i| ≈ 0.19–0.22` against steady-state MAE 0.09). Less authority where more is already being demanded.
- The `staticTune=` line in the analyzer **actively mislabels** all four as `kp/ki/kd/kf`, so a reader would take them for PID gains.

None of this is resolved through the live/static toggle path — these come straight off `car_params.lateralTuning.torque` and were in force regardless of the Chunk 1 toggle finding.

### Original scope

- Extend `ControlSample` with `mono_time`, `d_term` (`torqueState.d`, logged at [log.capnp:923](../cereal/log.capnp) but currently dropped — D matters for oscillation), and `curvature` (`desired_la / v_ego**2`, guarded at low `v_ego`).
- Handle `initData` in the message loop to capture the params map.
- New section printing static / live-filtered / **effective** triple, resolving the toggles by mirroring the gating in [starpilot_variables.py:634-635](../starpilot/common/starpilot_variables.py) (`has_auto_tune = LTP.useParams`) and the resolution in `get_torque_control_params` ([controlsd.py:50-71](../selfdrive/controls/controlsd.py)). Also print derived friction amplitude `friction × latAccelFactor` — the only path by which `latAccelFactor` reaches this car, via `get_friction` ([lateral.py:167](../opendbc_repo/opendbc/car/lateral.py)); the Bolt's `torque_from_lateral_accel` is a siglin `np.interp` that ignores `torque_params` entirely.
- Warn on one line when static and effective differ. Relabel the existing line `staticTune=`.

**Verify:** re-run on the ON route. All existing sections byte-identical. New section reports `ForceAutoTune=1`, `ForceAutoTuneOff=0`, effective `1.2916 / -0.3182 / 0.1370` matching the `liveTorqueFiltered` line already in the report — and finally settles whether `AdvancedLateralTune` was on, the precondition gating the whole toggle.

## Chunk 2 — `unwind_detected` reconstruction (the headline test) — **DONE (2026-08-07)**

Implemented and verified against the ON route; task list and review history in `update_analyzer_chunk2_task.md`.

### Result: hypothesis CONFIRMED

```
Unwind reconstruction:
  active_samples=213342 unwind_frac=0.0259 unclassified=147347
  phase            n      unwind_frac  mean|i|     bias    mean|d_des|
  entering_left    18468       0.0000   0.2015  -0.0975        0.9958
  entering_right   12425       0.0777   0.1639  +0.0358        1.0452
  exiting_left     21321       0.0579   0.2060  -0.0338        0.8556
  exiting_right    13781       0.0000   0.1743  -0.0359        0.9612
  ff offset check: n=  857 mean_f=+0.1032
  verdict: consistent with static tune
```

Left turns get the intended behaviour; right turns get it exactly inverted. Sanity gates all passed (`unwind_frac` 2.59%, `mean|d_des|` ~0.9–1.05 sitting on the −1.0 threshold, `unclassified` 69%), and `ControlsState tracking` was byte-identical to the Chunk 1 run.

**How much the zeros are worth.** They are tautological, not evidence: `unwind_detected` requires `rate < -1.0`, and `entering_left` / `exiting_right` are *defined* by `rate > 0`, so they can never fire. The classification and the condition share the rate sign. The content is in the two phases that *can* fire — and of those, the controller treats **right turn-entry** as an unwind: 7.77% of 12,425 samples ≈ 965 samples ≈ **9.6 s of frozen integrator during right turn-ins** on this route.

Two corroborations in the same table:

- `mean|i|` is suppressed on both right phases (0.164 / 0.174) against both left (0.202 / 0.206) — ~18% less integrator, which is what a one-sided freeze looks like.
- `entering_right` is the only phase with **positive** bias (+0.0358); every other phase is negative. Positive means actual is left of desired — under-turning into the right turn, the lag expected while the integrator is held.

### Mechanism, end to end

`unwind_detected` also requires `|setpoint| < 0.3`, so on right entry the freeze applies only while desired lat accel is still small — the opening moments of the turn. Error accumulates un-integrated. Once `|setpoint|` crosses 0.3 the freeze releases and the integrator catches up against both the accumulated error and the ~0.34 m/s² uncorrected rightward ff bias from Chunk 1. **That is the slam.** The overshoot drives the left correction.

The other half falls out of the same sign error: `exiting_right` never freezes, so the integrator stays live through right turn-exit — which is why the original report's `unwind` bucket was the worst (`mae=0.3144`) and the only one with positive bias (`+0.0512`, leftward overshoot).

One sign error produces both reported symptoms.

### Also settled

`ff offset check` = +0.1032 (n=857), three times closer to the static prediction (0.00) than the live one (+0.33); residual is the friction and deadzone-boost terms not fully cancelling. Chunk 1's conclusion now rests on the controller's own feedforward arithmetic, independent of any param reasoning.

### Original scope

Depends on Chunk 1 for `mono_time`.

- Reconstruct per the formula above. Guard: only evaluate when the previous sample was also `latActive` and the monotime gap is within ~1.5×`DT_CTRL`. Mirror the controller's reset of `prev_desired_lateral_accel = 0.0` on the inactive branch.
- Classify every active sample into `entering_left` / `exiting_left` / `entering_right` / `exiting_right` by sign of `setpoint` and sign of `rate`.
- Per phase report: fraction with `unwind_detected` True, mean `|i|`, mean tracking bias.

**Verify:** `unwind_detected` should be True on a small single-digit percentage of active samples overall — if ~0% or >50%, the rate reconstruction or gap guard is wrong. Then read the table: the hypothesis predicts heavy firing on `entering_right`, near-zero on `exiting_right`, reversed on the left.

**→ Decision point. Re-assess before continuing.**

---

# Decision point resolved (2026-08-07)

The table came back starkly asymmetric, which was the "diagnosis essentially done" branch. The remaining chunks are re-scoped accordingly. **Nothing below has been built yet.**

| chunk | status | why |
|---|---|---|
| 1b — complete the effective-tune reporting | **DONE (2026-08-07)** | Chunk 1 mislabelled four tune fields and omitted the ~11% right-biased ff asymmetry. Cheap (reporting only), and it changed how every existing number in the report is read. |
| 3 — direction/speed splits | **superseded, drop** | Its purpose was the direction split on transients. The unwind table already delivered the split that mattered, with a cleaner phase definition than the jerk-sign buckets would have given. |
| 4 — oscillation + 2D band table | **keep, re-scoped** | Still the only thing that can localise the oscillation, but the hypothesis has changed from static friction to the Bolt dynamic gain layer. See the rewritten Chunk 4. |
| 5 — turn-in event analysis | **keep, re-purposed** | No longer exploration. Peak `|error|`, time `at_limit`, and peak opposite-sign error in the following 3 s are exactly the before/after metrics for validating a controller fix. Build it *before* changing the controller so the yardstick exists first. |
| 6 — N-route side-by-side | **keep, needed for the fix** | Becomes the comparison harness for pre-fix vs post-fix drives rather than ON vs OFF toggle drives. |

**Order:** 1b → 4 → 5 → 6 → controller fix → re-drive → compare.

## The indicated controller fix — out of scope here, needs its own review

The condition tests the *signed* rate when it means to test whether magnitude is decreasing:

```python
# current — latcontrol_torque.py:1977
unwind_detected = (desired_lateral_accel_rate < UNWIND_D_DES_THRESHOLD and
                   abs(setpoint) < UNWIND_LAT_ACCEL_NEAR_ZERO)

# indicated
unwind_detected = ((abs(setpoint) - abs(self.prev_desired_lateral_accel)) / self.dt < UNWIND_D_DES_THRESHOLD and
                   abs(setpoint) < UNWIND_LAT_ACCEL_NEAR_ZERO)
```

This is safety-relevant lateral behaviour and remains **out of scope for this plan**, which is diagnosis only. It needs its own review, and a before/after drive on the same roads. Note the fix changes behaviour on *both* sides: it stops freezing at right turn-in (removing the slam mechanism) and starts freezing at right turn-exit (which the controller has never done on this car), so the post-fix drive is testing two changes at once.

Two further notes on the fix:

- **It does not eliminate every instance of the reported entry condition.** Entering a right turn *from a left-hand curve* (setpoint +0.5 → −2) passes through a stretch where `|setpoint|` is genuinely decreasing while still `< 0.3`, so the magnitude-rate form fires there too. That is semantically correct — it really is unwinding the left turn — but a right entry reached that way will still see a frozen integrator. Only right entries from straight are fully fixed.
- **The correct classifier already exists in the same file.** `_bolt_2022_2023_transition_phase` ([latcontrol_torque.py:960](../selfdrive/controls/lib/latcontrol_torque.py)) separates turn-in from unwind via `tanh(la × jerk / scale)` — the direction-agnostic product form, which is right. The controller has a correct unwind classifier roughly a thousand lines from the broken one, feeding a different consumer.

Before the re-drive, also settle whether the ~0.34 m/s² uncorrected ff bias should be addressed separately — it is an independent contributor to the same symptom and will still be there after the sign fix. The same question applies to the ~11% right-biased ff asymmetry from Chunk 1b: three contributors now push the same direction, and a fix to one alone may not be measurable against the other two.

---

## Chunk 1b — Complete the effective-tune reporting — **DONE (2026-08-07)**

Implemented and verified against the ON route in a single pass; task list and review history in `update_analyzer_chunk1b_task.md`.

### Result

The report now states the Bolt feedforward layer under its real names:

```
staticTune=... ffScalePos=1.0300 ffScaleNeg=1.1449 kiMult=0.8649 deadzoneBoost=0.0150
  bolt: ffAsym left=x1.0300 right=x1.1449 (+11.2% right), blended over ff in [-0.0500,+0.0500]
        kiMult=0.8649 (applied to pid._k_i)  deadzoneBoost=0.0150 (reach |latAccel|<0.15, unscaled additive)
```

All four values match the prediction from [interface.py:444-459](../opendbc_repo/opendbc/car/gm/interface.py), read off the logged `car_params` rather than hardcoded. **The ~11% right-biased ff asymmetry is confirmed as real and in force on the recorded route** — the third independent right-turn contributor, alongside the `unwind_detected` sign error and the uncorrected ~0.34 m/s² ff bias. `kiMult` clears the controller's `> 0.0 and != 1.0` gate, so Ki really did run 13.5% below nominal.

Every pre-existing section came back unchanged from the Chunk 2 run, and two consecutive runs are byte-identical — the seed holds.

### Original scope

- Import `BOLT_CARS` from `openpilot.selfdrive.controls.lib.latcontrol_torque` rather than re-listing fingerprints — it is already `BOLT_2022_2023_CARS + BOLT_2018_2021_CARS + BOLT_2017_CARS` ([latcontrol_torque.py:107](../selfdrive/controls/lib/latcontrol_torque.py)).
- In `main`, when `car_params.carFingerprint in BOLT_CARS`, print the four fields under their real names — `ffScalePos`, `ffScaleNeg`, `kiMult`, `deadzoneBoost` — instead of `kp/ki/kd/kf`. Keep the existing generic labels on non-Bolt platforms so the script stays usable elsewhere.
- Extend the `Effective tune` section with a Bolt block stating the asymmetry explicitly, e.g.
  `ffAsym: left=×1.0300 right=×1.1449 (+11.2% right), blended over ff∈[-0.05,+0.05]`,
  plus `kiMult` and `deadzoneBoost` with its `DEADZONE_BOOST_LAT_ACCEL = 0.15` reach.
- Add this block **separately** from `resolve_effective_tune`'s static/live/effective triple — these values never pass through the live-params toggle path, so threading them through that function would misrepresent them.

Two small correctness items, folded in here:

- `curvature` is stored as `0.0` when `v <= 1.0` ([analyze_bolt_lateral.py:440](../tools/tuning/analyze_bolt_lateral.py)). Chunk 4 bins on curvature and those samples would form a fake zero-curvature pile. Store `float("nan")` and let the existing `np.isfinite` idiom exclude them.
- Seed the RNG in `main` before constructing `TorqueEstimator`, so the `TorqueEstimator fit` line becomes reproducible and can be included in every verification from here on.

**Verify:** re-run on the recorded route. `ControlsState tracking`, `Unwind reconstruction`, and `Torque map residuals` byte-identical to the Chunk 2 run. `Effective tune` gains the Bolt block reporting 1.03 / 1.1449 / 0.8649 / 0.015. `TorqueEstimator fit` now stable across two consecutive runs. — **all five verification steps passed.**

## Chunk 4 is next

Re-scoped Chunk 4 below is the only remaining diagnostic unknown: the oscillation at one particular rightward curvature. Note that Chunk 1b's result sharpens its discriminating test — the static-friction fallback is now ~1.44× over-applied rather than ~1.55×, and the `ff_scale` peak-band coincidence test is unchanged.

## Chunk 3 — Direction and speed splits, real at-limit column — **SUPERSEDED, do not build**

Self-contained rework of the `masks` tuple in `summarize_control_samples`.

- Replace the lumped transient buckets with `turn_in_left`, `turn_in_right`, `unwind_left`, `unwind_right` (same jerk/lat-accel criteria plus `desired >= 0.1` / `<= -0.1`).
- Replace the single `v < 14.0` with bands `< 6`, `6-10`, `10-14` — effective P gain rises from ~6.9 to ~16.6 across 8→5 m/s (`KP_INTERP` × the `low_speed_factor` amplifier at [latcontrol_torque.py:1986-1989](../selfdrive/controls/lib/latcontrol_torque.py)).
- Drop `& (~saturated)` from `transition_base`; add an `at_limit` fraction column using `abs(torque_cmd) >= 0.99` (`steer_max = 1.0`, [latcontrol.py:17](../selfdrive/controls/lib/latcontrol.py)). Keep `saturated` as a separate column labeled ">10 m/s only".

**Verify:** transient bucket `n` counts rise slightly vs the current report (saturated samples no longer excluded); steady-state buckets unchanged. `at_limit` should be non-zero in the low-speed right buckets if the slam is real.

## Chunk 4 — Oscillation detection and the 2D band table — **RE-SCOPED (2026-08-07)**

The largest chunk, and the one that answers "a specific curvature to the right."

### The hypothesis has changed

The original scope assumed the oscillation came from static friction over-application (0.260 applied against ~0.168 inferred). That is now the **fallback**, not the leading explanation, for one reason: static friction over-application chatters near center at *all* curvatures. The reported symptom is one *particular* rightward curvature.

There is a dynamic gain layer that does predict a specific curvature, which this plan had not previously accounted for: `get_bolt_2022_2023_ff_scale`, `get_bolt_2022_2023_friction_scale`, and `get_bolt_2022_2023_friction_threshold` ([latcontrol_torque.py:974-1022](../selfdrive/controls/lib/latcontrol_torque.py)). It is **unconditionally active** on this car — `bolt_2022_2023_tuned_path_active = self.is_bolt_2022_2023`, with no testing-ground gate, unlike the Volt/G90/EV6 paths. Its shape:

- **Curvature-banded.** `extra_scale = gain × σ((|la| − 0.12)/0.07) × σ((1.35 − |la|)/0.28)` — a gain bump that switches on near `|la| ≈ 0.12` and off near 1.35.
- **Direction-asymmetric.** `FF_GAIN_LEFT` 0.11 vs `FF_GAIN_RIGHT` 0.06; `UNWIND_TAPER` 0.38/0.40; `UNWIND_FRICTION_REDUCTION` 0.27/0.28; `TURN_IN_FRICTION_BOOST` 0.10/0.07.
- **Low-speed weighted.** `1/(1 + (v/9)²)` — strongest exactly where the symptom lives.
- **Hard-switched on phase.** `tanh(la × jerk / 0.12)` saturates almost immediately (`|la·jerk| > 0.3 ⇒ |phase| > 0.99`), so the turn-in and unwind gain sets swap near-discontinuously rather than blending.

A gain bump confined to a specific `|la|` band, direction-asymmetric, strongest below ~9 m/s, switched hard on the sign of `la × jerk`, is a much better fit for the reported symptom. Chunk 4 as originally written could not see any of it, because it binned outcomes without reconstructing the gains that produced them.

### Part A — reconstruct the dynamic gains

Import and call the real functions; do not reimplement them (same reuse discipline as the existing `NON_LINEAR_TORQUE_PARAMS` import):

```python
from openpilot.selfdrive.controls.lib.latcontrol_torque import (
  get_bolt_2022_2023_ff_scale, get_bolt_2022_2023_friction_scale,
  get_bolt_2022_2023_friction_threshold, get_friction_threshold,
)
```

They are pure functions of `(desired_lateral_accel, desired_lateral_jerk, v_ego)` — all three already on `ControlSample` as `desired_la`, `desired_jerk`, `v_ego`. **No schema change, no re-drive.** They are scalar-only, so evaluate once with a list comprehension and cache the arrays; at ~200k samples that is a few seconds, acceptable for a one-shot report.

Gate on `carFingerprint in BOLT_2022_2023_CARS` and print a skip line otherwise, so the section degrades cleanly on other platforms.

### Part B — oscillation detector

Unchanged from the original scope.

- Segment into contiguous `latActive` runs (break on inactive, `steeringPressed`, or monotime gap — reuse the same gap guard as `reconstruct_unwind`, [analyze_bolt_lateral.py:59](../tools/tuning/analyze_bolt_lateral.py)).
- On the tracking error `desired - actual`: detrend with a ~1 s moving mean, count sign changes in a sliding ~2 s window, require a minimum peak-to-peak amplitude so sensor noise isn't counted.
- Dominant frequency per window via `np.fft.rfft` — numpy only, no new dependency.

### Part C — the 2D band table

Rows = `|desired lat accel|` bins (`0–0.2, 0.2–0.4, 0.4–0.7, 0.7–1.1, 1.1–1.6, 1.6+`), columns = speed bins (`<6, 6–10, 10–14, 14–20, 20+`), printed **twice — once left, once right**. Each cell: oscillating-sample fraction, median dominant Hz, sample count so thin cells are visibly untrustworthy, **plus median `ff_scale` and median `friction_scale`** from Part A.

Build in the order A → B → C; each is testable before the next, and the detector is testable on its own before any table rendering exists.

**Verify:** near-zero prevalence in `center` and high-speed cells (which drive fine), non-zero in a right-side low-speed cell. If everything lights up, the amplitude threshold is too low.

The new discriminating test is whether the oscillating cells **coincide with the `ff_scale` peak band** — the `0.2–0.4` and `0.4–0.7` rows at `<6` / `6–10`, where the onset sigmoid has opened and the low-speed weight is still near 1. If they coincide, the dynamic gain layer is the mechanism. If `ff_scale` comes back flat near 1.0 across all cells, this hypothesis is dead and static-friction over-application returns as the explanation.

## Chunk 5 — Turn-in event analysis

Independent of Chunk 4; either can be skipped without breaking the other.

- Detect events where `|desired|` crosses 0.4 with jerk of the same sign, at `v < 14`.
- Per event: peak `|error|`, peak `|torque_cmd|`, time `at_limit`, time-to-peak, and the **peak opposite-sign error in the following 3 s** — the left overcorrection, quantified.
- Aggregate by direction; print the worst ~10 events with route-relative timestamps so specific moments can be found in the log.

**Verify:** worst events should cluster on right turns, and the opposite-sign-overshoot column should be materially larger for rights than lefts.

## Chunk 6 — N-route side-by-side

Deliberately last: it touches every section's rendering, so doing it once over final code beats refactoring repeatedly.

- `route` becomes `nargs="+"`.
- Refactor `main` so per-route parsing returns a result object; render each section as columns (one per route) with a delta against the first.

Reuse unchanged throughout: `siglin_torque`, `summarize_torque_points`, the `TorqueEstimator` wiring, the `NON_LINEAR_TORQUE_PARAMS` import.

**Verify:** `analyze_bolt_lateral.py <route>` with a single route produces output equivalent to Chunk 5's. Then `analyze_bolt_lateral.py <route_on> <route_off>` once the second drive exists.

---

## ~~A prediction worth recording before the second drive~~ — MOOT

The original prediction was that turning Force Auto-Tune **Off** would make both symptoms worse, by deleting a +0.318 m/s² leftward ff bias and raising friction amplitude 0.177 → 0.260.

**That condition was already the baseline.** The recorded route ran at `latAccelOffset=0.0` and friction amplitude 0.260. The symptoms — right-turn slam, left overcorrection, oscillation at one curvature — were all observed *under* the predicted-worse tune. There is nothing left to predict and no second drive to make it visible.

The genuinely untested condition is now the *live* one (latAccelFactor 1.2916, offset −0.3182, friction amplitude 0.177). Reaching it requires `TuningLevel=3` **and** `TuningLevelConfirmed=True`; `ForceAutoTuneOff` cannot produce it, or anything else, on a GM. Deferred — not needed for Chunks 2–5, all of which run on the route already recorded.

## Out of scope

No changes to `latcontrol_torque.py`, the cereal schema, or any tune values. This is diagnosis only — the `unwind_detected` sign asymmetry is a confirmed defect, but the controller fix gets its own review and its own before/after drive.

Note that Chunks 1b and 4 *import from* `latcontrol_torque.py` (`BOLT_CARS`, the Bolt scale functions). Reading it is in scope; editing it is not.
