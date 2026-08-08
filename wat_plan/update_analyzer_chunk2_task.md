# Chunk 2 task list — `unwind_detected` reconstruction (the headline test)

**Audience:** an implementing subagent (Gemini Flash). Follow the tasks literally and in order.
**Only file to edit:** `tools/tuning/analyze_bolt_lateral.py`. Nothing else. No schema changes, no controller changes.
**Depends on:** Chunk 1 (complete) — `ControlSample` already carries `mono_time`, `d_term`, `curvature`.

## Context

The driver of a 2022/2023 Chevy Bolt reports a reproducible symptom: a right 90° turn where the wheel slams right, followed by an overcorrection to the left, plus oscillation at one particular rightward curvature. Steady-state tracking is fine (MAE 0.09); low-speed transients are 3–6× worse.

The leading hypothesis comes from reading the controller ([latcontrol_torque.py:1976-1979](../selfdrive/controls/lib/latcontrol_torque.py)):

```python
desired_lateral_accel_rate = (setpoint - self.prev_desired_lateral_accel) / self.dt
unwind_detected = (desired_lateral_accel_rate < UNWIND_D_DES_THRESHOLD and   # -1.0
                   abs(setpoint) < UNWIND_LAT_ACCEL_NEAR_ZERO)               # 0.3
```

The rate is **signed, not absolute**. Entering a right turn the setpoint falls (0 → −2), so the rate is negative and `unwind_detected` fires — freezing the integrator at right turn-*in*. Exiting a right turn the setpoint rises (−2 → 0), so it never fires — leaving the integrator live during right turn-*exit*. Left turns get the intended behavior on both. That inversion maps onto the reported symptom precisely.

`unwind_detected` is not in the cereal schema and does not need to be: it derives entirely from `setpoint`, which is logged verbatim as `desiredLateralAccel` ([latcontrol_torque.py:2113](../selfdrive/controls/lib/latcontrol_torque.py)).

Chunk 1 raised the stakes on this test. It established that the controller ran the **static** tune with `latAccelOffset=0.0`, while two independent estimators put the plant offset near −0.33 — so feedforward sits ~0.34 m/s² rightward of what the plant wants, and the integrator carries that load (`|i| ≈ 0.19–0.22` against a steady-state MAE of 0.09). Freezing a loaded integrator matters far more than freezing an idle one.

**This chunk is the decision point of the whole plan.** If the table comes back starkly asymmetric, the diagnosis is essentially done. If it comes back symmetric, the hypothesis is dead and Chunks 3–4 become the priority. Do not build anything beyond what is specified here.

---

## Task 0 — Capture `torqueState.active`

The controller branches on `if not active:` where `active` is `CC.latActive` ([controlsd.py:237](../selfdrive/controls/controlsd.py)), and it records that same boolean into the message it logs as `pid_log.active` ([latcontrol_torque.py:1950](../selfdrive/controls/lib/latcontrol_torque.py), schema field `LateralTorqueState.active`).

`ControlSample.lat_active` currently comes from `latest["carControl"].latActive` — the most recent `carControl` seen before this `controlsState`. The log is time-sorted across message types, so at an engagement or disengagement boundary that can be one frame stale. Those boundaries are exactly where `prev_setpoint` resets to 0.0, so a one-frame slip mis-handles the reset every time.

Add a field to `ControlSample`:

```python
  torque_active: bool
```

populated in the `controlsState` branch as `torque_active=torque_state.active`. **Use `torque_active` for all Chunk 2 logic.** Leave `lat_active` in place and untouched so `summarize_control_samples` stays byte-identical.

## Task 1 — Reconstruct `unwind_detected` per sample

Add a module-level constant block mirroring the controller, with a comment naming the source. Put it at the **top of the file**, next to `LOW_ROLL_THRESHOLD_DEG`, and define both Chunk 2 helper functions **above `main`** — the current placement after `main` runs correctly but is the only place in the file that breaks that convention:

```python
# mirrors latcontrol_torque.py:47-48 and controlsd.py:105
DT_CTRL = 0.01
UNWIND_D_DES_THRESHOLD = -1.0
UNWIND_LAT_ACCEL_NEAR_ZERO = 0.3
```

Write a function that walks `samples` in order and returns two parallel numpy arrays, `unwind` (bool) and `d_des_rate` (float), both the same length as `samples`.

Rules, in this order per sample `k`:

1. **Inactive resets state.** If `not samples[k].torque_active`, set `prev_setpoint = 0.0`, mark `unwind[k] = False`, `d_des_rate[k] = nan`, and continue. This mirrors the controller's inactive branch ([latcontrol_torque.py:1957](../selfdrive/controls/lib/latcontrol_torque.py)), which resets `prev_desired_lateral_accel = 0.0`.
2. **Gap guard.** If `k == 0`, or the previous sample was not `torque_active`, or `(samples[k].mono_time - samples[k-1].mono_time) / 1e9 > 1.5 * DT_CTRL`, then the previous setpoint is not trustworthy: mark `unwind[k] = False`, `d_des_rate[k] = nan`, but **still** set `prev_setpoint = samples[k].desired_la` so the next sample can be evaluated.
3. **Otherwise** compute exactly as the controller does — with the **fixed** `DT_CTRL`, not the measured gap, because the controller uses its fixed `self.dt`:

