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
