#!/usr/bin/env python3
"""Open question #3 (bolt-tuning-916.md): check steerActuatorDelay/lat_delay behavior
around the 137/10 t=6.5-11s ringdown.

Two things this reports:
1. The `lat_delay` actually used by LatControlTorque.update() at each sample
   (`liveDelay.lateralDelay + LAT_SMOOTH_SECONDS`, per controlsd.py:788/292) --
   including whether liveDelay was actually "estimated" or fell back to the
   static CP.steerActuatorDelay-derived initial_lag (lagd.py).
2. An empirical cross-correlation lag between the delay-compensated setpoint
   (`torqueState.desiredLateralAccel`) and the instantaneous measurement
   (`torqueState.actualLateralAccel`) over the oscillation window. If delay
   compensation were exactly right, measurement should track setpoint with
   ~zero lag. A positive best-fit lag (measurement trails setpoint) means the
   real actuator delay is bigger than what's being compensated for --
   under-compensation, which is exactly the kind of thing that turns a P
   term chasing a "gap" FF (finding #6) into overshoot instead of a clean
   correction. A negative lag would mean over-compensation.
"""
import sys

import numpy as np

from openpilot.tools.lib.logreader import LogReader, ReadMode

# Hardcoded rather than imported from selfdrive/modeld/modeld.py: that module
# pulls in cereal.messaging -> msgq's compiled ipc_pyx.so, which in this
# worktree is an aarch64 device binary (see project_native_ext_testing memory)
# and isn't importable here. Value confirmed via
# `grep -n "LAT_SMOOTH_SECONDS\s*=" selfdrive/modeld/modeld.py` -> 0.1.
# controlsd.py:292 returns this constant for brand="gm" (Bolt), since GM isn't
# rivian/subaru (the only brands with a speed-varying override).
LAT_SMOOTH_SECONDS = 0.1

path = sys.argv[1]
t_start = float(sys.argv[2])
t_end = float(sys.argv[3])
pad = float(sys.argv[4]) if len(sys.argv) > 4 else 3.0

lr = LogReader(path, default_mode=ReadMode.RLOG, sort_by_time=True)

t0 = None
car_params = None
delay_rows = []   # (t, lateralDelay, status, estimate, estimate_std)
sample_rows = []  # (t, setpoint, measurement, v_ego)


for msg in lr:
  try:
    which = msg.which()
  except Exception:
    continue
  if which == "carParams" and car_params is None:
    car_params = msg.carParams
    continue
  if which == "controlsState":
    if t0 is None:
      t0 = msg.logMonoTime / 1e9
  if t0 is None:
    continue
  t_rel = msg.logMonoTime / 1e9 - t0

  if which == "liveDelay":
    if not (t_start - pad <= t_rel <= t_end + pad):
      continue
    ld = msg.liveDelay
    delay_rows.append((t_rel, ld.lateralDelay, str(ld.status), ld.lateralDelayEstimate, ld.lateralDelayEstimateStd))
  elif which == "controlsState":
    if not (t_start - pad <= t_rel <= t_end + pad):
      continue
    lateral_state = msg.controlsState.lateralControlState
    if lateral_state.which() != "torqueState":
      continue
    ts = lateral_state.torqueState
    sample_rows.append((t_rel, ts.desiredLateralAccel, ts.actualLateralAccel))

print(f"static CP.steerActuatorDelay={car_params.steerActuatorDelay if car_params else float('nan')}  "
      f"LAT_SMOOTH_SECONDS={LAT_SMOOTH_SECONDS}")

print(f"\nliveDelay samples in [{t_start - pad:.1f}, {t_end + pad:.1f}]: n={len(delay_rows)}")
print(f"{'t':>7s} {'lateralDelay':>13s} {'lat_delay_used':>15s} {'status':>12s} {'estimate':>9s} {'est_std':>8s}")
for t, ld, status, est, est_std in delay_rows:
  print(f"{t:7.2f} {ld:13.4f} {ld + LAT_SMOOTH_SECONDS:15.4f} {status:>12s} {est:9.4f} {est_std:8.4f}")

if not sample_rows:
  print("\nNo controlsState/torqueState samples found in window.")
  sys.exit(0)

sample_rows.sort(key=lambda r: r[0])
t_arr = np.array([r[0] for r in sample_rows])
setpoint_arr = np.array([r[1] for r in sample_rows])
measurement_arr = np.array([r[2] for r in sample_rows])

# Resample onto a uniform 100Hz grid spanning the padded window (controlsState
# is already ~100Hz but interpolate defensively in case of dropped frames).
dt = 0.01
t_grid = np.arange(t_arr[0], t_arr[-1], dt)
setpoint_grid = np.interp(t_grid, t_arr, setpoint_arr)
measurement_grid = np.interp(t_grid, t_arr, measurement_arr)

# Restrict the correlation search to the actual event window (not the pad,
# which mostly has ~0 signal and would dilute the correlation).
mask = (t_grid >= t_start) & (t_grid <= t_end)
setpoint_evt = setpoint_grid[mask] - setpoint_grid[mask].mean()
measurement_full = measurement_grid  # keep full-res version to shift against

max_lag_s = 0.5
max_lag_n = int(max_lag_s / dt)
lags = range(-max_lag_n, max_lag_n + 1)
event_idx = np.where(mask)[0]

results = []
for lag_n in lags:
  shifted_idx = event_idx + lag_n
  if shifted_idx.min() < 0 or shifted_idx.max() >= len(measurement_full):
    continue
  meas_shifted = measurement_full[shifted_idx] - measurement_full[shifted_idx].mean()
  denom = (np.linalg.norm(setpoint_evt) * np.linalg.norm(meas_shifted))
  corr = float(np.dot(setpoint_evt, meas_shifted) / denom) if denom > 0 else 0.0
  results.append((lag_n * dt, corr))

best_lag, best_corr = max(results, key=lambda r: r[1])
zero_lag_corr = next(c for lg, c in results if abs(lg) < 1e-9)

print(f"\nCross-correlation of actual_la(t + lag) vs setpoint(t) over [{t_start},{t_end}]s "
      f"(event window only, pad used just to allow shifting):")
print(f"  correlation at lag=0:        {zero_lag_corr:.4f}")
print(f"  best-fit lag:                {best_lag:+.3f}s  (corr={best_corr:.4f})")
print(f"  interpretation: positive lag => measurement trails setpoint by that much MORE than")
print(f"  the delay compensation already assumes (real delay > compensated lat_delay,")
print(f"  i.e. under-compensation). Negative => over-compensation.")

print(f"\n{'lag(s)':>7s} {'corr':>7s}")
for lg, c in results:
  if abs(lg) <= 0.3 + 1e-9:
    marker = " <-- best" if abs(lg - best_lag) < 1e-9 else ""
    print(f"{lg:7.2f} {c:7.4f}{marker}")
