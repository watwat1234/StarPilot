# Chevy Bolt lateral oscillation investigation (started 2026-09-16)

## >>> NEXT STEP (as of 2026-09-18, read this first) <<<

**Fix (a) (the additive low-speed FF correction, finding #13) was driven
on-vehicle and did NOT help -- reverted, see finding #15.** Do not re-ship
that diff as-is. Decide between item #7 (planner/demand-side) and fix (b)
(easing `error_with_lsf`'s low-speed P-amplification), per finding #10
below -- those are now the two live, unprototyped candidate directions.

**Decide between item #7 (planner/demand-side) and a *different* FF-side
angle than the one already tried**, per finding #10 below. The gate check
(item #6, "is 137/10 genuinely actuator-torque-limited?") is now answered --
with a nuance neither original branch of that question anticipated -- so
don't re-run it; read finding #10 first.

Short version: the physical lateral accel this maneuver needs is nowhere
near the actuator's ceiling (2-3x headroom), so it is **not** torque-limited
in the "car literally can't turn this tight at this speed" sense. But the
PID's *internal correction signal* (`p+i+f+d`, pre-clip) genuinely does clip
against that same ceiling intermittently (6.9% of samples in the 6.5-11s
window, worst burst exactly matching finding #3's -59.3 deg dip) -- driven
by the low-speed error-amplification factor (`error_with_lsf`) inflating a
modest tracking error into an oversized P response, not by the maneuver
needing more lateral accel than the tires/actuator can produce. Finding #9's
FF-knob prototype (`FF_GAIN_LEFT`/`TURN_IN_BOOST_LEFT`) is confirmed too
weakly gated to close this: it can only move the FF term by ~0.02-0.05 at
these lateral-accel magnitudes, versus the ~0.37 (up to 16% over ceiling)
excess actually observed. That doesn't reopen item #8 as originally scoped
-- it rules it out more conclusively -- but it does suggest two *new*,
not-yet-investigated candidate directions worth weighing against item #7:
(a) a more substantial low-speed FF fix sized to finding #6's actual
~0.2-0.24 residual gap (previously waved off as "too broad-blast-radius,
undersupported by data" when the candidate was re-fitting
`NON_LINEAR_TORQUE_PARAMS`/lowering `MIN_VEL` -- worth revisiting now that
there's a concrete mechanism tying it to this event), or (b) easing the
`error_with_lsf` low-speed P-amplification itself so a modest tracking error
doesn't get blown up into a correction that exceeds the ceiling. Neither (a)
nor (b) has been prototyped yet. Item #7 (planner-side demand easing)
remains a live, untouched alternative that would attack the same root cause
(the size of the tracking error P has to fight) from upstream instead.

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
- **Hardware constraint (user-reported, not independently verified against
  `CP.minSteerSpeed` in code this session): comma cannot command this Bolt's
  steering rack below ~6 mph (~2.68 m/s)**, for reasons not further
  specified. Consistent with the observed data -- the whole 6.5-11s
  oscillation window stays at 4.3-10.2 m/s (9.6-22.8 mph), never approaching
  6 mph, so nothing in this investigation contradicts it. Relevant for
  scoping any low-speed FF correction curve (item 8a): its low end doesn't
  need to extend meaningfully below ~6 mph, since steering won't be actively
  commanded there regardless of what the curve says.

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

### CORRECTION to finding #6: the "live estimator never looks below MIN_VEL" framing understated the problem -- it never looks at all, at any speed, for this car

Caught by the user questioning whether `torqued.py`'s live tuning even
applies to GM/starpilot's custom tune in the first place, rather than being
speed-gated. Checked directly rather than trust the earlier framing:

```python
# selfdrive/locationd/torqued.py
ALLOWED_CARS = ['toyota', 'hyundai', 'rivian', 'honda']
self.use_params = CP.brand in ALLOWED_CARS and CP.lateralTuning.which() == 'torque'
```

GM is not in `ALLOWED_CARS`. `controlsd.py:492` only calls
`LaC.update_live_torque_params(...)` (the call that actually pushes live
estimates into the controller) when `torque_params.useParams or
force_auto_tune` -- and `useParams` is exactly this `self.use_params` flag,
`False` for every GM car including the Bolt. (`force_auto_tune` is a
starpilot toggle that could override this; default is off, and **whether it
was active on this specific route was not checked** -- if it turns out to be
on, this correction would need re-checking.)

**So the correct statement is stronger than finding #6's original framing,
not weaker**: it's not "the live estimator watches real driving above 15 m/s
and is blind below it" (implying some speed range gets corrected) -- for the
Bolt, **the live estimator's output is never applied at any speed**, GM cars
are excluded from live torque tuning entirely at the `controlsd.py` gate.
The `MIN_VEL=15` gate inside `torqued.py` is real code and does what
finding #6 said, but it's moot for this car -- GM never reaches the "use
these estimated params" stage regardless of `v_ego`. Combined with finding
#10's separate point (the siglin FF curve ignores `torque_params` entirely
regardless of live/static), the Bolt's FF curve is **fully static, full
stop** -- not "static only in an unvalidated speed band."

This doesn't change finding #6's core numeric result (the residual bias
measured from real driving is still real and still speed-dependent) or the
conclusion that a low-speed FF fix (item 8a) is worth prototyping -- if
anything it strengthens the case, since there's no live-tuning path at all
standing between "the static fit is wrong" and "it stays wrong forever" for
this car. It does mean: don't describe or design a fix around "extending
live coverage below `MIN_VEL`" as if that would help GM/Bolt -- it wouldn't,
since GM doesn't consume live params regardless. Any fix here has to be a
static constant/curve change (or an FLM-testable override), not a live-tuning
change.

**Follow-up refinement (same session, user confirmed `force_auto_tune` is
actually enabled on their own Bolt)**: `force_auto_tune` is exactly the
override this correction flagged as unverified -- confirmed active for the
user's car. This does change what applies on their vehicle specifically, but
not the conclusion above:

- `use_live_params = ... and (torque_params.useParams or force_auto_tune)`
  (`controlsd.py:492`) -- `force_auto_tune=True` bypasses the GM exclusion at
  the *application* gate, so `update_live_torque_params(latAccelFactor,
  latAccelOffset, friction)` **is** called on this car.
- Of those three, only `latAccelOffset` (roll compensation) and `friction`
  (via `get_friction()`) feed anything the Bolt's controller actually uses.
  `latAccelFactor` is also live-updated, but for this car it's only consumed
  *inside* `get_friction()` as a multiplier on the friction term
  (`opendbc/car/lateral.py:196`: `torque_params.friction *
  torque_params.latAccelFactor`) -- it has no path to the FF curve itself
  (`torque_from_lateral_accel_siglin` ignores `torque_params` outright, as
  established above). So in practice, `force_auto_tune` live-tunes friction
  magnitude and roll-offset compensation for this car, not the base
  lataccel-to-torque mapping.
- Critically, **`force_auto_tune` only changes the application gate, not the
  fitting gate**: `torqued.py`'s point accumulation for the underlying
  `latAccelFactor`/`friction`/`latAccelOffset` estimate is separately gated
  at `vego > MIN_VEL = 15 m/s` (`torqued.py:202`), unconditionally,
  independent of `force_auto_tune` or car brand. So even with it on, the
  live friction/offset values applied to this car are still fit exclusively
  from >15 m/s driving and extrapolated down into the 4-10 m/s regime this
  oscillation lives in -- not measured there either. Finding #6/#10's core
  conclusion (this speed band is structurally unvalidated, static-FF-curve
  fix is the right target) is unaffected by whether `force_auto_tune` is on.

### 7. `steerActuatorDelay`/delay-compensation checked -- ruled out as the cause

Investigated open question #3. Wrote `.scratch/check_delay_lag.py`, which (a)
dumps the `liveDelay` message (the live-estimated lateral delay actually fed
into `LatControlTorque.update()` as `lat_delay = liveDelay.lateralDelay +
LAT_SMOOTH_SECONDS`, per `controlsd.py:788`/`:292` -- `LAT_SMOOTH_SECONDS=0.1`
is a GM-brand constant, hardcoded in the script rather than imported from
`selfdrive/modeld/modeld.py` since that module transitively needs the
device's compiled `msgq` extension, see `project_native_ext_testing` memory)
and (b) empirically cross-correlates the logged delay-compensated setpoint
(`torqueState.desiredLateralAccel`) against the instantaneous measurement
(`torqueState.actualLateralAccel`) over the 6.5-11s window, since if the
compensation were mismatched the measurement should lag (or lead) the
setpoint by roughly the mismatch amount.

Run: `uv run python3 .scratch/check_delay_lag.py
"/mnt/c/Users/Kirin/Documents/bolt_routes/00000137--3003b4ee60/00000137--3003b4ee60--10/rlog.zst"
6.5 11` (note the actual path has the route dir nested one level deeper than
the shorthand earlier in this doc suggested: `<routes>/<route>/<route>--N/`,
not `<routes>/<route>--N/` directly).

**Results:**
- `liveDelay.status = estimated` throughout the window (not falling back to
  an unconverged/invalid default) with `lateralDelay = 0.334s` -- already
  *larger* than the static GM default (`CP.steerActuatorDelay = 0.2s`ish, see
  `opendbc/car/gm/interface.py`), i.e. the live estimator had already found
  and was compensating for more delay than the static guess. Effective
  `lat_delay` fed to the controller: `0.434s`.
- Cross-correlation of measurement vs. setpoint peaks at lag **+0.06s**
  (corr=0.913) vs. **0.0s** (corr=0.906) -- a small, not sharp, offset; the
  correlation curve is quite flat across the whole -0.1s to +0.12s range.
  This is not the signature of a meaningfully mismatched delay compensation
  (which would show a clear, sizable peak shift).

**Conclusion: delay compensation is not implicated.** The live delay
estimate was active, larger than the static default, and tracking the
measurement well. This answers open question #3 in the negative and
reinforces finding #6 (undersized low-speed FF map) combined with finding #3
(actuator saturation) as the leading explanation for the ringing, rather
than a P/delay-phase problem.

**Caveat**: `lateralDelayEstimateStd` read exactly `0.0000` at every sample
in the dump -- didn't verify whether that's a genuine converged-variance
value or an artifact of how/when `lagd.py` populates that field; doesn't
change the conclusion above (the cross-correlation check is independent of
it) but worth a sanity check if `liveDelay` variance ever becomes load-bearing
for a future finding.

### 8. An existing knob already targets this exact regime -- and it's set asymmetrically the wrong way for a right turn

Before inventing a new Bolt-specific low-speed FF knob (the direction floated
after finding #7), checked whether `get_bolt_2022_2023_ff_scale`
(`selfdrive/controls/lib/latcontrol_vehicle_tunes.py`, function starts
~line 2367) already has a mechanism for "boost FF during a low-speed
turn-in" -- **it does**:

- `_bolt_2022_2023_low_speed_factor(v_ego) = 1 / (1 + (v_ego /
  BOLT_2022_2023_TRANSITION_SPEED)^2)`, with `TRANSITION_SPEED=9.0` -- this
  factor fades in almost exactly across the oscillation's 4.3-10.2 m/s speed
  range (finding #4).
- `turn_in_boost = 1 + turn_in_boost_{left,right} * turn_in_weight *
  low_speed_factor`, where `turn_in_weight = max(phase, 0)` and `phase =
  tanh(desired_la * desired_jerk / BOLT_2022_2023_PHASE_SCALE)` -- this
  fires specifically when desired lateral accel and desired jerk share sign,
  i.e. during turn-in (not unwind), which is exactly this maneuver's shape.
- The base `gain` term (`BOLT_2022_2023_FF_GAIN_LEFT` / `_RIGHT`) also
  multiplies the FF boost, keyed on turn direction via
  `_bolt_2022_2023_side_value` (`left_value if desired_lateral_accel >= 0.0
  else right_value`, i.e. positive/left-turn vs. negative/right-turn
  convention).

**Current constant values are asymmetric in the wrong direction for this
case:**

| knob | left | right |
|---|---|---|
| `BOLT_2022_2023_FF_GAIN_{LEFT,RIGHT}` | 0.11 | **0.06** |
| `BOLT_2022_2023_TURN_IN_BOOST_{LEFT,RIGHT}` | 0.18 | **0.13** |

Right turns currently get *less* low-speed turn-in FF boost than left turns
on both knobs, even though: the driver's reported oscillation is specifically
on a right turn-in, and finding #6's residual data showed the (thin, n=9)
right-turn subset of the 6.5-11s window had a *larger* FF under-prediction
bias (-0.378) than the left-turn subset (-0.221). Not conclusive on its own
(n=9 is thin, noted as low-confidence in finding #6), but it's a real,
already-existing lever pointed in a suspicious direction, not a made-up one.

**Also notable**: both `ff_gain_{left,right}` and `turn_in_boost_{left,right}`
are read through `_flm_vehicle_knob("gm_bolt_2022_2023.<name>", <static
default>)` -- the same FLM (fleet-tuning override) mechanism referenced
elsewhere in this codebase (`starpilot/system/the_galaxy/flm_workspace.py`,
`flm_active_profile_id`/`flm_active_overrides` in `latcontrol_torque.py`).
**Not yet confirmed** whether FLM actually has a live/per-vehicle override
path for these two specific knob names that could be used to test a bump
without a full build+deploy cycle -- that's the immediate next thing to
check before deciding how to ship a fix.

**Revised fix-direction conclusion**: don't invent a new Bolt-specific
low-speed FF knob (the direction floated after finding #7) -- retune the
existing `FF_GAIN_RIGHT` / `TURN_IN_BOOST_RIGHT` constants (or their FLM
overrides, if that path exists) using finding #6's residual data to size the
bump, rather than adding new code surface for something the tune already
has a mechanism for.

### CORRECTION to finding #8: the sign mapping above was backwards

Caught while sizing a candidate value for the "revised fix-direction
conclusion" just above -- **do not act on that conclusion as written**. The
`_bolt_2022_2023_side_value(desired_lateral_accel, left_value, right_value)`
naming ("left"/"right") is keyed on the SIGN of `desired_lateral_accel` in
this controller's internal units, which is **not** the same as the physical
turn direction. `latcontrol_torque.py`'s `update()` computes
`measured_curvature = -VM.calc_curvature(...)`, an explicit negation of
`VM.calc_curvature`, which itself is same-signed with `steeringAngleDeg`
(positive angle = physical left/CCW turn, the standard openpilot
convention). Net effect, confirmed against the raw log data (t=6.01 in this
window: `steer_angle=-112 deg`, a physical right turn, paired with
`desired_la=+0.521`, both positive throughout the whole 6.5-11s window):
**positive `desired_lateral_accel` = a physical RIGHT turn** in this
controller's units, not left.

That flips the conclusion: 137/10's entire right-turn oscillation runs on
`desired_lateral_accel >= 0`, which selects the **`_LEFT`-named** constants
-- `FF_GAIN_LEFT=0.11`, `TURN_IN_BOOST_LEFT=0.18` -- not the `_RIGHT` ones.
Those are already the *larger* of the two knob pairs, not the neglected
smaller ones. The asymmetry noted above (right-side knobs smaller than
left-side) is real, but it's irrelevant to this specific event -- this event
already gets the bigger boost, and per finding #6, still under-predicts by
~0.2-0.24 with that bigger boost already applied.

### 9. Prototyped the "just bump FF_GAIN_LEFT/TURN_IN_BOOST_LEFT" fix open-loop -- result is a near-null effect, don't ship this alone

Built `.scratch/prototype_bolt_ff_tune.py`: reruns the real
`LatControlTorque.update()` against segment 10's logged inputs twice (stock
constants vs. a monkeypatched candidate), diffing the recomputed FF term and
torque command over 6.5-11s. Needed a temporary x86_64 rebuild of
`msgq_repo/msgq/ipc_pyx.so`, `common/transformations/transformations.so`
(+ their build-cache side effects `common/libcommon.a`,
`panda/board/obj/{gitversion.h,version}`) via `scons` per
`project_native_ext_testing` memory -- reverted with `git checkout --` after
the run, working tree is clean again. Run (from repo root, after `source
/home/kirin/starpilot/StarPilot/.venv/bin/activate && export
PYTHONPATH="$PWD"`, NOT via `uv run` which resets the venv):
```
python3 .scratch/prototype_bolt_ff_tune.py \
  "/mnt/c/Users/Kirin/Documents/bolt_routes/00000137--3003b4ee60/00000137--3003b4ee60--10/rlog.zst" \
  6.5 11
```
Candidate tried: `FF_GAIN_LEFT 0.11 -> 0.20`, `TURN_IN_BOOST_LEFT 0.18 -> 0.32`
(roughly doubling both, as a first probe, not a sized-to-data final value).

**Caught and fixed a real bug in the script while building it**: an early
version gated all message processing (including the `t0` anchor) behind
"has `carParams` arrived yet", which silently anchored `t0` to whichever
`controlsState` followed the first `carParams` message (~2.3s later in this
file) instead of the true first `controlsState`, per the convention used
throughout this doc. Caught by cross-checking against `dump_window.py`'s
`v_ego` values for the same nominal timestamps (4.35 m/s expected at
t=6.01s; the buggy version showed 9.33 m/s there) -- a good example of why
this doc's "always measure, don't ship on code-reading confidence" discipline
extends to trusting a brand-new tool's own output before using it.

**Results (fixed version, correctly aligned)**:
- Over the full 6.5-11s window, saturation fraction barely moves: stock
  138/449 (30.7%) vs. candidate 141/449 (31.4%) samples at `|torque|>=0.99`.
- During the actual worst dip (~7.0-8.0s, the -59.3 deg excursion from
  finding #2), **both stock and candidate are already clipped at exactly
  -1.000** -- doubling the FF constants changes the *internal* `f` term
  (e.g. at t=7.14s: 0.652 -> 0.687) but the *output* `torque_cmd` is
  identical, because the actuator is already at the hard limit regardless of
  how the P/F split arrives there. More feedforward cannot reduce saturation
  during the part of the maneuver that's actually saturated.
- Outside the saturated stretch, the effect is small: max
  `|torque_candidate - torque_stock|` over the whole window is only **0.064**
  (out of a ±1.0 range), and averages **0.021** across the non-saturated
  samples -- even after *doubling* both constants. This is because
  `get_bolt_2022_2023_ff_scale`'s boost is itself gated by `onset`/`cutoff`
  sigmoids (centered near `abs_lateral_accel=0.12`) and `low_speed_factor`
  (fading out toward `TRANSITION_SPEED=9.0`) -- at the lateral-accel
  magnitudes seen early in this window (0.04-0.3 m/s^2), `extra_scale` is
  itself small, so doubling the constants only doubles a small number.

**Conclusion: don't ship "just retune `FF_GAIN_LEFT`/`TURN_IN_BOOST_LEFT`"
as a standalone fix based on this data.** Two independent reasons the
mechanism can't close finding #6's ~0.2-0.24 residual gap here: (1) it's
too weakly gated at these lateral-accel/speed values to produce a large
enough delta even at 2x the stock constants, and (2) for roughly a third of
the window the actuator is already torque-saturated, so no amount of extra
FF changes the actual commanded output there -- the bottleneck during the
worst part of the ringdown is the physical/software torque ceiling itself,
not the FF/P split feeding into it. This reopens the harder question
floated back in finding #5: this maneuver may be genuinely close to (or at)
the actuator's torque limit at this speed/curvature, in which case a
knob-level FF fix has a hard ceiling on how much it can help, and the
`detect_turn_in_events`/planner-side question (is the desired curvature
*rate* itself unnecessarily aggressive at low speed, could easing that
reduce how often/how deep the actuator saturates in the first place) may
matter more than which side of the FF split delivers the torque.

**Caveats on this prototype** (see also the module docstring in the script
itself):
- This is OPEN-LOOP: `CS` (steering angle, vEgo, ...) is replayed verbatim
  from the log -- what the real car did under the STOCK tune. There is no
  plant model, so this cannot show whether the ringdown itself would
  actually be damped by a given change, only whether the controller would
  have *commanded* something different at each instant along the real
  trajectory. A genuine validation needs an on-vehicle test or a real plant
  simulation.
- P and I terms are provably unaffected by this change (error only depends
  on setpoint/measurement, neither touched by the FF constants), so the
  diff shown is a clean isolation of the FF-side effect -- not conflated
  with any controller error-dynamics difference.
- Live torque params (`liveTorqueParameters`) are not applied in this
  script (skipped to avoid importing `controlsd.py`'s heavy dependency
  chain, see the script's inline comment) -- both runs use CP's static
  `torque_params`, fine for a relative stock-vs-candidate diff, not for
  matching the on-device absolute FF magnitude.

### 10. Gate check answered: NOT physically torque-limited, but the PID's internal correction signal does clip against the torque ceiling -- and it's a P-amplification artifact, not a demand-vs-capability wall

Investigated open item #6. Two things needed doing, both done:

**(a) Computed the real torque ceiling.** For the Bolt 2022/2023,
`CI.torque_from_lateral_accel()`/`lateral_accel_from_torque()`
(`opendbc/car/gm/interface.py:206-224`) both route through the siglin curve
whenever `get_nonlinear_torque_params(fingerprint)` is set (it is, for this
car) -- and that branch **ignores the `torque_params` argument entirely**,
using only the static `NON_LINEAR_TORQUE_PARAMS[CHEVROLET_BOLT_ACC_2022_2023]`
sigmoid+linear fit (`left=[2.6531724862969748, 1.1, 0.1919764879840985, 0.0]`,
`right=[2.7031724862969746, 1.0, 0.1469764879840985, 0.0]`). So no live/route
`torque_params` lookup was needed -- simplifies the original plan. Wrote
`.scratch/check_torque_ceiling.py`, which reimplements that exact curve-build
math (verified line-for-line against `interface.py:180-198`) and evaluates
`lateral_accel_from_torque(±steer_max=±1.0)`:

```
Max-torque lateral-accel ceiling: [-3.403, +2.355] m/s^2
```

(asymmetric because the left/right siglin fits differ slightly -- expected,
matches the existing left/right split elsewhere in this tune).

**(b) Compared against the actual maneuver -- first naively, then correctly.**
First pass: dumped `desiredLateralAccel`/`actualLateralAccel` for the
7.14-7.68s dip directly against the ceiling. Peak values were tiny relative
to the ceiling (peak `|desired_la|`=0.758, `|actual_la|`=0.730, vs.
ceiling ±2.355/-3.403 -- **~1.6-2x headroom**). Taken at face value this says
"not torque-limited, plenty of margin" -- **but this is the wrong quantity to
check**, and comparing it alone would have been a wrong conclusion.

The quantity that's actually clipped against that ceiling is
`self.pid.control = clip(p + i + f + d, neg_limit, pos_limit)`
(`common/pid.py:56-60`), where `pos_limit`/`neg_limit` are set from exactly
this ceiling by `LatControlTorque.update_limits()`
(`latcontrol_torque.py:221-223`) -- **not** `desired_la`/`actual_la`
themselves. `p`/`i`/`f`/`d` are logged raw (pre-clip) in `torqueState`, so
their sum can legitimately exceed the ceiling even though the clipped
`self.control` (and the resulting commanded lateral accel) cannot. Recomputed
over the full 6.5-11s window (547 samples) by summing the logged `p+i+f+d`
per sample and comparing to the ceiling:

| check | result |
|---|---|
| raw `desired_la`/`actual_la` vs ceiling | **0/547** samples exceed it (confirms the naive check above) |
| `p+i+f+d` (pre-clip PID sum) vs ceiling | **38/547 (6.9%)** samples exceed it |
| worst excess | **0.372** beyond the ceiling (at t=7.29s: sum=2.727 vs. ceiling 2.355 -- a ~16% overshoot), landing right inside the -59.3 deg dip window (finding #3) |
| peak `\|desired_la\|`/`\|actual_la\|` over the whole window | 1.004 / 1.083 -- still well under the 2.355 ceiling even at the window's overall peak |

So: **the maneuver is not torque-limited in the sense of "the car physically
cannot produce enough lateral accel for this curve at this speed"** -- there
is real, substantial headroom on that axis throughout. But it **is**
genuinely limited in a different, narrower sense: the controller's *internal
correction-effort signal* clips against that same ceiling for real, repeated
stretches, confirming finding #3's saturated-torque observation was not a
red herring.

**Why the correction signal clips despite so much physical headroom**: the
low-speed error amplification `error_with_lsf = error * (1 + low_speed_factor
/ current_kp)` (`latcontrol_torque.py:288-290`) inflates a modest raw
tracking error (`setpoint - measurement`, order ~0.4-0.7 m/s^2 through this
window) into a P term of 1.7-1.9 at these speeds (`current_kp` itself is
already large at low speed per `KP_INTERP`). Adding `f`≈1.0 on top pushes the
sum past the ~2.35 ceiling on the worst samples. This is a **P-response
amplification artifact, not a demand-vs-capability wall** -- the controller
is asking for more correction authority than the ceiling allows *in order to
close a comparatively small error quickly*, not because the curve itself
demands near-max lateral accel.

**Also resolved in passing**: `actuators.torque` (the `torq` column in
`dump_window.py`'s output) reads -1.000 during the clipped stretch even
though the clipped PID sum is at the *positive* limit (+2.355, a physical
right-turn correction per finding #8's sign convention) -- initially looked
like a sign contradiction. Root cause: `LatControlTorque.update()` explicitly
returns `-output_torque` (`latcontrol_torque.py:721`, with the inline comment
"left is positive in this convention"), i.e. `actuators.torque` is in the
opposite sign convention from `torqueState.p/i/f/d`/`desiredLateralAccel`. No
bug -- just two different sign conventions coexisting in the log, worth
remembering if cross-checking `torq` against `p/i/f/d`/`des_la` again.
Also explains why `torqueState.saturated` reads False throughout (finding
#3's earlier open item): `_check_saturation` (`latcontrol.py:26-32`) is a
debounced flag requiring `sat_time` to accumulate to `sat_limit` before
firing; the clipping here happens in scattered short bursts (order 0.05-0.15s
each per the 6.9%-of-547-samples spread), which plausibly never accumulates
enough continuous `sat_time` to cross that threshold even though instantaneous
clipping is real. Not independently verified against `sat_limit`'s actual
value -- consistent with, not proven by, the numbers gathered here.

**Consequence for finding #9**: this explains *why* doubling
`FF_GAIN_LEFT`/`TURN_IN_BOOST_LEFT` did nothing to the saturated stretch --
it was never going to. That fix targets the `f` term, which finding #9 showed
moves by only ~0.02-0.05 in this lateral-accel range (too weakly gated by
`onset`/`cutoff`/`low_speed_factor`) -- nowhere near enough to offset a 0.37
control-sum excess that's mostly coming from the *amplified P term*, not `f`.
This isn't new evidence that FF is irrelevant, though: finding #6's ~0.2-0.24
FF under-prediction, if actually closed, would shrink the raw tracking error
feeding into `error_with_lsf` -- and because that error gets amplified before
becoming `p`, even a moderate error reduction could disproportionately reduce
the P-term excess. That's a **materially different, untested mechanism**
from finding #9's knob (which acts on `f` directly, weakly, in this regime)
-- not yet prototyped.

**Caveats**:
- `low_speed_factor` value itself wasn't pulled/logged directly this
  session -- inferred its effect from the `KP_INTERP` schedule and the
  observed `p` magnitudes, not measured directly.
- Did not attempt a closed-loop or plant-model simulation -- everything here
  is still open-loop arithmetic on the real logged `p/i/f/d`/error values,
  same caveat as finding #9.
- The "worth revisiting a bigger low-speed FF fix" and "ease
  `error_with_lsf` amplification" directions floated above are candidate
  next steps, not validated conclusions -- neither has been sized or
  prototyped.

### 11. Prototyped candidate fix (a) (additive low-speed FF correction) -- open-loop check shows it does NOT reduce (and naively appears to worsen) the pre-clip control-signal excess; real validation needs a closed-loop/plant model this investigation doesn't have

Built the fix (a) design agreed in the previous NEXT STEP section: a new,
v_ego-gated, ADDITIVE FF correction (distinct from finding #9's ruled-out
multiplicative knob), sized to cancel finding #6's measured bias by band
(`np.interp` over `[2.5,6,8,10,12]` m/s -> `[0.067,0.156,0.198,0.121,0.0]`
correction magnitude, signed by turn direction, tapering to 0 by 12 m/s so
the validated-good highway range is untouched).

**First attempt (`.scratch/prototype_bolt_ff_additive_fix.py`): open-loop
replay of the real `LatControlTorque.update()`, same pattern as finding #9's
prototype, patched via a monkeypatch on `get_bolt_2022_2023_ff_scale`.
Abandoned -- found a real fidelity bug in the replay harness itself**, not
in the candidate: even the *unpatched/stock* replay pass didn't reproduce
ground truth -- at t=7.14s the replay computed `p=5.18`, `sum=5.83` versus
the REAL logged `p=1.67`, `sum=2.44` (finding #10's numbers). `f` was also
off (0.65 replay vs. 0.97 real, matching finding #9's own stock replay
number exactly -- so that specific gap was already latent in finding #9's
harness too, just not flagged as a magnitude-fidelity problem at the time).
Root cause not chased down given time budget (candidates: cold-started PID
integrator state since the controller object is only built ~2.3s into the
segment when `carParams` first appears, vs. the real device's continuous
history from vehicle startup; possible `liveParameters`/`roll` staleness;
or something in how `error_with_lsf`'s `low_speed_factor` term is computed
diverging from production). **Whatever the cause, this replay harness
(and by extension `prototype_bolt_ff_tune.py`'s finding #9 results) should
be treated as only qualitatively indicative on `f`, not quantitatively
trustworthy on `p`/`sum`** -- worth a fresh investigation before leaning on
this replay method again for anything involving the P term.

**Second attempt (`.scratch/prototype_bolt_ff_additive_fix_real_log.py`):
sidestepped the replay bug entirely** by reading REAL logged `p/i/f/d`
directly (ground truth, no replay) and adding the candidate correction to
`f` only, leaving `p`/`i`/`d` untouched -- justified the same way finding
#9 justified isolating its own FF-only counterfactual: `error = setpoint -
measurement` depends only on the real logged setpoint/measurement, neither
of which an FF-only correction touches, so `p`/`i`/`d` are provably
unaffected by construction. This reproduced finding #10's stock numbers
almost exactly as a sanity check (max excess 0.372 at t=7.29, matching
finding #10's real-log measurement precisely) -- confirms this method is
sound where the replay wasn't.

**Result: the candidate correction makes the measured control-signal excess
worse, not better** -- clipping fraction over the 6.5-11s window went from
8.5% (38/448, stock) to 12.9% (58/448, candidate); max excess grew from
0.372 to 0.530, both peaking at the same t=7.29s sample.

**Why, and what this does/doesn't mean**: this is NOT evidence the real fix
would make the car worse. It's a structural limitation of single-instant
open-loop counterfactuals, the same one finding #9 already flagged but
which manifests differently here. Fix (a)'s actual theory of benefit is a
**closed-loop, cross-time** effect: a better FF estimate, applied
continuously, would let `measurement` track `setpoint` more closely as the
maneuver unfolds, which would make the REAL `error` (and therefore the REAL
`p`) smaller at each subsequent instant, than what's in the log today.
Neither open-loop method can represent that -- both hold `p`/`i` fixed at
whatever they actually were under the STOCK (under-FF'd) trajectory, and
then ask "what if `f` alone had been bigger, holding everything else
frozen" -- which can only ever show additional commanded effort stacking on
top of an unchanged (already-compensating) `p`, never the reduced `p` that
the fix is actually supposed to produce. This is a **general ceiling on
what any single-instant open-loop check can tell us about this class of
fix** -- confirmed twice now, by two different, methodologically-sound
approaches with the same underlying limitation, not a coincidence of one
buggy script.

**Consequence**: validating fix (a)'s real effect requires either (i) a
genuine closed-loop plant-model resimulation (out of scope, no such tool
exists in this investigation), or (ii) an actual on-vehicle test. Offline
open-loop tooling, as built so far, has now hit its ceiling for
distinguishing between fix (a) and fix (b) (`error_with_lsf` amplification)
-- neither can be meaningfully open-loop-validated for its *intended*
effect, only for effects that don't depend on changing the error trajectory
itself (which is exactly what both candidate fixes are trying to do).

**Not yet decided**: whether to (1) build a minimal closed-loop
plant/bicycle-model resimulation to actually test this class of fix
offline (nontrivial new tooling, not attempted), (2) ship fix (a) on
physical/first-principles reasoning + finding #6's real residual-bias
evidence alone and validate on-vehicle, or (3) reconsider the
planner-side option (item #7) despite the earlier reasoning against it,
since it's the one candidate whose benefit (reducing the demand the
controller has to track in the first place) doesn't require a closed-loop
model to reason about -- easing the curvature/jerk ramp directly reduces
`setpoint`'s rate of change, which is visible and measurable without
needing to resimulate the plant.

### 12. Root-caused the replay harness bug from finding #11: a StarPilot-specific "SteerKP" override flattens the low-speed P-gain curve at runtime, and every replay script in this investigation has been silently ignoring it

Chased down why the open-loop replay's own *stock* pass didn't reproduce
real logged `p` (5.18 replayed vs. 1.67 real at t=7.14s, finding #11).
Triangulated algebraically first: solving for the `current_kp` value that
would make BOTH the real logged `error` (=`error_with_lsf`, confirmed via
the cereal schema and `pid_log.error = float(error_with_lsf)`) and the real
logged `p` self-consistent gives `current_kp ≈ 0.60`, essentially flat
across this whole near-constant-speed stretch (v_ego 5.86-6.01 m/s) --
**not** the ~9.3 that `np.interp(v_ego, INTERP_SPEEDS, KP_INTERP)` (the
curve defined in `latcontrol_torque.py`) gives at this speed. Ruled out a
stale-code mismatch first (`git log 2c47bf1f47e2..HEAD` for
`latcontrol_torque.py`, `common/pid.py`, and `drive_helpers.py` -- zero
diffs, so the code that generated this log is byte-identical to what's
checked out now).

**Root cause found**: `selfdrive/controls/controlsd.py:448`, called every
cycle from `ControlsD.update()` (a different method than `LatControlTorque
.update()`, which is all any replay script in this investigation has ever
called):
```python
if hasattr(self.LaC, "pid") and self.CP.lateralTuning.which() != "pid":
  self.LaC.pid._k_p = self.starpilot_toggles.steerKp
```
`starpilot/common/starpilot_variables.py:790` builds `steerKp` as a
**single-breakpoint** array: `toggle.steerKp = [[0], [self.get_value(
"SteerKP", ..., default=KP, ...)]]` (`KP=0.6`, `latcontrol_torque.py`'s
own high-speed-endpoint constant -- `starpilot_variables.py:716`). Fed into
`np.interp(v_ego, [0], [value])`, that returns `value` at **every** speed,
flat -- this is a StarPilot advanced-lateral-tuning UI slider ("SteerKP",
`selfdrive/ui/layouts/settings/starpilot/lateral.py:305`) that doesn't
adjust the built-in `KP_INTERP` low-speed-boost curve (250 at 1 m/s tapering
to 0.6 at 30 m/s) -- **it replaces it entirely** with one flat scalar, every
cycle, for every torque-tuned car. Default value happens to equal `KP`
exactly, matching what the real log implied.

**Consequence**: every replay script built in this investigation
(`prototype_bolt_ff_tune.py` finding #9, `prototype_bolt_ff_additive_fix.py`
finding #11) constructs `LatControlTorque` directly and only ever calls its
`.update()` method -- none of them replicate this `ControlsD.update()`-level
toggle application, so `self.pid._k_p` stays at its `__init__` default (the
full speed-varying `[INTERP_SPEEDS, KP_INTERP]` curve) for the entire
replay, while the real device was running with the flat override the whole
time. At v_ego≈5.9 this is a ~15x gap in the effective P-gain (9.3 vs.
0.6), which fully explains finding #11's blown-up replayed `p`.

**This is fixable, not a dead end**: any future replay script needs to also
read `starpilotPlan.starpilotToggles`'s `steerKp` field from the log (the
existing `process_starpilot_toggles()` helper already parses that JSON blob
-- it just needs to actually be applied to `controller.pid._k_p` each cycle,
matching `controlsd.py:448`, which none of the existing scripts do) before
calling `controller.update()`. Not yet implemented or re-tested -- next
session's job if the replay approach is revisited.

**Second-order finding worth flagging on its own**: this Bolt/StarPilot
build is running with an **effectively flat low-speed P-gain**
(`current_kp≈0.6` at ~6 m/s, not the ~9.3 the base `KP_INTERP` curve would
give) -- the built-in low-speed P-gain boost is structurally bypassed by
this toggle mechanism regardless of what value it's set to (single
breakpoint, always flat). That's a real, load-bearing fact about *why*
`error_with_lsf`'s low-speed amplification (finding #10's mechanism) is
doing so much of the work here: with a flat/small P-gain, the amplified
`error_with_lsf` term (via `low_speed_factor`) has to do most of the
low-speed responsiveness lifting on its own, since the gain-schedule side of
low-speed boost isn't contributing what the base code's constants would
suggest. Not yet reconciled against finding #10's numbers (which used real
logged `p/i/f/d` directly, so they're unaffected by this bug -- only the
*replay* scripts were wrong) -- but worth keeping in mind for fix (b)
(easing `error_with_lsf`) sizing, since it may now be carrying more of the
low-speed-boost load than the code alone would suggest, given `current_kp`
being flattened by this toggle.

### 13. Fix (a) shipped as an actual diff, harness bug from finding #12 fixed and re-validated -- same open-loop-limited result as finding #11, confirming the diagnosis rather than changing the plan

**Diff applied** (uncommitted, per `feedback_commit_permission`):
- `selfdrive/controls/lib/latcontrol_vehicle_tunes.py`: new constants
  `BOLT_2022_2023_LOW_SPEED_FF_CORRECTION_BP`/`_V` (the same
  `[2.5,6,8,10,12]` m/s -> `[0.067,0.156,0.198,0.121,0.0]` curve designed
  earlier) and a new function `get_bolt_2022_2023_low_speed_ff_correction
  (desired_lateral_accel, v_ego)` -- additive, signed by turn direction,
  wrapped in `_flm_vehicle_knob("gm_bolt_2022_2023.low_speed_ff_correction",
  ...)` matching the existing pattern for every other Bolt 2022/2023 knob in
  this file (so it's FLM-overridable as a flat scalar if that path is ever
  used, same as `center_taper_max` etc.).
- `selfdrive/controls/lib/latcontrol_torque.py`: one new line in the
  `bolt_2022_2023_tuned_path_active` block, right after the existing
  multiplicative `ff *= get_bolt_2022_2023_ff_scale(...)`:
  `ff += get_bolt_2022_2023_low_speed_ff_correction(setpoint, CS.vEgo)`.
- Not gated behind `bolt_2022_2023_lateral_testing_ground_active()` --
  confirmed that flag isn't actually wired into `bolt_2022_2023_tuned_path_active`
  at all currently (`bolt_2022_2023_tuned_path_active = self.is_bolt_2022_2023`,
  unconditional), so every other knob in this block already ships
  unconditionally for all Bolt 2022/2023 cars; this fix follows the same
  existing pattern rather than introducing new gating machinery.

**Fixed finding #12's replay harness bug and re-validated with it**: wrote
`.scratch/validate_shipped_fix.py`, which applies `starpilot_toggles.steerKp`
to `controller.pid._k_p` every cycle (matching `controlsd.py:448`, the piece
missing from every prior replay script), reading the real `steerKp` value
from this route's own `starpilotPlan.starpilotToggles` log messages
(confirmed present: `[[0],[0.6]]`, flat -- matches finding #12's
derivation exactly). With the harness now applying the real P-gain, the
stock-pass numbers land much closer to finding #10's real-log measurement
(5.1% clipping / 0.253 max excess replayed vs. 8.5% / 0.372 real-logged --
still not exact, some residual open-loop drift expected from ~4s of
untracked warm-up state before the window, but far more plausible than
finding #11's pre-fix replay which was off by an order of magnitude).

**Result with the corrected harness: same qualitative outcome as finding
#11** -- the shipped fix increases (not decreases) the open-loop-measured
clipping: 5.1% -> 9.8%, max excess 0.253 -> 0.412. This is **not a
regression signal** -- it's the exact same structural limitation finding
#11 already diagnosed (single-instant open-loop counterfactuals can't
represent the closed-loop error-reduction benefit this fix is designed to
produce), now confirmed to persist even after fixing the unrelated P-gain
bug. If anything, this strengthens confidence that finding #11's
"structural ceiling" framing was the right read of the situation, rather
than an artifact of that specific harness bug -- two different bugs fixed
in two different sessions, same open-loop result both times.

### 14. `/code-review low` on the diff -- two real bugs found and fixed, one process flag worth being explicit about

Ran `/code-review low` on the ~15-line diff. Three findings, all worth
recording:

1. **Unit-space mismatch (real bug, fixed).** The correction was added to
   `ff` inside the `bolt_2022_2023_tuned_path_active` block up near line
   374 -- but `ff` is still in **lateral-acceleration space** at that point
   (the code's own comment confirms: "do error correction in lateral
   acceleration space, convert at end to handle non-linear torque responses
   correctly"). The correction's magnitude was sized from finding #6's
   residual (`siglin_pred - actual_measured_steer`, a **torque-domain**
   quantity on the ±1 actuator scale). Adding a torque-scale number to a
   lateral-accel-scale variable, then passing it through the nonlinear/
   asymmetric siglin conversion later, does not produce the intended
   torque-domain offset -- the real effect on commanded torque would have
   differed from what was measured/designed. **Fixed**: moved the
   correction to apply directly to `output_torque` (torque-domain,
   `latcontrol_torque.py` ~line 556), right after `output_torque =
   self.torque_from_lateral_accel(output_lataccel, ...)` and before the
   existing `center_output_scale`/`low_speed_center_output_limit`
   post-processing -- same domain finding #6 actually measured in, and
   still subject to the existing low-speed output safety clip.
2. **No magnitude taper near `desired_lateral_accel==0` (real bug,
   fixed).** Original version only special-cased the exact value `0.0`; any
   nonzero value, however small, got the *full* speed-interpolated
   correction via `copysign`, creating a discontinuous step (~±0.2 swing)
   right across zero -- exactly the kind of near-center injected
   discontinuity the whole separate Ioniq 6 chatter investigation
   (`feedback_chatter_metric_scope` memory) spent significant effort
   fighting, and a real regression risk here. **Fixed**: reused the sibling
   `get_bolt_2022_2023_ff_scale`'s own onset sigmoid
   (`BOLT_2022_2023_FF_ONSET`/`_WIDTH`, centered ~0.12) to fade the
   correction's magnitude toward 0 near center, matching this file's
   existing precedent for how near-zero lateral accel is handled elsewhere
   in this exact function. Not a perfect fix (there's still a small
   residual step exactly at the `==0.0` boundary, same as the sibling
   function's own `return 1.0` special-case at that exact point) but a
   large reduction, consistent with how the rest of this tune already
   handles this boundary.
3. **Process flag, not a code bug**: the reviewer correctly pointed out
   that shipping this into the live control path (even gated only by an
   FLM override, unconditional otherwise) sits in tension with finding #11
   explicitly measuring the open-loop clipping getting *worse*, and this
   doc's own "Not yet decided" language a few sections up. Worth being
   explicit about, not glossing over: **this diff is written and its two
   real bugs are fixed, but it has NOT been decided that it should ship**.
   The reasoning for writing it anyway (discussed with the user) was "get
   it into reviewable form and validate on-vehicle, since offline open-loop
   testing has hit a structural ceiling it cannot get past" -- that's a
   deliberate choice to accept on-vehicle testing as the validation path,
   not a resolution of whether the fix is correct. **The diff should not be
   committed or deployed without the user explicitly confirming they want
   to proceed on that basis**, per the standing `feedback_commit_permission`
   rule and given this specific open question.

### 15. Fix (a) driven on-vehicle -- did NOT help, reverted

Per finding #14's process flag, the diff from finding #13 was committed
(`a405bedf3`, "Add Bolt 2022/2023 low-speed FF correction for measured
torque under-prediction") and validated on-vehicle by driving. **Result:
the tune did not help.** Per the user's direction on 2026-09-18, the fix
was reverted (`git revert a405bedf3`, commit `13a7e381d` on
`wat-bolt-analysis`) -- `get_bolt_2022_2023_low_speed_ff_correction` and
its `BOLT_2022_2023_LOW_SPEED_FF_CORRECTION_BP`/`_V` constants are gone
again from `latcontrol_torque.py`/`latcontrol_vehicle_tunes.py`.

This doesn't invalidate finding #6's underlying residual measurement (the
siglin curve under-predicting torque in the 5-11 m/s band is still real
and still structurally unvalidated by the live estimator) -- it means
*this specific additive correction*, sized and shaped the way it was, was
not the right fix, or the open-loop-vs-closed-loop gap flagged in findings
#11/#13 masked a sizing/shape problem that on-vehicle testing couldn't
distinguish from the underlying approach being wrong. Fix (b) (easing the
`error_with_lsf` P-amplification instead of adding more FF) and item #7
(planner-side demand easing) remain the untried directions -- see the
NEXT STEP note above.

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
   - ~~Decide what a fix would even look like~~ **ATTEMPTED then WALKED BACK,
     see finding #8's correction and finding #9** -- ruled out re-fitting
     `NON_LINEAR_TORQUE_PARAMS` and lowering `MIN_VEL` (both too
     broad-blast-radius / undersupported by data). Initially thought the
     existing `get_bolt_2022_2023_ff_scale` turn-in-boost mechanism's
     right-turn constants were the fix target -- **that was backwards** (the
     "_LEFT"/"_RIGHT" naming is keyed on the sign of `desired_lateral_accel`,
     which is inverted from physical turn direction for this controller; the
     right-turn oscillation actually runs on the already-larger `_LEFT`
     constants). Corrected and prototyped open-loop in finding #9: even
     doubling `FF_GAIN_LEFT`/`TURN_IN_BOOST_LEFT` produces only a ~2-9%
     change in the FF term and does **nothing** to the actual commanded
     torque during the worst (saturated) third of the window, since the
     actuator is already clipped at its ceiling there regardless of FF
     magnitude. **Still an open question**, now reframed: is this maneuver
     genuinely actuator-torque-limited at this speed/curvature (in which
     case a bigger FF fix has a hard ceiling), and would attacking the
     planner-side demand (desired curvature rate at low speed) help more
     than any FF-side knob?
3. ~~Check steerActuatorDelay / P-gain behavior~~ **DONE, see finding #7** --
   delay compensation checked out fine (live-estimated, larger than static
   default, measurement tracks setpoint within ~60ms). Ruled out as the
   cause; P is just doing its job against an undersized FF (finding #6) plus
   genuine actuator saturation (finding #3).
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
6. ~~Check whether this maneuver is genuinely actuator-torque-limited~~
   **DONE, see finding #10.** Answer is nuanced, not a plain yes/no: the
   physical lateral accel needed has 1.6-2x+ headroom against the torque
   ceiling (not physically limited), but the PID's internal `p+i+f+d`
   correction signal does clip against that same ceiling in real, repeated
   bursts (6.9% of the 6.5-11s window, worst excess 0.372/~16% over) --
   driven by low-speed P-error amplification (`error_with_lsf`), not by the
   curve demanding near-max lateral accel. This also explains finding #9's
   near-null result: `FF_GAIN_LEFT`/`TURN_IN_BOOST_LEFT` act on `f`, which
   moves far too little (~0.02-0.05) to offset a 0.37 control-sum excess
   dominated by the amplified `p` term -- so that specific knob is ruled out
   more conclusively now, not reopened.
7. **Planner/demand-side angle** -- unchanged from before, still not started,
   still a live candidate: easing the planner's curvature/jerk ramp at low
   speed would shrink the raw tracking error feeding `error_with_lsf`
   directly, attacking finding #10's root cause from upstream. Would need to
   look at the upstream planner output (`desiredCurvature`) independent of
   the torque controller.
8. ~~If an FF-side fix is still wanted...~~ **Superseded by finding #10** --
   don't pursue `FF_GAIN_LEFT`/`TURN_IN_BOOST_LEFT` further, it's confirmed
   too weak in this regime regardless of FLM-override availability. Two new,
   not-yet-prototyped candidates opened by finding #10 instead:
   - **8a.** A more substantial low-speed FF fix sized to finding #6's actual
     ~0.2-0.24 residual gap (bigger than any turn-in-boost-knob delta) --
     previously shelved as "too broad-blast-radius" when the candidate was
     re-fitting `NON_LINEAR_TORQUE_PARAMS` / lowering `torqued.py`'s
     `MIN_VEL`; worth revisiting now that there's a concrete, measured
     mechanism (shrinking `error_with_lsf`'s input) tying a bigger FF fix to
     less P-term saturation, not just a residual-table observation.
   - **8b.** Ease the `error_with_lsf` low-speed amplification factor itself
     (`latcontrol_torque.py:288-290`) so a modest tracking error doesn't get
     inflated into a correction that exceeds the ceiling -- untried, would
     need scoping against how this factor is shared with other cars/speeds
     before touching it (it's not Bolt-specific code).

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
