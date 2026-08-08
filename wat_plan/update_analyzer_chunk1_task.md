# Chunk 1 task list — sample capture + effective tune reporting

**Audience:** an implementing subagent (Gemini Flash). Follow the tasks literally and in order.
**Only file to edit:** `tools/tuning/analyze_bolt_lateral.py`. Nothing else. No schema changes, no controller changes.

## Context

`analyze_bolt_lateral.py` reports on a 2022/2023 Chevy Bolt route. Steady-state lateral tracking looks fine, but low-speed transients are 3–6× worse and the driver reports a right-turn "slam" followed by a left overcorrection. The report today cannot diagnose this, partly because **it prints the wrong tune**: the `torqueTune=` line reads the static `CP.lateralTuning.torque` values, but with the Force Auto-Tune toggle on, the controller actually ran with live-filtered values (friction ≈ 0.137, `latAccelOffset` ≈ −0.318 instead of 0.130 / 0.0).

Chunk 1 is the small, prerequisite step of a six-chunk plan (`wat_plan/update_analyzer.md`): capture three extra per-sample fields that later chunks need, and print the *effective* tune so every later number is interpretable. **It must not change any existing output except one relabel.**

---

## Task 1 — Extend `ControlSample`

In the `ControlSample` dataclass (`tools/tuning/analyze_bolt_lateral.py:13-26`) add three fields at the end:

```python
  mono_time: int
  d_term: float
  curvature: float
```

Populate them in the `controlsState` branch of the message loop (currently lines 175-188):

- `mono_time=msg.logMonoTime` — the raw nanosecond int, no conversion.
- `d_term=torque_state.d` — the `d` field exists on `LateralTorqueState` (`cereal/log.capnp:923`) and is currently dropped.
- `curvature=` computed from the *same* `desiredLateralAccel` and `vEgo` used for the other fields, guarded against low speed:

```python
v = latest["carState"].vEgo
curvature = torque_state.desiredLateralAccel / (v * v) if v > 1.0 else 0.0
```

Do **not** use these fields in any existing computation or print. They are captured for Chunks 2–5 only.

## Task 2 — Capture `initData.params`

In `main`, the message loop begins with a `carParams` branch followed by `if car_params is None: continue` (lines 159-165). `initData` is emitted **before** `carParams`, so its handler must be placed **above that guard** or it will never run.

Add before the loop:

```python
  log_params: dict[str, bytes] = {}
```

and as the first branch inside the loop, before the `carParams` branch:

```python
    if which == "initData":
      log_params = {entry.key: entry.value for entry in msg.initData.params.entries}
      continue
```

`initData.params` is a `Map(Text, Data)` (`cereal/log.capnp:177`); keys are `str`, values are `bytes`. This exact access pattern is used in `system/loggerd/tests/test_loggerd.py:181`. All the params below are `PERSISTENT` only (`common/params_keys.h`), so loggerd writes their values.

## Task 3 — Resolve the effective tune

Add a module-level helper, e.g. `resolve_effective_tune(car_params, log_params, live_snapshot)`, returning the effective `(lat_accel_factor, lat_accel_offset, friction)` plus the intermediate flags for printing. It must **mirror** two pieces of existing production logic — do not invent new rules.

**Params to read** (decode bytes with `.decode()`; missing key → treat as absent):

| key | type | used for |
|---|---|---|
| `AdvancedLateralTune` | bool (`b"1"`/`b"0"`) | gates everything |
| `ForceAutoTune` | bool | |
| `ForceAutoTuneOff` | bool | |
| `SteerFriction` | float | custom friction |
| `SteerLatAccel` | float | custom latAccelFactor |
| `TuningLevel` | int | tuning-level gate (Step A0) |
| `TuningLevelConfirmed` | bool | tuning-level gate (Step A0) |

