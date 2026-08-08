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

- **Chunks 1 and 2 are done.** Both ran against the single already-recorded route; no second drive was needed or done.
- **The `unwind_detected` sign asymmetry is confirmed as real**, with a mechanism that accounts for both halves of the reported symptom. See Chunk 2's result section.
- **Chunk 1 falsified the plan's own premise** that Force Auto-Tune was active: the controller ran the static tune the whole time. Two separate reasons, either sufficient — GM is absent from torqued's `ALLOWED_CARS`, and `ForceAutoTune` requires TuningLevel 3 while the device sits at 2.
- **Two UI defects surfaced along the way** and are tracked outside this plan: the mici and galaxy UIs display settings the backend refuses to read (no tuning-level filtering), and neither has the Tuning Level selector that only the legacy Qt panel provides.
- **Still unexplained:** the oscillation at one particular rightward curvature. Chunk 4's target.
- **Known measurement quirk:** `TorqueEstimator fit` is not reproducible run-to-run — `get_points` subsamples via `np.random.choice` with no seed ([helpers.py:101](../selfdrive/locationd/helpers.py)). Two runs over the identical route gave 1.0936 and 1.1158. Harmless now; it will masquerade as a real difference once Chunk 6 compares routes. Seed or average before then.

# Chunk ordering

Six chunks, each independently runnable and verifiable against the route already recorded. Ordered so the cheapest hypothesis-killing work lands first. **Superseded after Chunk 2 — see "Decision point resolved" below.**

**Decision point after Chunk 2.** If the `unwind_detected` table comes back starkly asymmetric, the diagnosis is essentially done and the right next move may be to stop and address the controller rather than build Chunks 3–5. If it comes back symmetric, the hypothesis is dead, the P-gain/delay explanation moves to the front, and Chunks 3–4 become the priority. Re-decide there rather than committing now.

---

## Chunk 1 — Sample capture + effective tune reporting — **DONE (2026-08-07)**

Implemented and verified against the ON route; task list and review history in `update_analyzer_chunk1_task.md`.

### What Chunk 1 established

The effective tune is the **static** tune: `latAccelFactor=2.0 latAccelOffset=0.0 friction=0.130`, friction amplitude 0.260. Live values were computed by `torqued` and logged, but never reached the controller. This falsifies Context item 4 above and moots the second-drive prediction below.

Three consequences that reframe the remaining chunks — all hypotheses to test, not conclusions:

1. **An uncorrected feedforward bias.** Two independent estimators on this route put the plant offset near −0.33 (`liveTorqueFiltered` −0.3182, `TorqueEstimator fit` −0.3411). The controller applied 0.0, leaving ff ~0.34 m/s² rightward of what the plant wants — the direction of the reported slam.
2. **The integrator is absorbing it.** `|i| ≈ 0.19–0.22` in every steady-state bucket, against a steady-state MAE of 0.09. That is feedback doing feedforward's job — and it raises the stakes on the `unwind_detected` freeze, which releases exactly that compensation.
3. **Friction is over-applied.** Controller uses `0.130 × 2.0 = 0.260`; the estimator infers `0.1537 × 1.0936 ≈ 0.168`, i.e. ~1.55×. Over-applied friction near center is the standard chatter mechanism — Chunk 4's target.

The `unwind_detected` hypothesis is untouched by any of this: it derives from `desiredLateralAccel` alone and involves no tune value.

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
| 3 — direction/speed splits | **superseded, drop** | Its purpose was the direction split on transients. The unwind table already delivered the split that mattered, with a cleaner phase definition than the jerk-sign buckets would have given. |
| 4 — oscillation + 2D band table | **keep, independent value** | Nothing found so far explains the oscillation at one particular curvature. That remains the friction over-application (0.260 applied against ~0.168 inferred), and Chunk 4 is the only thing that can localise it. |
| 5 — turn-in event analysis | **keep, re-purposed** | No longer exploration. Peak `|error|`, time `at_limit`, and peak opposite-sign error in the following 3 s are exactly the before/after metrics for validating a controller fix. Build it *before* changing the controller so the yardstick exists first. |
| 6 — N-route side-by-side | **keep, needed for the fix** | Becomes the comparison harness for pre-fix vs post-fix drives rather than ON vs OFF toggle drives. |

**Suggested order:** 5 → 6 → controller fix → re-drive → compare. Chunk 4 any time; it is independent.

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

Before the re-drive, also settle whether the ~0.34 m/s² uncorrected ff bias should be addressed separately — it is an independent contributor to the same symptom and will still be there after the sign fix.

---

## Chunk 3 — Direction and speed splits, real at-limit column — **SUPERSEDED, do not build**

Self-contained rework of the `masks` tuple in `summarize_control_samples`.

- Replace the lumped transient buckets with `turn_in_left`, `turn_in_right`, `unwind_left`, `unwind_right` (same jerk/lat-accel criteria plus `desired >= 0.1` / `<= -0.1`).
- Replace the single `v < 14.0` with bands `< 6`, `6-10`, `10-14` — effective P gain rises from ~6.9 to ~16.6 across 8→5 m/s (`KP_INTERP` × the `low_speed_factor` amplifier at [latcontrol_torque.py:1986-1989](../selfdrive/controls/lib/latcontrol_torque.py)).
- Drop `& (~saturated)` from `transition_base`; add an `at_limit` fraction column using `abs(torque_cmd) >= 0.99` (`steer_max = 1.0`, [latcontrol.py:17](../selfdrive/controls/lib/latcontrol.py)). Keep `saturated` as a separate column labeled ">10 m/s only".

**Verify:** transient bucket `n` counts rise slightly vs the current report (saturated samples no longer excluded); steady-state buckets unchanged. `at_limit` should be non-zero in the low-speed right buckets if the slam is real.

## Chunk 4 — Oscillation detection and the 2D band table

The largest chunk, and the one that answers "a specific curvature to the right."

- Segment into contiguous `latActive` runs (break on inactive, `steeringPressed`, or monotime gap).
- On the tracking error `desired - actual`: detrend with a ~1 s moving mean, count sign changes in a sliding ~2 s window, require a minimum peak-to-peak amplitude so sensor noise isn't counted.
- Dominant frequency per window via `np.fft.rfft` — numpy only, no new dependency.
- 2D table: rows = `|desired lat accel|` bins (`0–0.2, 0.2–0.4, 0.4–0.7, 0.7–1.1, 1.1–1.6, 1.6+`), columns = speed bins (`<6, 6–10, 10–14, 14–20, 20+`), printed **twice — once left, once right**. Each cell: oscillating-sample fraction, median dominant Hz, and sample count so thin cells are visibly untrustworthy.

Build the detector and the table in that order; the detector is testable on its own before any table rendering exists.

**Verify:** near-zero prevalence in `center` and high-speed cells (which drive fine), non-zero in a right-side low-speed cell. If everything lights up, the amplitude threshold is too low.

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

No changes to `latcontrol_torque.py`, the cereal schema, or any tune values. This is diagnosis only — the `unwind_detected` sign asymmetry is a suspected defect, but confirming it with data comes before touching the controller.