```python
rate = (s.desired_la - prev_setpoint) / DT_CTRL
unwind[k] = (rate < UNWIND_D_DES_THRESHOLD) and (abs(s.desired_la) < UNWIND_LAT_ACCEL_NEAR_ZERO)
prev_setpoint = s.desired_la
```

Note `prev_setpoint` is updated on **every** active sample regardless of the guard, matching the controller's unconditional `self.prev_desired_lateral_accel = setpoint`.

## Task 2 — Classify every active sample into a turn phase

Four phases from the sign of the setpoint and the sign of the rate. Use a deadband so near-center samples don't get classified from noise:

```
PHASE_LAT_ACCEL_DEADBAND = 0.1   # m/s^2
PHASE_RATE_DEADBAND = 0.2        # m/s^3
```

For each sample with `torque_active`, `not steering_pressed`, finite `d_des_rate`, `abs(desired_la) >= PHASE_LAT_ACCEL_DEADBAND`, and `abs(d_des_rate) >= PHASE_RATE_DEADBAND`:

| phase | condition |
|---|---|
| `entering_left` | `desired_la > 0` and `d_des_rate > 0` (magnitude growing) |
| `exiting_left` | `desired_la > 0` and `d_des_rate < 0` (magnitude shrinking) |
| `entering_right` | `desired_la < 0` and `d_des_rate < 0` |
| `exiting_right` | `desired_la < 0` and `d_des_rate > 0` |

Samples failing any precondition belong to no phase. Do not invent a fifth bucket for them; just report the count as `unclassified`.

## Task 3 — Print the phase table

New section, after `ControlsState tracking:` and before the `TorqueEstimator fit` line. Match the file's existing style (2-space indent, f-strings, `:.4f`), and start the header with `\n` as every other section does.

```
Unwind reconstruction:
  active_samples=NNNNN unwind_frac=0.0XXX unclassified=NNNNN
  phase            n      unwind_frac  mean|i|   bias      mean|d_des|
  entering_left    NNNNN  0.XXXX       0.XXXX    +0.XXXX   XX.XXXX
  entering_right   NNNNN  ...
  exiting_left     NNNNN  ...
  exiting_right    NNNNN  ...
```

- `unwind_frac` — fraction of that phase's samples with `unwind[k]` True.
- `mean|i|` — `np.mean(np.abs(i_term[mask]))`.
- `bias` — `np.mean(actual[mask] - desired[mask])`, the same convention the existing `ControlsState tracking` section uses, where positive is leftward.
- `mean|d_des|` — `np.mean(np.abs(d_des_rate[mask]))`, useful for spotting a units or `dt` error at a glance.
- The `active_samples` / `unwind_frac` line is over all `torque_active` samples, not just classified ones.
- Print phases in the order above so left/right pairs sit adjacent for reading.
- **A phase with `n == 0` must print `--` in every numeric column, never `0.0000`.** This is the table a go/no-go decision hangs on, and `entering_right 0 0.0000` reads as "never fires" when it means "no data" — the exact value that would wrongly kill the hypothesis. Same rule for the `mean_f` value in Task 4 when its bucket is empty.
- Align the numeric columns with the header row.

## Task 4 — Settle static-vs-live from the controller's own arithmetic

Chunk 1 concluded the static tune was effective by reconstructing param gating. This task confirms it from the logged feedforward instead, depending on no param, toggle, or tuning level.

`torqueState.f` is the feedforward in **lateral-acceleration units** — `self.f = feedforward` with no `k_f` ([common/pid.py:50](../common/pid.py)). Under live params the controller would compute `ff -= latAccelOffset` ([latcontrol_torque.py:1995](../selfdrive/controls/lib/latcontrol_torque.py)), adding a flat **+0.3182 m/s²** to every sample, scaled by `ff_scale ≈ 1.03` (that is `kpDEPRECATED`, fixed at init and unaffected by live params, [latcontrol_torque.py:1913](../selfdrive/controls/lib/latcontrol_torque.py)).

Isolate samples where every other ff contribution is near zero, so `f` reflects the offset alone:

```
lat_active, not steering_pressed,
abs(desired_la) < 0.05, abs(desired_jerk) < 0.05,
abs(desired_la - actual_la) < 0.05, abs(roll_rad) < radians(0.2)
```

Print, using the `f_term` already captured:

```
  ff offset check: n=NNNNN mean_f=+0.XXXX  (static predicts ~0.00, live predicts ~+0.33)
```

