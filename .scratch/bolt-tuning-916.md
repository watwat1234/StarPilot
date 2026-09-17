# Chevy Bolt lateral oscillation investigation (started 2026-09-16)

## Context / how this started

Driver reports a lateral oscillation on their **2022/2023 Chevrolet Bolt**
(`CHEVROLET_BOLT_ACC_2022_2023`) when turning right into a curving driveway that
continues to curve right after the turn-in. The oscillation is **heavy on the old
driving model ("SC")** and **less pronounced on RDFv4**. Goal: characterize it
objectively and find a lateral-tuning fix, the same way the separate Ioniq 6
chatter investigation was done (see `.scratch/ioniq-detailed-tuning.md` and the
`project_ioniq6_tuning_investigation` / `feedback_ioniq6_tuning_philosophy` /
`feedback_chatter_metric_scope` memory files in this repo's auto-memory system --
**this is a different car and a different investigation thread**, don't conflate
state between them, though the general "always measure, don't ship on
code-reading confidence" discipline still applies).

**Housekeeping done (2026-09-16, this session)**: this investigation started by
accident directly on `wat-ioniq-chatter-amplitude-metric` in the
`starpilot-ioniq-analysis` worktree (unrelated Ioniq branch, just happened to be
checked out). It has since been moved to its own **dedicated worktree**:
`/home/kirin/starpilot/starpilot-wat-bolt-analysis`, branch `wat-bolt-analysis`,
branched off `wat-bolt-tuning` (the branch that was actually running readonly in
the car for the analyzed route -- commit `2c47bf1f47e2`, see build-info note
below). This file and the three ad hoc scripts (`dump_window.py`,
`dump_gains_window.py`, `bolt_ff_residual_window.py`) now live in this
worktree's `.scratch/`. Also copied over from the Ioniq worktree's
`tools/tuning/` (not yet present on `wat-bolt-tuning`): `replay_common.py`,
`replay_latcontrol_torque.py` (replays the real `LatControlTorque` controller
against a logged route -- useful for testing a corrected FF curve against real
data), and `inspect_torque_buckets_streaming.py` (memory-bounded route reader).
Deliberately did **not** copy `chatter_metrics.py` -- its docstring and the
`feedback_chatter_metric_scope` memory rule scope it to the Ioniq sustained
near-center chatter investigation specifically; this Bolt issue is a
single-event decaying ringdown, a different shape of problem, not obviously the
same metric. Nothing has been committed on this branch yet (standing rule:
never commit without the user's explicit go-ahead, see
`feedback_commit_permission` memory).

## Environment / how to reproduce

- Worktree: `/home/kirin/starpilot/starpilot-wat-bolt-analysis` (a git worktree,
  NOT the main checkout -- run all commands from here, don't `cd` elsewhere).
  (Historical note: findings 1-6 below were originally gathered from the
  `starpilot-ioniq-analysis` worktree before this housekeeping move; nothing
  about the findings themselves changes, just the working directory.)
- Python environment: **use `uv run python3 ...`**, not bare `python3` -- the
  system python3 doesn't have numpy/capnp/etc. installed, `uv run` picks up the
  project's venv correctly. Every script below assumes this.
- Route data lives on the Windows-side filesystem, not in the repo:
  `/mnt/c/Users/Kirin/Documents/bolt_routes/00000137--3003b4ee60--<seg>/` --
  one directory per segment (0 through 12 exist as of this writing), each with
  `qlog.zst`, `rlog.zst`, `qcamera.ts`, `[def]camera.hevc`.
  **This is a different directory from the Ioniq routes**
  (`/mnt/c/Users/Kirin/Documents/ioniq_routes/`) -- don't confuse them, they're
  different cars. There's also an `ioniq_jul4/` directory, also unrelated.
- Dongle ID for this route is `4103f561284c7b04`, route id `00000137--3003b4ee60`.
  The segment of interest is **segment 10** (confirmed with the user --
  "10:11" in their shorthand meant segment 10 going into 11, not a wall-clock
  time).
- **Driving model confirmed via `initData` params in the rlog**:
  `DrivingModel=rdf43` / `DrivingModelName=Regret Driven Framework V4`. So
  **segment 10 of this route is the RDFv4 side** of the comparison (the
  "less pronounced" one) -- there is currently **no equivalent SC-model route
  identified/confirmed** for a real side-by-side. If the user provides one, it
  must go through the same "confirm which model via initData params" step
  before trusting which side of the comparison it represents.
- Build info from this route's `initData`: commit `2c47bf1f47e2`, branch
  `wat-bolt-tuning`, dirty=False. (That's the branch that was running readonly
  in the car when this was recorded -- not necessarily related to any branch
  name we create for this analysis work.)
- There's already a **purpose-built analysis tool** in this repo:
  `tools/tuning/analyze_bolt_lateral.py`. It takes a route/local-log path and
  prints: effective tune resolution (static vs. live vs. FLM-overridden),
  roll context, controlsState tracking stats by band, an "unwind
  reconstruction" (old vs. new integrator-freeze gate), torque-estimator
  residuals against the siglin curve, Bolt dynamic-gain-band tables, and
  **automatic turn-in event detection** (`detect_turn_in_events` /
  `summarize_turn_in_events`) with worst-events-by-peak-error reporting.
  Run it directly against a local rlog file, e.g.:
  ```
  uv run python3 tools/tuning/analyze_bolt_lateral.py \
    "/mnt/c/Users/Kirin/Documents/bolt_routes/00000137--3003b4ee60--10/rlog.zst" \
    --mode rlog
  ```
  (LogReader's `parse_direct` accepts a plain existing file path directly, no
  need to resolve dongle/route/segment through the comma API machinery.)
  **Caveat found this session**: this tool's own turn-in-event detector only
  found 4 events in the whole 60s segment (t_rel 3.1s, 18.2s, 48.9s, 51.3s
  from the first `controlsState` sample), and **none of them line up with the
  oscillation described below at t=6.5-11s**, even though that window clearly
  contains multiple qualifying desired-lat-accel threshold crossings with the
  right sign of jerk. Didn't root-cause why the detector missed it (didn't
  block this session's findings since raw sample dumps were used instead) --
  worth checking the `armed`/`TURN_IN_REARM_LA` re-arm logic in
  `detect_turn_in_events` (`tools/tuning/analyze_bolt_lateral.py`) if the event
  detector needs to be trusted for future segments.

## Time-window convention used throughout

**"t" / "t_rel" = seconds since the FIRST `controlsState` message in the
segment's rlog**, NOT since the first message of any type in the file (the
file's very first message can be ~600s of monotonic time before driving
actually starts, apparently because `logMonoTime` is continuous device uptime
across the whole route, and segment 10 starts ~10*60s into that). This matches
`analyze_bolt_lateral.py`'s own `t_rel` basis (`t0_mono_start =
samples[0].mono_time`), so times reported by that tool and times reported by
the ad hoc scripts below are directly comparable.

The user's window of interest is **t=6.5s to t=11.0s of segment 10** -- the
right turn into the curving driveway.

## Ad hoc scripts written this session (not yet committed)

**Update (housekeeping, 2026-09-16)**: these now live for real at
`.scratch/dump_window.py`, `.scratch/dump_gains_window.py`, and
`.scratch/bolt_ff_residual_window.py` in this worktree -- no longer only
inline in this doc / a session tmp dir. Still uncommitted (never commit
without explicit go-ahead, see `feedback_commit_permission` memory); still
reproduced in full below for reference/history.

### `dump_window.py` -- raw per-sample dump over a time window
Reads a local rlog, joins `controlsState`/`carState`/`carControl` the same way
`analyze_bolt_lateral.py` does, and prints one row per `controlsState` sample
(100Hz) in `[t_start, t_end]` (with 0.5s pad on each side, marked `(pad)`):
`t, v_ego, steer_angle_deg, steering_pressed, desired_la, actual_la,
desired_jerk, p, i, f, d, torque_cmd, active, saturated`. `t0` is set to the
first `controlsState` message's `logMonoTime`, per the convention above.

```python
#!/usr/bin/env python3
"""Dump controlsState/carState samples in a specific time window of a segment."""
import sys

from openpilot.tools.lib.logreader import LogReader, ReadMode

path = sys.argv[1]
t_start = float(sys.argv[2])
t_end = float(sys.argv[3])

lr = LogReader(path, default_mode=ReadMode.RLOG, sort_by_time=True)

t0 = None
latest = {}
rows = []

for msg in lr:
  try:
    which = msg.which()
  except Exception:
    continue
  if t0 is None and which == "controlsState":
    t0 = msg.logMonoTime / 1e9
  if t0 is None:
    continue
  t_rel = msg.logMonoTime / 1e9 - t0

  if which in ("carState", "carControl"):
    latest[which] = getattr(msg, which)
  elif which == "controlsState":
    lateral_state = msg.controlsState.lateralControlState
    if lateral_state.which() != "torqueState":
      continue
    if not (t_start - 0.5 <= t_rel <= t_end + 0.5):
      continue
    ts = lateral_state.torqueState
    cs = latest.get("carState")
    cc = latest.get("carControl")
    rows.append(dict(
      t=t_rel,
      v_ego=cs.vEgo if cs else float("nan"),
      steer_angle=cs.steeringAngleDeg if cs else float("nan"),
      steer_pressed=cs.steeringPressed if cs else None,
      desired_la=ts.desiredLateralAccel,
      actual_la=ts.actualLateralAccel,
      desired_jerk=ts.desiredLateralJerk,
      p=ts.p, i=ts.i, f=ts.f, d=ts.d,
      torque_cmd=cc.actuators.torque if cc else float("nan"),
      active=ts.active,
      saturated=ts.saturated,
    ))

print(f"n_rows(with 0.5s pad)={len(rows)}")
print(f"{'t':>6s} {'v_ego':>6s} {'ang':>7s} {'press':>5s} {'des_la':>7s} {'act_la':>7s} {'jerk':>7s} {'p':>7s} {'i':>7s} {'f':>7s} {'d':>7s} {'torq':>7s} {'act':>4s} {'sat':>4s}")
for r in rows:
  marker = "" if t_start <= r["t"] <= t_end else " (pad)"
  print(
    f"{r['t']:6.2f} {r['v_ego']:6.2f} {r['steer_angle']:7.2f} {str(r['steer_pressed'])[:1]:>5s} "
    f"{r['desired_la']:7.3f} {r['actual_la']:7.3f} {r['desired_jerk']:7.3f} "
    f"{r['p']:7.3f} {r['i']:7.3f} {r['f']:7.3f} {r['d']:7.3f} {r['torque_cmd']:7.3f} "
    f"{str(r['active'])[:1]:>4s} {str(r['saturated'])[:1]:>4s}{marker}"
  )
```
Invocation used: `uv run python3 dump_window.py "/mnt/c/Users/Kirin/Documents/bolt_routes/00000137--3003b4ee60--10/rlog.zst" 6.5 11`

### `dump_gains_window.py` -- Bolt FF/friction/center-output gain schedule over a window
Same log-joining approach, but for each sample in `[t_start, t_end]` (no pad)
evaluates the actual production gain-schedule functions from
`selfdrive/controls/lib/latcontrol_vehicle_tunes.py`
(`get_bolt_2022_2023_ff_scale`, `get_bolt_2022_2023_friction_scale`,
`get_bolt_2022_2023_friction_threshold`, `get_gm_base_friction_threshold`,
`get_bolt_2022_2023_center_output_scale`) against the real
`(v_ego, desired_la, desired_jerk)` at that sample, and prints the constants
plus a per-sample table plus a min/max summary.

```python
#!/usr/bin/env python3
"""Reconstruct the Bolt 2022-2023 FF/friction gain schedule for a specific time window of a segment."""
import sys

import numpy as np

from openpilot.tools.lib.logreader import LogReader, ReadMode
from openpilot.selfdrive.controls.lib.latcontrol_torque import BOLT_2022_2023_CARS
from openpilot.selfdrive.controls.lib.latcontrol_vehicle_tunes import (
  get_bolt_2022_2023_ff_scale,
  get_bolt_2022_2023_friction_scale,
  get_bolt_2022_2023_friction_threshold,
  get_gm_base_friction_threshold,
  get_bolt_2022_2023_center_output_scale,
  BOLT_2022_2023_FF_GAIN_LEFT, BOLT_2022_2023_FF_GAIN_RIGHT,
  BOLT_2022_2023_FF_ONSET, BOLT_2022_2023_FF_ONSET_WIDTH,
  BOLT_2022_2023_FF_CUTOFF, BOLT_2022_2023_FF_CUTOFF_WIDTH,
  BOLT_2022_2023_TRANSITION_SPEED, BOLT_2022_2023_PHASE_SCALE,
  BOLT_2022_2023_TURN_IN_BOOST_LEFT, BOLT_2022_2023_TURN_IN_BOOST_RIGHT,
  BOLT_2022_2023_UNWIND_TAPER_LEFT, BOLT_2022_2023_UNWIND_TAPER_RIGHT,
  BOLT_2022_2023_FRICTION_MULT, BOLT_2022_2023_FRICTION_LAT_RISE, BOLT_2022_2023_FRICTION_JERK_RISE,
  BOLT_2022_2023_CENTER_FRICTION_THRESHOLD_BUMP, BOLT_2022_2023_CENTER_FRICTION_THRESHOLD_SPEED,
  BOLT_2022_2023_TURN_IN_THRESHOLD_REDUCTION_LEFT, BOLT_2022_2023_TURN_IN_THRESHOLD_REDUCTION_RIGHT,
  BOLT_2022_2023_UNWIND_THRESHOLD_INCREASE_LEFT, BOLT_2022_2023_UNWIND_THRESHOLD_INCREASE_RIGHT,
  BOLT_2022_2023_TURN_IN_FRICTION_BOOST_LEFT, BOLT_2022_2023_TURN_IN_FRICTION_BOOST_RIGHT,
  BOLT_2022_2023_UNWIND_FRICTION_REDUCTION_LEFT, BOLT_2022_2023_UNWIND_FRICTION_REDUCTION_RIGHT,
)

path = sys.argv[1]
t_start = float(sys.argv[2])
t_end = float(sys.argv[3])

print("Active constants (static, car-wide):")
print(f"  ff_gain: left={BOLT_2022_2023_FF_GAIN_LEFT} right={BOLT_2022_2023_FF_GAIN_RIGHT}  "
      f"onset=({BOLT_2022_2023_FF_ONSET},w={BOLT_2022_2023_FF_ONSET_WIDTH})  "
      f"cutoff=({BOLT_2022_2023_FF_CUTOFF},w={BOLT_2022_2023_FF_CUTOFF_WIDTH})")
print(f"  transition_speed={BOLT_2022_2023_TRANSITION_SPEED}  phase_scale={BOLT_2022_2023_PHASE_SCALE}")
print(f"  turn_in_boost: left={BOLT_2022_2023_TURN_IN_BOOST_LEFT} right={BOLT_2022_2023_TURN_IN_BOOST_RIGHT}")
print(f"  unwind_taper: left={BOLT_2022_2023_UNWIND_TAPER_LEFT} right={BOLT_2022_2023_UNWIND_TAPER_RIGHT}")
print(f"  friction_mult={BOLT_2022_2023_FRICTION_MULT} lat_rise={BOLT_2022_2023_FRICTION_LAT_RISE} jerk_rise={BOLT_2022_2023_FRICTION_JERK_RISE}")
print(f"  turn_in_friction_boost: left={BOLT_2022_2023_TURN_IN_FRICTION_BOOST_LEFT} right={BOLT_2022_2023_TURN_IN_FRICTION_BOOST_RIGHT}")
print(f"  unwind_friction_reduction: left={BOLT_2022_2023_UNWIND_FRICTION_REDUCTION_LEFT} right={BOLT_2022_2023_UNWIND_FRICTION_REDUCTION_RIGHT}")
print(f"  center_friction_threshold_bump={BOLT_2022_2023_CENTER_FRICTION_THRESHOLD_BUMP} speed_anchor={BOLT_2022_2023_CENTER_FRICTION_THRESHOLD_SPEED}")
print(f"  turn_in_threshold_reduction: left={BOLT_2022_2023_TURN_IN_THRESHOLD_REDUCTION_LEFT} right={BOLT_2022_2023_TURN_IN_THRESHOLD_REDUCTION_RIGHT}")
print(f"  unwind_threshold_increase: left={BOLT_2022_2023_UNWIND_THRESHOLD_INCREASE_LEFT} right={BOLT_2022_2023_UNWIND_THRESHOLD_INCREASE_RIGHT}")

lr = LogReader(path, default_mode=ReadMode.RLOG, sort_by_time=True)

t0 = None
latest = {}
car_params = None
rows = []

for msg in lr:
  try:
    which = msg.which()
  except Exception:
    continue
  if which == "carParams" and car_params is None:
    car_params = msg.carParams
    continue
  if which in ("carState", "carControl"):
    latest[which] = getattr(msg, which)
  elif which == "controlsState":
    if t0 is None:
      t0 = msg.logMonoTime / 1e9
    t_rel = msg.logMonoTime / 1e9 - t0
    lateral_state = msg.controlsState.lateralControlState
    if lateral_state.which() != "torqueState":
      continue
    if not (t_start <= t_rel <= t_end):
      continue
    ts = lateral_state.torqueState
    cs = latest.get("carState")
    cc = latest.get("carControl")
    rows.append(dict(
      t=t_rel,
      v_ego=cs.vEgo if cs else float("nan"),
      des_la=ts.desiredLateralAccel,
      des_jerk=ts.desiredLateralJerk,
      torque_cmd=cc.actuators.torque if cc else float("nan"),
    ))

if car_params is not None and car_params.carFingerprint not in BOLT_2022_2023_CARS:
  print(f"\nWARNING: carFingerprint={car_params.carFingerprint} not in BOLT_2022_2023_CARS -- gain schedule below won't apply.")

print(f"\nn_samples in window={len(rows)}")
print(f"{'t':>6s} {'v_ego':>6s} {'des_la':>7s} {'jerk':>7s} {'ff_scale':>9s} {'fric_scale':>10s} {'fric_thr':>9s} {'base_thr':>9s} {'center_out':>10s} {'torq':>7s}")
for r in rows:
  v = r["v_ego"]
  la = r["des_la"]
  jerk = r["des_jerk"]
  ff = get_bolt_2022_2023_ff_scale(la, jerk, v)
  fr = get_bolt_2022_2023_friction_scale(v, la, jerk)
  th = get_bolt_2022_2023_friction_threshold(v, la, jerk)
  base_th = get_gm_base_friction_threshold(v)
  co = get_bolt_2022_2023_center_output_scale(la, v)
  print(f"{r['t']:6.2f} {v:6.2f} {la:7.3f} {jerk:7.3f} {ff:9.4f} {fr:10.4f} {th:9.4f} {base_th:9.4f} {co:10.4f} {r['torque_cmd']:7.3f}")

if rows:
  v_arr = np.array([r["v_ego"] for r in rows])
  la_arr = np.array([r["des_la"] for r in rows])
  jerk_arr = np.array([r["des_jerk"] for r in rows])
  ff_arr = np.array([get_bolt_2022_2023_ff_scale(la, j, v) for la, j, v in zip(la_arr, jerk_arr, v_arr)])
  fr_arr = np.array([get_bolt_2022_2023_friction_scale(v, la, j) for v, la, j in zip(v_arr, la_arr, jerk_arr)])
  th_arr = np.array([get_bolt_2022_2023_friction_threshold(v, la, j) for v, la, j in zip(v_arr, la_arr, jerk_arr)])
  co_arr = np.array([get_bolt_2022_2023_center_output_scale(la, v) for la, v in zip(la_arr, v_arr)])
  print(f"\nSummary over window: v_ego [{v_arr.min():.2f},{v_arr.max():.2f}]  "
        f"ff_scale [{ff_arr.min():.4f},{ff_arr.max():.4f}]  "
        f"friction_scale [{fr_arr.min():.4f},{fr_arr.max():.4f}]  "
        f"friction_threshold [{th_arr.min():.4f},{th_arr.max():.4f}]  "
        f"center_output_scale [{co_arr.min():.4f},{co_arr.max():.4f}]")
```
Invocation used: `uv run python3 dump_gains_window.py "/mnt/c/Users/Kirin/Documents/bolt_routes/00000137--3003b4ee60--10/rlog.zst" 6.5 11`

There's also a small `analyze_window.py` used once to numerically extract
steering-angle extrema/half-periods and error/p/i/f/saturation stats from
`dump_window.py`'s output -- not reproduced here since it's a quick-and-dirty
extrema finder (has a known bug: its 0.15s extrema-collapsing window doesn't
correctly extend when a bigger extremum appears slightly later, so it
misdetected/dropped the deepest trough around t=7.85 in the printed table --
**don't trust its automated extrema list**, the manual read of the raw
`dump_window.py` output below is the reliable one). If reconstructing, it's
just: read the dump file, mean-center is NOT used (this is angle, not torque
or centered steering, so no centering issue like the Ioniq chatter-metric
had), find local max/min of the `ang` column, dedupe by picking the
most-extreme point in each 0.15s cluster (buggy as noted), report deltas
between successive extrema.

## Findings so far

### 1. Model identity confirmed
Segment 10 of this route was driven with **RDFv4** (`DrivingModel=rdf43`,
`DrivingModelName=Regret Driven Framework V4`) -- confirmed by reading
`initData.params` from the rlog. This is the driver's "less pronounced"
side. **No SC-model comparison route has been identified/pulled yet.**

### 2. The oscillation is real and well-characterized, not noise
Steering angle (`carState.steeringAngleDeg`) over t=6.0-10.8s of segment 10
(t=0 is first `controlsState` sample = start of driving in this segment; see
convention above), right turn into a continuously-tightening driveway curve:

```
t=6.0   -112 deg   (deep initial turn-in, unwinding fast, carried in from before the window)
t=6.5-6.9  0 -> +6.9 deg   (overshoots PAST CENTER to the opposite side)
t=7.0-7.9  crosses back, dives to -59.3 deg   (second, deeper excursion)
t=8.3-8.7  briefly recovers to ~-14 deg   (close to the eventual steady angle)
t=9.0-9.1  dives again to -36.5 deg   (third excursion)
t=9.5-9.6  back to ~-10 deg
t=10.0-10.1  fourth, smaller dip to -34 deg
t=10.4-10.8  settles at ~-29.7 deg and holds   (steady-state for the curve)
```

This is a **classic decaying/underdamped ringdown**: amplitude decays roughly
112 -> 59 -> 36 -> 34 degrees before settling near 30 deg (the apparent correct
steady-state angle for this curve), with **half-periods of ~0.46-0.9s once
it's oscillating (~1-1.2 Hz)**. Not attributable to sensor noise or logging
artifacts -- it's a coherent, physically continuous swing pattern sample to
sample at 100Hz.

### 3. Torque saturation during the worst swing
During the deepest dip (t≈7.14-7.68s, the -59.3 deg excursion), `carControl.
actuators.torque` is **pinned at the full -1.0 actuator limit for ~0.55s
continuously** (46+ consecutive 100Hz samples). Across the whole 6.5-11s
window, |torque_cmd| >= 0.99 for 10.2% of samples. `torqueState.saturated`
flag itself reads False throughout in the raw dump -- **that flag does not
appear to reflect this clipping** (worth checking `latcontrol_torque.py`'s
`saturated` computation if this matters later; not investigated further this
session).

### 4. Term breakdown during the window
- `f` (feedforward) dominates: mean |f|=0.99, peak 1.34.
- `p` (proportional) reacts hard: mean |p|=0.57, peak 1.87.
- `i` (integral) stays modest: mean |i|=0.12, range -0.52 to +0.08 -- **windup
  does not look like the primary story here**, unlike some of the anti-windup
  issues found in the separate Ioniq 6 investigation.
- Lateral-accel tracking error (`actualLateralAccel - desiredLateralAccel`)
  swings from -0.48 to +0.19 m/s^2 over the window (asymmetric, biased
  negative).
- `v_ego` ranges 4.3-10.2 m/s across the window (about 10-23 mph) -- i.e. this
  entire maneuver happens in the **low/transition speed band**, straddling
  several Bolt-specific speed-anchored knobs at once:
  `BOLT_2022_2023_TRANSITION_SPEED=9.0`,
  `BOLT_2022_2023_CENTER_FRICTION_THRESHOLD_SPEED=6.7` (both get crossed
  mid-window), plus `desired_la` itself rises from ~0 to ~0.79 m/s^2 with
  `desired_jerk` up to ~0.3 -- a fast, tight, low-speed turn-in.

### 5. Gain-schedule check -- the multipliers themselves look normal-range
Evaluated the real production functions
(`get_bolt_2022_2023_ff_scale/friction_scale/friction_threshold/
center_output_scale` from `selfdrive/controls/lib/latcontrol_vehicle_tunes.py`)
against the actual per-sample `(v_ego, desired_la, desired_jerk)` through the
window:

| quantity | range over 6.5-11s |
|---|---|
| `ff_scale` | 1.03 - 1.12 |
| `friction_scale` | 0.96 - 1.13 |
| `friction_threshold` | 0.168 - 0.233 (base threshold alone: ~0.176-0.183 -- the center-band bump only adds ~0.02-0.05) |
| `center_output_scale` | 0.93 - **1.00** (no output limiting active for nearly all of the saturated stretch) |

**None of these is an extreme or unusual value** -- they're all within a
normal ~1.0-1.13x band, and `center_output_scale` is at 1.0 (no attenuation)
for almost the entire saturated period. **Working conclusion: the Bolt-specific
gain-schedule multipliers (turn_in_boost, unwind_taper, center_taper, friction
threshold bump, etc.) are NOT what's amplifying this oscillation** -- the base
P+F control effort is large enough on its own to saturate the actuator during
this fast/tight/low-speed turn-in, and the schedule terms only nudge it a few
percent either direction. This reframes where a fix should be sought: probably
not "some Bolt knob is set too aggressively," more likely something in the
base feedforward map (siglin torque curve) or P-gain/delay-compensation
behavior, or possibly this maneuver is close to the physical torque limit of
what the actuator can deliver at this speed/curvature and the ringing is a
downstream symptom of hitting that limit repeatedly, not a tunable-gain
problem per se.

### 6. FF (siglin torque curve) systematically under-predicts torque in exactly this speed band -- and is structurally unvalidated there

Investigated open question #2. Wrote a new ad hoc script,
`.scratch/bolt_ff_residual_window.py`, that reimplements
`TorqueEstimator.handle_log`'s
measured-lateral-accel-from-yaw-rate + steer-torque pairing logic
(`selfdrive/locationd/torqued.py`) but with the `MIN_VEL` gate removed and
restricted to a `[t_start, t_end]` window, then runs the same
`siglin_torque` residual check `analyze_bolt_lateral.py`'s
`summarize_torque_points` uses.

**Key structural finding**: production's `TorqueEstimator` only ever
accumulates fit points when `v_ego > MIN_VEL = 15 m/s`
(`selfdrive/locationd/torqued.py:23`). This maneuver happens at 4.3-10.2 m/s
-- **entirely below `MIN_VEL`** -- so the existing whole-segment residual
check in `analyze_bolt_lateral.py` has **zero coverage of this speed band**;
it silently never sees these points. The live torque estimator has never
observed or corrected the FF curve down here; whatever the static/offline
`NON_LINEAR_TORQUE_PARAMS` siglin fit says is what's used, unvalidated below
highway speed.

Ran the new script over the whole 60s segment (not just the 6.5-11s window)
and bucketed residuals (`err = siglin_pred - actual_measured_steer`) by
speed:

| v_ego band | bias (pred - actual) | mae | n |
|---|---|---|---|
| 0-5 m/s | -0.067 | 0.339 | 117 |
| 5-7 m/s | -0.156 | 0.217 | 231 |
| 7-9 m/s | -0.198 | 0.237 | 96 |
| 9-11 m/s | -0.121 | 0.127 | 175 |
| 11+ m/s | **+0.034** | 0.061 | 78 |

Bias is negative and largest in magnitude in the **5-9 m/s band -- right
where the oscillation happens** -- and is roughly unbiased (small, opposite
sign) above ~11 m/s, i.e. right around where the live estimator would
actually be gathering data if it were engaged. This is a clean,
speed-monotonic trend, not noise.

Scoped just to the 6.5-11s oscillation window itself: n=80 points,
mae=0.239, bias=-0.239 (left-turn subset: n=71, bias=-0.221; right-turn
subset: n=9, bias=-0.378 but very small n, don't over-read that split).

**Interpretation**: the FF (`f`) term is systematically under-delivering
torque relative to what's actually needed to hold the measured lateral
accel, specifically in the 5-11 m/s band this maneuver lives in. That
matches finding #4's observation that `f` dominates control effort but `p`
still has to react hard (mean |p|=0.57, peak 1.87) -- if FF is shorting the
required torque by ~0.15-0.2 on a sustained basis (not just transient
error), P has to fill a *systematic* gap every cycle, not just noise, which
is a more overshoot-prone thing for a P term to be doing, especially on top
of the already-observed actuator saturation (finding #3). This reframes the
likely fix target again: not just "the P/delay response is misbehaving in
isolation" (open question #3) but possibly "the P response is compensating
for a genuinely undersized low-speed FF map," which would point toward
fixing/extending the siglin fit's low-speed behavior (or feeding it live
data down here) rather than only tuning P/delay.

**Caveats / not yet verified**:
- Haven't cross-checked this against a genuinely different maneuver/segment
  at similar speed to confirm the bias isn't an artifact of turning itself
  (e.g. unmodeled dynamics during transients vs. steady low-speed turning).
- Sign/convention was reasoned through but not independently
  cross-validated against a known-good reference point outside this
  script -- worth a sanity gut-check before leaning on the exact numbers.
- Script bypasses `MIN_VEL` but keeps all other production gates
  (`lat_active`, `~steering_pressed`, `abs(steer) > STEER_MIN_THRESHOLD`) --
  right(lat<0) n=9 is thin, low confidence on that subset split.

## Open questions / next steps (not yet started)

1. **Find/confirm an SC-model comparison route.** Nothing has been pulled for
   the old model yet. Per the standing chatter-metric-scope lesson from the
   Ioniq investigation (`feedback_chatter_metric_scope` memory -- applies here
   too even though it's a different car/metric): any comparison must be
   same-maneuver / same-route-type, not a blind cross-route amplitude
   comparison. Ideally the *same* driveway, same direction, comparable speed.
2. ~~Check the base FF map (siglin torque curve) fit quality at this
   speed/lat-accel point~~ **DONE, see finding #6** -- FF under-predicts
   (not over-predicts) torque by a speed-dependent amount, worst in the
   5-9 m/s band, and this speed range is structurally invisible to the live
   torque estimator (`MIN_VEL=15`). Follow-ups this opened up, not yet
   started:
   - Cross-check the bias against another low-speed turning segment (not
     just this one maneuver) to make sure it's a genuine speed-dependent FF
     gap and not an artifact specific to this turn-in transient.
   - Decide what a fix would even look like: extend/re-fit
     `NON_LINEAR_TORQUE_PARAMS` for low speed, or lower `MIN_VEL` so the
     live estimator can see and correct this band (careful: `MIN_VEL=15` is
     shared across `ALLOWED_CARS`, changing it is not Bolt-specific), or a
     Bolt-specific low-speed FF scale knob (parallel to how
     `get_bolt_2022_2023_ff_scale` already scales FF by speed/jerk -- check
     whether it happens to already push the *wrong* direction at this speed,
     since finding #5 found it currently ranges 1.03-1.12, i.e. slightly
     boosting FF, when the residual says FF needs MORE boost here, not
     less).
3. **Check steerActuatorDelay / P-gain behavior** given the actuator is
   genuinely saturating -- is the P response overshooting because of a delay
   mismatch, or is this just an actuator torque ceiling being hit on a
   maneuver that demands more?
4. **Root-cause why `detect_turn_in_events` in `analyze_bolt_lateral.py`
   didn't flag this window** as an event (see caveat in the Environment
   section above) -- would make future characterization faster if the
   existing tool's automated detector can be trusted instead of hand-dumping
   windows each time.
5. ~~Move this file and the scratch scripts into a dedicated branch~~ **DONE,
   see housekeeping note at top of doc** -- now in worktree
   `/home/kirin/starpilot/starpilot-wat-bolt-analysis`, branch
   `wat-bolt-analysis`. Still uncommitted; consider committing the scripts
   under `tools/tuning/` once they're useful beyond this one-off window (ask
   the user first per the commit-permission rule).

## Cross-route scan for a similar signature in routes 135 and 138

The user provided two new routes to check for a recurrence of the 137/10
oscillation: `00000135--dab4b928bd` and `00000138--005fac0409` (both under
`/mnt/c/Users/Kirin/Documents/bolt_routes/`, same nested-subdir layout as
137: segments live at `<route_dir>/<route_dir_basename>--N/rlog.zst`, 12
segments each, 0-11). Both routes are also **RDFv4** (`DrivingModel=rdf43`),
confirmed via `initData` the same way as finding #1.

**Tools written this session** (both uncommitted, in `.scratch/`):
- `scan_ringdown_signature.py` -- an early attempt at matching the literal
  137/10 steering-angle waveform (decaying alternating-sign extrema). **Not
  trusted / abandoned partway through** -- while building it, found a real
  bug in its extrema detector (adjacent-sample-diff sign-change scan silently
  drops an extremum that sits on a flat quantization plateau, e.g. 3
  identical-angle samples at the true peak) that caused it to miss the
  documented -59.3 deg trough at t=7.84s on 137/10 itself. **Do not trust
  this script's output** -- the fixed version of the extrema logic lives
  instead in `scan_saturation_events.py`'s `find_extrema_robust` (see below).
  Kept the file only as the source of that lesson; superseded by the
  mechanism-based scanner.
- `scan_saturation_events.py` -- the scanner actually used. Per the user's
  redirect (137/10's exact steering waveform is road-geometry-specific to
  that one driveway and "rarely happens anywhere else"), this does NOT try
  to match the waveform. Instead it looks for the underlying **controller
  mechanism** from findings #3/#4/#6: sustained actuator saturation
  (`|torque_cmd| >= 0.99` for >= 0.15s) while hands-off, in the 5-9 m/s
  FF-under-prediction speed band. Sanity-checked against 137/10 itself first
  (correctly flags t=7.03-7.47s, the real event, before trusting it on new
  data). Usage: `uv run python3 .scratch/scan_saturation_events.py
  "/mnt/c/Users/Kirin/Documents/bolt_routes/<route_dir>"`.
  **Iteration 1** (saturation-only) also fired on ordinary large monotonic
  steering unwinds -- the user identified these by eye as **90-degree
  left-turns onto a straight road** (confirmed independently: dumped
  135/seg6 and 138/seg0's flagged windows and saw a smooth, non-oscillating
  angle sweep of 100-300+ degrees, e.g. 138/seg0 t~34-36s goes -341 deg ->
  -92 deg with zero direction reversals). **Iteration 2** added a shape
  classifier (`analyze_shape`) to tell "137/10-style ringing on a continuing
  curve" apart from "big unwind onto a straight": requires (a) >=2 genuine
  direction reversals (via the fixed `find_extrema_robust`, which handles
  flat-plateau extrema correctly), (b) total angle span in the event+2s-post
  window < 150 deg (137/10's real swings were 30-115 deg; the 90-degree
  unwinds spanned 250-430 deg), and (c) `desired_la` keeps the same sign and
  magnitude >= 0.3 m/s^2 both before and ~2s after the event (i.e. the curve
  keeps curving, doesn't straighten out). This correctly re-flags 137/10's
  real event and correctly drops all the 90-degree-unwind false positives.

**Result after the shape filter:**
- **Route 135**: zero candidates.
- **Route 138**: one candidate, **seg10 t=43.52-45.51s** (v_ego 3.9-5.2 m/s,
  hands-off, span=139 deg, continues_curving=True).

**138/seg10 t=43.5-48.5s investigated in detail and reclassified -- NOT the
same mechanism, do not treat as a second 137/10 data point:**
- Raw dump (`dump_window.py`, t=43-48s) shows: angle climbs to a peak of
  ~175 deg and holds (torque pinned near +-1.0 for extended stretches),
  then unwinds fast from ~175 -> 38 deg, overshoots back up to ~81 deg, then
  settles down to ~30 deg. This is a **single overshoot**, not a decaying
  multi-cycle ringdown like 137/10 (the shape classifier's "76 reversals"
  reading for this window is quantization-noise-inflated, not a real
  oscillation count -- don't trust that number as a cycle count).
- **The user identified this from context as a left turn where the
  controller actually saturated hard enough to trigger the "Take Control"
  alert and cede/regain control for a few seconds** -- confirmed in the log:
  `onroadEvents` shows `steerSaturated` firing at t=44.93s with
  `selfdriveState.alertText = 'take control' / 'turn exceeds limit'`, plus a
  second brief `steerSaturated` blip at t=46.60-46.62s. (Note: I could not
  find an actual drop in `carControl.latActive` around this window in the
  log -- it read `True` continuously from t=42.02 onward through t=55 -- so
  whatever "gave up control" looked like from the driver's seat, it isn't
  visible as a `latActive` transition in this particular signal; didn't dig
  further into which field would show it.)
- **Decision (discussed with user): drop 138/10 from this investigation.**
  It's a qualitatively different, more severe failure category (crosses the
  actual `steerSaturated` disengage-alert threshold) than 137/10 (a
  sub-threshold, no-alert P/FF ringing/overcorrection). Chasing it would
  pull focus into disengage-timer/alert-threshold logic that's orthogonal to
  the 137/10 tuning question. One soft connection worth remembering: if the
  eventual 137/10 fix is "boost the low-speed FF curve" (finding #6), that
  would likely reduce actuator load in cases like 138/10 too, as a
  side-effect -- not a reason to invest more analysis in 138/10 now, just
  a note that the two aren't fully unrelated.

**Net takeaway: no clean second occurrence of the 137/10 oscillation found in
135 or 138.** The investigation should stay focused on the original 137/10
event and the open questions in the previous section (SC-model comparison
route, FF/MIN_VEL fix direction, steerActuatorDelay/P-gain check, the
`detect_turn_in_events` miss).