Helper suggestions: `_param_bool(log_params, key)` → `log_params.get(key, b"0") == b"1"`; `_param_float(log_params, key, default)` → `float(value)` inside `try/except (ValueError, TypeError)` falling back to `default`.

**Step A0 — the tuning-level gate. Do this first; every other param read depends on it.**

`get_value` short-circuits *before* it ever reads the param ([starpilot_variables.py:427](../starpilot/common/starpilot_variables.py)):

```python
if not condition or (self.starpilot_toggles.tuning_level < self.tuning_levels.get(key, 0)):
```

A param whose required level exceeds the session's tuning level behaves exactly as if its condition were false — it falls back to `default`, i.e. the static value. Reading the raw bytes without this gate can report `ForceAutoTune=1` on a route where the controller had it disabled, which is the precise failure mode this chunk exists to eliminate.

Session level, per [starpilot_variables.py:385](../starpilot/common/starpilot_variables.py) and `TUNING_LEVELS` (`MINIMAL 0, STANDARD 1, ADVANCED 2, DEVELOPER 3`, :246):

```
tuning_level = int(TuningLevel) if TuningLevelConfirmed else 2   # 2 == ADVANCED
```

Per-key required level is the **5th field** of each `params_keys.h` entry (`{flags, type, default, stock, tuningLevel}`). Hard-code this dict in the script with a comment naming that file as the source of truth:

```python
# required tuning level per key; 5th field of the entry in common/params_keys.h
PARAM_TUNING_LEVELS = {
  "AdvancedLateralTune": 2,
  "ForceAutoTune": 3,
  "ForceAutoTuneOff": 2,
  "SteerFriction": 3,
  "SteerLatAccel": 3,
}
```

Gate every read: if `tuning_level < PARAM_TUNING_LEVELS[key]`, treat the param as **absent** — bools become `False`, floats fall back to the static value. Fold this into `_param_bool` / `_param_float` (pass `tuning_level` in) rather than sprinkling checks at call sites.

Both `TuningLevel` and `TuningLevelConfirmed` are `PERSISTENT` with no `DONT_LOG` ([params_keys.h:590-591](../common/params_keys.h)), so they are in `initData.params`.

**Step A — toggle gating**, mirroring `starpilot/common/starpilot_variables.py:603,633-642`. Every param below is read through the Step A0 gate:

```
has_auto_tune       = live_snapshot.useParams  (False if there is no liveTorqueParameters message)
alt                 = AdvancedLateralTune
force_auto_tune     = alt and (not has_auto_tune) and ForceAutoTune
force_auto_tune_off = alt and has_auto_tune and ForceAutoTuneOff
```

The Bolt is a torque car with `steerControlType != angle`, so the `is_torque_car and not is_angle_car` conditions in the source are satisfied. Assert this rather than assume: skip the whole section (print one line saying so) if `car_params.lateralTuning.which() != "torque"`.

**Step B — custom-value resolution.** When `alt` is false the toggle value falls back to the static tune, so:

```
custom_friction  = clamp(SteerFriction, 0.0, 1.0)        if alt else static_friction
custom_lat_accel = clamp(SteerLatAccel,
                         0.5 * static_lat_accel_factor,
                         1.5 * static_lat_accel_factor)  if alt else static_lat_accel_factor
```

Then reproduce lines 639 and 642 **including their operator precedence** — `A and B and not C or D` parses as `(A and B and not C) or D`:

```
use_custom_friction  = (round(custom_friction, 2)  != round(static_friction, 2)          and not force_auto_tune) or force_auto_tune_off
use_custom_latAccel  = (round(custom_lat_accel, 2) != round(static_lat_accel_factor, 2)  and not force_auto_tune) or force_auto_tune_off
```

Note the consequence: with `ForceAutoTuneOff` on, both flags are True regardless of the param values.

**Step C — effective values**, mirroring `get_torque_control_params` (`selfdrive/controls/controlsd.py:50-71`) and the `use_live_params` expression at `controlsd.py:162`:

