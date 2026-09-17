#!/usr/bin/env python3
"""
Check the siglin FF torque-curve fit specifically in the low-speed window of
the Bolt lateral-oscillation maneuver (see .scratch/bolt-tuning-916.md).

Production TorqueEstimator (selfdrive/locationd/torqued.py) only accumulates
points when v_ego > MIN_VEL (15 m/s) -- our maneuver (v_ego 4.3-10.2 m/s) is
entirely below that, so analyze_bolt_lateral.py's existing torque-map-residual
check has ZERO coverage there. This script reimplements the same
measured-lateral-accel-from-yaw-rate + steer-torque pairing logic as
TorqueEstimator.handle_log, but with the MIN_VEL gate removed and restricted
to a specific [t_start, t_end] window (t_rel convention: seconds since first
controlsState message), then runs the same siglin_torque residual check
analyze_bolt_lateral.py uses.
"""
import sys
from collections import deque, defaultdict

import numpy as np

from openpilot.tools.lib.logreader import LogReader, ReadMode
from openpilot.common.constants import ACCELERATION_DUE_TO_GRAVITY
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.locationd.helpers import PoseCalibrator, Pose
from opendbc.car.gm.interface import NON_LINEAR_TORQUE_PARAMS

sys.path.insert(0, "tools/tuning")
from analyze_bolt_lateral import siglin_torque  # noqa: E402

HISTORY = 5  # secs, matches torqued.py
STEER_MIN_THRESHOLD = 0.02  # matches torqued.py
MIN_ENGAGE_BUFFER = 2  # secs, matches torqued.py

path = sys.argv[1]
t_start = float(sys.argv[2])
t_end = float(sys.argv[3])

lr = LogReader(path, default_mode=ReadMode.RLOG, sort_by_time=True)

hist_len = int(HISTORY / DT_MDL)
raw_points = defaultdict(lambda: deque(maxlen=hist_len))
calibrator = PoseCalibrator()
lag = 0.0
car_params = None

t0 = None  # first controlsState mono time, seconds -- the t_rel=0 reference
points = []  # (t_rel, v_ego, steer, lateral_acc)

for msg in lr:
  try:
    which = msg.which()
  except Exception:
    continue

  if which == "carParams" and car_params is None:
    car_params = msg.carParams
    continue

  if which == "controlsState" and t0 is None:
    t0 = msg.logMonoTime / 1e9

  t = msg.logMonoTime / 1e9

  if which == "carControl":
    raw_points["carControl_t"].append(t + lag)
    raw_points["lat_active"].append(msg.carControl.latActive)
  elif which == "carOutput":
    raw_points["carOutput_t"].append(t + lag)
    raw_points["steer_torque"].append(-msg.carOutput.actuatorsOutput.torque)
  elif which == "carState":
    raw_points["carState_t"].append(t + lag)
    raw_points["vego"].append(msg.carState.vEgo)
    raw_points["steer_override"].append(msg.carState.steeringPressed)
  elif which == "liveCalibration":
    calibrator.feed_live_calib(msg.liveCalibration)
  elif which == "liveDelay":
    lag = msg.liveDelay.lateralDelay
  elif which == "livePose":
    if t0 is None:
      continue
    if len(raw_points["steer_torque"]) != hist_len:
      continue
    t_rel = t - t0
    if not (t_start <= t_rel <= t_end):
      continue

    device_pose = Pose.from_live_pose(msg.livePose)
    calibrated_pose = calibrator.build_calibrated_pose(device_pose)
    yaw_rate = calibrated_pose.angular_velocity.yaw
    roll = device_pose.orientation.roll

    lat_active = np.interp(
      np.arange(t - MIN_ENGAGE_BUFFER, t + lag, DT_MDL),
      raw_points["carControl_t"], raw_points["lat_active"],
    ).astype(bool)
    steer_override = np.interp(
      np.arange(t - MIN_ENGAGE_BUFFER, t + lag, DT_MDL),
      raw_points["carState_t"], raw_points["steer_override"],
    ).astype(bool)
    vego = np.interp(t, raw_points["carState_t"], raw_points["vego"])
    steer = np.interp(t, raw_points["carOutput_t"], raw_points["steer_torque"]).item()
    lateral_acc = (vego * yaw_rate) - (np.sin(roll) * ACCELERATION_DUE_TO_GRAVITY).item()

    # Same gates as production EXCEPT vego > MIN_VEL (that's the one we're bypassing)
    # and EXCEPT the LAT_ACC_THRESHOLD<=1 cap used for the *fit* buckets (not the
    # all_torque_points list) -- kept here for visibility, not to exclude points.
    if len(lat_active) == 0 or not all(lat_active) or any(steer_override) or abs(steer) <= STEER_MIN_THRESHOLD:
      continue

    points.append((t_rel, float(vego), steer, float(lateral_acc)))

if car_params is None:
  raise RuntimeError("No carParams found in route.")

print(f"carFingerprint={car_params.carFingerprint}")
print(f"window t_rel=[{t_start},{t_end}]  n_points={len(points)}")

if not points:
  print("No qualifying points in window (check lat_active/steer_override/steer-threshold gating).")
  sys.exit(0)

arr = np.array(points)  # columns: t_rel, vego, steer, lateral_acc
print(f"\n{'t':>6s} {'v_ego':>6s} {'steer':>7s} {'lat_acc':>8s} {'pred':>8s} {'err':>7s}")
params = NON_LINEAR_TORQUE_PARAMS.get(car_params.carFingerprint)
if params is None:
  print(f"No siglin torque params configured for {car_params.carFingerprint}.")
  sys.exit(0)

preds = np.array([siglin_torque(la, params) for la in arr[:, 3]])
errs = preds - arr[:, 2]
for i in range(len(points)):
  print(f"{arr[i,0]:6.2f} {arr[i,1]:6.2f} {arr[i,2]:7.4f} {arr[i,3]:8.4f} {preds[i]:8.4f} {errs[i]:7.4f}")

print(f"\nWindow residual summary: n={len(points)} mae={np.mean(np.abs(errs)):.4f} bias={np.mean(errs):+.4f}")
for name, mask in (("left(lat>=0)", arr[:, 3] >= 0.0), ("right(lat<0)", arr[:, 3] < 0.0)):
  if np.any(mask):
    print(f"  {name:14s} n={int(mask.sum()):4d} mae={np.mean(np.abs(errs[mask])):.4f} bias={np.mean(errs[mask]):+.4f}")

# Linearized correction, same as summarize_torque_points does for the whole segment
x = np.column_stack([preds, np.ones(len(preds))])
y = arr[:, 2]
scale, offset = np.linalg.lstsq(x, y, rcond=None)[0]
fit = scale * preds + offset
print(f"\nLinearized correction against current siglin (this window only):")
print(f"  scale={scale:.4f} offset={offset:+.4f} mae_fit={np.mean(np.abs(fit - y)):.4f}  (vs mae_raw={np.mean(np.abs(errs)):.4f})")

# Speed-bucketed bias check (appended, only runs on the SAME points collected above)
print("\nSpeed-bucketed bias (this window's points):")
for lo, hi in ((0, 5), (5, 7), (7, 9), (9, 11), (11, 100)):
  m = (arr[:, 1] >= lo) & (arr[:, 1] < hi)
  if np.any(m):
    print(f"  v_ego [{lo:2d},{hi:3d}) n={int(m.sum()):4d} mae={np.mean(np.abs(errs[m])):.4f} bias={np.mean(errs[m]):+.4f}")
