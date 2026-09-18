#!/usr/bin/env python3
"""Check whether the 137/10 dip (t~7.14-7.68s) is actuator-torque-limited.

Computes the Bolt 2022/2023's max-torque lateral-accel ceiling from the real
production siglin curve (CI.lateral_accel_from_torque(), same call site as
LatControlTorque.update_limits()) and compares it against the
actual/desired lateral accel the controller needed during the dip.

Usage:
  uv run python3 .scratch/check_torque_ceiling.py <rlog_path> <t_start> <t_end>
"""
import sys

from openpilot.tools.lib.logreader import LogReader, ReadMode
from opendbc.car.gm.interface import CarInterface, get_nonlinear_torque_params
from opendbc.car.gm.values import CAR

path = sys.argv[1]
t_start = float(sys.argv[2])
t_end = float(sys.argv[3])

fingerprint = CAR.CHEVROLET_BOLT_ACC_2022_2023
nl_params = get_nonlinear_torque_params(fingerprint)
print(f"NON_LINEAR_TORQUE_PARAMS[{fingerprint}] = {nl_params}")

# Reproduce CarInterface.get_lataccel_torque_siglin()'s curve build without
# constructing a full CarInterface (needs CP/CarParams plumbing we don't have
# handy here) -- same math, verified against interface.py:180-198.
import numpy as np
from math import exp, fabs

def torque_from_lateral_accel_siglin_func(lateral_acceleration):
  side_key = "left" if lateral_acceleration >= 0 else "right"
  a, b, c, d = nl_params[side_key]
  sig_input = a * lateral_acceleration
  sig = np.sign(sig_input) * (1 / (1 + exp(-fabs(sig_input))) - 0.5)
  return float((sig * b) + (lateral_acceleration * c) + d)

lataccel_values = np.arange(-5.0, 5.0, 0.01)
torque_values = [torque_from_lateral_accel_siglin_func(x) for x in lataccel_values]

def lateral_accel_from_torque_siglin(torque):
  return float(np.interp(torque, torque_values, lataccel_values))

steer_max = 1.0
max_la = lateral_accel_from_torque_siglin(steer_max)
min_la = lateral_accel_from_torque_siglin(-steer_max)
print(f"\nMax-torque lateral-accel ceiling (steer_max={steer_max}): "
      f"[{min_la:.3f}, {max_la:.3f}] m/s^2")

# Now pull actual/desired lateral accel over the window from the log.
lr = LogReader(path, default_mode=ReadMode.RLOG, sort_by_time=True)
t0 = None
rows = []
for msg in lr:
  try:
    which = msg.which()
  except Exception:
    continue
  if which != "controlsState":
    continue
  if t0 is None:
    t0 = msg.logMonoTime / 1e9
  t_rel = msg.logMonoTime / 1e9 - t0
  if not (t_start <= t_rel <= t_end):
    continue
  lateral_state = msg.controlsState.lateralControlState
  if lateral_state.which() != "torqueState":
    continue
  ts = lateral_state.torqueState
  rows.append((t_rel, ts.desiredLateralAccel, ts.actualLateralAccel))

print(f"\nn_samples in [{t_start},{t_end}]: {len(rows)}")
print(f"{'t':>6s} {'des_la':>8s} {'act_la':>8s}  vs ceiling[{min_la:.2f},{max_la:.2f}]")
worst_des = max(rows, key=lambda r: abs(r[1])) if rows else None
worst_act = max(rows, key=lambda r: abs(r[2])) if rows else None
for t, des, act in rows:
  flag = ""
  if des > max_la or des < min_la:
    flag += " DES>CEIL"
  if act > max_la or act < min_la:
    flag += " ACT>CEIL"
  print(f"{t:6.2f} {des:8.3f} {act:8.3f}{flag}")

if rows:
  print(f"\nPeak |desired_la| in window: {worst_des[1]:.3f} at t={worst_des[0]:.2f}")
  print(f"Peak |actual_la|  in window: {worst_act[2]:.3f} at t={worst_act[0]:.2f}")
  print(f"Ceiling: [{min_la:.3f}, {max_la:.3f}]")
  gap_des = max(0.0, worst_des[1] - max_la, -min_la - worst_des[1])
  gap_act = max(0.0, worst_act[2] - max_la, -min_la - worst_act[2])
  print(f"Headroom (ceiling - peak desired_la, positive=has headroom): "
        f"{max_la - worst_des[1]:.3f}" if worst_des[1] >= 0 else
        f"Headroom: {worst_des[1] - min_la:.3f}")