```
use_live_params = has_auto_tune or force_auto_tune

lat_accel_factor, lat_accel_offset, friction = static values
if use_live_params:
    if not use_custom_latAccel:
        lat_accel_factor = live.latAccelFactorFiltered
        lat_accel_offset = live.latAccelOffsetFiltered   # offset only ever comes from live
    if not use_custom_friction:
        friction = live.frictionCoefficientFiltered
if use_custom_latAccel:
    lat_accel_factor = custom_lat_accel
if use_custom_friction:
    friction = custom_friction
```

The live snapshot to use is `live_torque_snapshots[-1]` — **the same object the existing `liveTorqueFiltered=` line already prints**, so the two lines are guaranteed consistent. If `live_torque_snapshots` is empty, treat `has_auto_tune` as False and use static values; say so in the output.

## Task 4 — Print the new section

Insert after the existing `liveTorqueFiltered=` block (line 220) and before `summarize_control_samples(control_samples)`. Suggested format — keep it to plain `print` calls in the file's existing style (2-space indent, f-strings, `:.4f`):

The header line must start with `\n`, like every other section in this file (`"\nRoll context:"`, `"\nControlsState tracking:"`), or it renders welded to the `liveTorqueFiltered=` line.

```
Effective tune:
  toggles=AdvancedLateralTune=1 ForceAutoTune=1 ForceAutoTuneOff=0 hasAutoTune=0(@route start) tuningLevel=3
  resolved=useLiveParams=1 useCustomLatAccel=0 useCustomFriction=0
  static=   latAccelFactor=... latAccelOffset=... friction=... frictionAmp=...
  liveFilt= latAccelFactor=... latAccelOffset=... friction=... frictionAmp=...
  effective=latAccelFactor=... latAccelOffset=... friction=... frictionAmp=...
  WARNING: effective tune differs from static tune; report figures reflect the effective tune.
```

- `frictionAmp` = `friction * latAccelFactor` — the peak friction contribution in torque units, per `get_friction` (`opendbc_repo/opendbc/car/lateral.py:167`). This is the **only** path by which `latAccelFactor` affects this car: the Bolt's `torque_from_lateral_accel` is a siglin `np.interp` that ignores `torque_params` entirely (`opendbc_repo/opendbc/car/gm/interface.py:181-189`).
- Print the WARNING line only when any of the three effective values differs from its static counterpart (compare with a small tolerance, e.g. `1e-6`).
- Print `tuningLevel=` on the toggle line, and suffix `hasAutoTune=` with `(@route start)`. That value comes from a `liveTorqueParameters` message sampled near the beginning of the route, whereas production reads the persisted `LiveTorqueParameters` param — the previous route's converged state. Since `has_auto_tune` selects *which* of the two toggles applies, an early-route `useParams=0` can flip the branch, and the label makes a surprising reading self-explaining. Keep the current source; only label it.
- If `log_params` is empty (qlog without `initData`, or an old log), print `  params unavailable in log; toggle state unknown` and skip the toggle line, still printing static/live/effective from what is known.

## Task 5 — Relabel one existing line

Change the literal `"torqueTune="` (line 202) to `"staticTune="`. This is the **only** permitted change to pre-existing output.

---

## Review outcome — first implementation pass

A first pass landed in `tools/tuning/analyze_bolt_lateral.py` and was reviewed. Correct and to be preserved: the `initData` handler sits above the `car_params is None` guard; the three new fields are captured and used nowhere else; the `(A and B and not C) or D` precedence is faithful; the effective values reuse `live_torque_snapshots[-1]`, so they cannot disagree with the printed `liveTorqueFiltered=` line; only the `staticTune=` relabel touches existing output.

Three fixes are required. They are already folded into the tasks above; this is the changelog.