Add a one-line verdict: `consistent with static tune` if `abs(mean_f) < 0.15`, `consistent with LIVE tune` if `mean_f > 0.20`, otherwise `INCONCLUSIVE`. If the bucket has fewer than 200 samples, print `n too small` instead of a verdict rather than reporting a number that cannot carry one.

---

## Review outcome — first implementation pass

A first pass landed `reconstruct_unwind` and `summarize_unwind_reconstruction` in `tools/tuning/analyze_bolt_lateral.py` and was reviewed. Correct and to be preserved: the fixed `DT_CTRL` rather than the measured gap; `prev_setpoint` updated unconditionally on every active sample including guarded ones; the inactive reset to 0.0; NaN discipline throughout (the `isfinite` gate on `d_des_rate`, and `abs(roll_rad) < radians(0.2)` correctly dropping NaN rolls); phase masks, verdict thresholds and section placement all as specified; existing sections untouched.

Two fixes required, both folded into the tasks above; this is the changelog.

1. **Empty phases printed fabricated zeros** — `if n == 0` emitted `0.0000` for `unwind_frac`, `mean|i|`, `bias` and `mean|d_des|`. See Task 3.
2. **Reset keyed on the wrong flag** — the logic used `lat_active` (from a separate `carControl` message, potentially one frame stale at exactly the engagement boundaries where the reset fires) instead of `torqueState.active`, which is the same boolean co-located in the message being read. See Task 0.

Two nits also folded in: the constants and both helpers were defined after `main` (works, since the defs execute before `main()` is called at the bottom, but it is the only place in the file that breaks the convention), and the table's numeric columns did not line up with its header.

## Verify

```bash
python tools/tuning/analyze_bolt_lateral.py <dongle>/<route>
```

1. Every pre-existing section is byte-identical to the Chunk 1 run. Diff to confirm.
2. **Sanity gate on the reconstruction itself, before reading anything into the table.** Overall `unwind_frac` should be a small single-digit percentage of active samples. If it is ~0% or >50%, the rate reconstruction or the gap guard is wrong — fix that before interpreting. `mean|d_des|` in the single digits is a supporting sign the units are right; values in the hundreds mean `DT_CTRL` was applied wrongly.
3. `unclassified` should be a large but not overwhelming share — most driving is near-center or near-steady, so both deadbands exclude it. If `unclassified` is ~0 or ~100%, the deadbands are wrong.
4. **Then read the table.** The hypothesis predicts heavy firing on `entering_right`, near-zero on `exiting_right`, and the reverse pattern on the left. Report the four `unwind_frac` values plainly whatever they are — a symmetric result is a valid and useful outcome that kills the hypothesis, not a failure to be explained away.
5. Task 4's verdict should read `consistent with static tune`. If it reads `consistent with LIVE tune`, stop and report — that contradicts Chunk 1 and one of the two analyses is wrong.

**A known, deliberate bias to state when reporting results, not to "fix":** the gap guard suppresses the first active sample after every inactive stretch, whereas the controller *does* evaluate that frame — it computes `rate = setpoint / 0.01` off the 0.0 reset, which easily clears the −1.0 threshold and can fire. So the reconstruction slightly undercounts overall. This is intentional (a missed sample makes the previous setpoint untrustworthy), it is small against ~172k samples, and it should not favour left or right since engagement almost always happens near-center. Do not loosen the guard to raise the count.

## Out of scope

Do not implement direction/speed splits of the existing `masks` tuple (Chunk 3), oscillation detection or the 2D band table (Chunk 4), turn-in event analysis (Chunk 5), or multi-route support (Chunk 6). Do not modify `latcontrol_torque.py`, `cereal/log.capnp`, or any tune value. Do not "fix" the `unwind_detected` sign asymmetry — confirming it with data comes before touching the controller.

---

## Implementation outcome & Verification — second implementation pass

Status: **Complete & Verified**

1. **Task 0 (`torque_active`)**: Added `torque_active: bool` to `ControlSample`, populated as `torque_state.active`. Updated `reconstruct_unwind` and `summarize_unwind_reconstruction` to rely on `torque_active` to prevent 1-frame staleness at engagement boundaries.
2. **Task 3 formatting (Empty Phases & Alignment)**: Updated phase output table to print `--` when `n == 0` for any phase, and `--` for empty `ff_n` mean_f check. Perfectly aligned numeric columns under headers.
3. **Code Placement**: Moved constants (`DT_CTRL`, `UNWIND_D_DES_THRESHOLD`, `UNWIND_LAT_ACCEL_NEAR_ZERO`, `PHASE_LAT_ACCEL_DEADBAND`, `PHASE_RATE_DEADBAND`) and helper functions (`reconstruct_unwind`, `summarize_unwind_reconstruction`) to above `main()`.
4. **Restored `summarize_torque_points`**: Restored missing `summarize_torque_points(car_params.carFingerprint, points)` call at the end of `main()` with two blank lines formatting before `if __name__ == "__main__":`.
5. **Syntax Verification**: `python -m py_compile tools/tuning/analyze_bolt_lateral.py` passed cleanly without errors.