1. **Missing tuning-level gate (correctness).** `resolve_effective_tune` reads raw param bytes and ignores `get_value`'s level short-circuit. With `TuningLevelConfirmed` unset, or `TuningLevel` below 3, the controller's `force_auto_tune` is False while the script would report `ForceAutoTune=1 useLiveParams=1` and print live values as effective. See **Step A0**.
2. **`hasAutoTune` provenance unlabeled.** Add `(@route start)` and print `tuningLevel=`. See Task 4.
3. **Missing leading newline** on the `Effective tune:` header. See Task 4.

Noted, deliberately **not** to be changed: `_param_float(..., default=static)` is *safer* than production rather than equivalent. The shared handle is `Params(return_defaults=True)` ([starpilot_variables.py:254](../starpilot/common/starpilot_variables.py)), so an unset `SteerFriction` yields `"0.0"` and `get_value`'s `default=` never fires. In practice `_sync_stock_param` ([:589](../starpilot/common/starpilot_variables.py)) writes the stock value at boot, so 0.0 should not occur — keep the safer fallback rather than reproducing a path that would report `friction=0.0`.

---

## Verify

Not yet run. Re-run against the already-recorded ON route:

```bash
python tools/tuning/analyze_bolt_lateral.py <dongle>/<route>
```

1. Every pre-existing section — `carFingerprint`, `steerRatio`, `liveTorqueFiltered`, `Roll context`, `ControlsState tracking`, `TorqueEstimator fit`, `Torque map residuals`, `Linearized correction` — is byte-identical to the previous run, except `torqueTune=` now reads `staticTune=`. Diff the old and new output to confirm.
2. The new section reports `ForceAutoTune=1`, `ForceAutoTuneOff=0`, and effective `latAccelFactor=1.2916 latAccelOffset=-0.3182 friction=0.1370` — these must match the `liveTorqueFiltered=` line exactly.
3. It states whether `AdvancedLateralTune` was on, and at what `tuningLevel`. `AdvancedLateralTune` is the precondition gating both toggles; if it reads 0, the whole force-auto-tune story is wrong and this must be reported back rather than worked around. If `tuningLevel` reads below 3, `ForceAutoTune` was gated off in the controller no matter what its stored value says — same conclusion, report back.
   This step is also the smoke test for the Step A0 gate: if the effective triple does *not* come back as `1.2916 / -0.3182 / 0.1370`, check the tuning-level handling first.
4. The WARNING line appears (static friction 0.130 / offset 0.0 differ from effective).
5. Sanity-check the captured fields with a throwaway snippet or debugger — do **not** leave debug prints in the file: `mono_time` is strictly increasing with ~10 ms spacing, `d_term` is non-zero somewhere, `curvature` is finite everywhere and 0.0 only where `vEgo <= 1.0`.

## Out of scope for this chunk

Do not implement `unwind_detected` reconstruction, direction splits, oscillation detection, event analysis, or multi-route support — those are Chunks 2–6. Do not modify `latcontrol_torque.py`, `cereal/log.capnp`, or any tune value. Do not change how `live_torque_snapshots` is collected (it currently keeps the *first* 8 messages; that is a known quirk, leave it — the effective-tune section deliberately uses the same snapshot the existing line prints).

---

## Implementation outcome & Verification — second implementation pass

Status: **Complete & Verified**

1. **Step A0 Tuning Level Gate implemented:** Defined `PARAM_TUNING_LEVELS` mapping from `common/params_keys.h` and implemented `_get_tuning_level()`. All parameter reads gate on active session `tuning_level` vs required parameter level before decoding raw bytes.
2. **Toggle Line Updated:** Printed line includes `tuningLevel=` and labels `hasAutoTune=` with `(@route start)`.
3. **Leading Newline Added:** Section header is formatted as `\nEffective tune:`.
4. **Syntax & Verification:** Verified static compilation using `python -m py_compile tools/tuning/analyze_bolt_lateral.py`. Only `tools/tuning/analyze_bolt_lateral.py` was modified.
