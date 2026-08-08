#!/usr/bin/env python3
import argparse
import math
from dataclasses import dataclass

import numpy as np

from openpilot.tools.lib.logreader import LogReader, ReadMode
from openpilot.selfdrive.locationd.torqued import TorqueEstimator
from opendbc.car.gm.interface import NON_LINEAR_TORQUE_PARAMS
from openpilot.selfdrive.controls.lib.latcontrol_torque import (
  BOLT_CARS,
  DEADZONE_BOOST_LAT_ACCEL,
  FF_SCALE_BLEND_LAT_ACCEL,
)


@dataclass
class ControlSample:
  v_ego: float
  steering_pressed: bool
  lat_active: bool
  saturated: bool
  roll_rad: float
  actual_la: float
  desired_la: float
  desired_jerk: float
  p_term: float
  i_term: float
  f_term: float
  torque_cmd: float
  mono_time: int
  d_term: float
  curvature: float
  torque_active: bool


LOW_ROLL_THRESHOLD_DEG = 1.5

# mirrors latcontrol_torque.py:47-48 and controlsd.py:105
DT_CTRL = 0.01
UNWIND_D_DES_THRESHOLD = -1.0
UNWIND_LAT_ACCEL_NEAR_ZERO = 0.3

PHASE_LAT_ACCEL_DEADBAND = 0.1  # m/s^2
PHASE_RATE_DEADBAND = 0.2  # m/s^3


def reconstruct_unwind(samples: list[ControlSample]) -> tuple[np.ndarray, np.ndarray]:
  n = len(samples)
  unwind = np.zeros(n, dtype=bool)
  d_des_rate = np.full(n, np.nan, dtype=float)

  prev_setpoint = 0.0

  for k, s in enumerate(samples):
    if not s.torque_active:
      prev_setpoint = 0.0
      unwind[k] = False
      d_des_rate[k] = np.nan
      continue

    # Gap guard: k == 0 or prev sample inactive or time gap > 1.5 * DT_CTRL
    if k == 0 or not samples[k - 1].torque_active or ((s.mono_time - samples[k - 1].mono_time) / 1e9 > 1.5 * DT_CTRL):
      unwind[k] = False
      d_des_rate[k] = np.nan
      prev_setpoint = s.desired_la
      continue

    rate = (s.desired_la - prev_setpoint) / DT_CTRL
    d_des_rate[k] = rate
    unwind[k] = (rate < UNWIND_D_DES_THRESHOLD) and (abs(s.desired_la) < UNWIND_LAT_ACCEL_NEAR_ZERO)
    prev_setpoint = s.desired_la

  return unwind, d_des_rate


def summarize_unwind_reconstruction(samples: list[ControlSample], unwind: np.ndarray, d_des_rate: np.ndarray) -> None:
  if not samples:
    return

  torque_active_mask = np.array([s.torque_active for s in samples], dtype=bool)
  steering_pressed = np.array([s.steering_pressed for s in samples], dtype=bool)
  desired_la = np.array([s.desired_la for s in samples])
  actual_la = np.array([s.actual_la for s in samples])
  i_term = np.array([s.i_term for s in samples])
  f_term = np.array([s.f_term for s in samples])
  jerk = np.array([s.desired_jerk for s in samples])
  roll_rad = np.array([s.roll_rad for s in samples])

  active_count = int(torque_active_mask.sum())
  if active_count == 0:
    print("\nUnwind reconstruction:\n  No active samples found.")
    return

  overall_unwind_frac = np.mean(unwind[torque_active_mask]) if active_count > 0 else 0.0

  classified_base = (
    torque_active_mask
    & (~steering_pressed)
    & np.isfinite(d_des_rate)
    & (np.abs(desired_la) >= PHASE_LAT_ACCEL_DEADBAND)
    & (np.abs(d_des_rate) >= PHASE_RATE_DEADBAND)
  )

  entering_left_mask = classified_base & (desired_la > 0) & (d_des_rate > 0)
  exiting_left_mask = classified_base & (desired_la > 0) & (d_des_rate < 0)
  entering_right_mask = classified_base & (desired_la < 0) & (d_des_rate < 0)
  exiting_right_mask = classified_base & (desired_la < 0) & (d_des_rate > 0)

  classified_count = int(
    entering_left_mask.sum() + entering_right_mask.sum() + exiting_left_mask.sum() + exiting_right_mask.sum()
  )
  unclassified_count = active_count - classified_count

  print("\nUnwind reconstruction:")
  print(
    f"  active_samples={active_count:5d} unwind_frac={overall_unwind_frac:.4f} unclassified={unclassified_count:5d}"
  )
  print("  phase            n      unwind_frac  mean|i|     bias    mean|d_des|")

  phases = (
    ("entering_left", entering_left_mask),
    ("entering_right", entering_right_mask),
    ("exiting_left", exiting_left_mask),
    ("exiting_right", exiting_right_mask),
  )

  for name, mask in phases:
    n = int(mask.sum())
    if n == 0:
      u_str, i_str, b_str, d_str = "--", "--", "--", "--"
    else:
      u_str = f"{np.mean(unwind[mask]):.4f}"
      i_str = f"{np.mean(np.abs(i_term[mask])):.4f}"
      b_str = f"{np.mean(actual_la[mask] - desired_la[mask]):+.4f}"
      d_str = f"{np.mean(np.abs(d_des_rate[mask])):.4f}"
    print(
      f"  {name:16s} {n:5d}       {u_str:>6s}   {i_str:>6s}  {b_str:>7s}        {d_str:>6s}"
    )

  ff_mask = (
    torque_active_mask
    & (~steering_pressed)
    & (np.abs(desired_la) < 0.05)
    & (np.abs(jerk) < 0.05)
    & (np.abs(desired_la - actual_la) < 0.05)
    & (np.abs(roll_rad) < math.radians(0.2))
  )
  ff_n = int(ff_mask.sum())
  if ff_n < 200:
    mean_f_str = f"{np.mean(f_term[ff_mask]):+.4f}" if ff_n > 0 else "--"
    print(f"  ff offset check: n={ff_n:5d} mean_f={mean_f_str}  (static predicts ~0.00, live predicts ~+0.33)")
    print("  verdict: n too small")
  else:
    mean_f = float(np.mean(f_term[ff_mask]))
    if abs(mean_f) < 0.15:
      verdict = "consistent with static tune"
    elif mean_f > 0.20:
      verdict = "consistent with LIVE tune"
    else:
      verdict = "INCONCLUSIVE"
    print(f"  ff offset check: n={ff_n:5d} mean_f={mean_f:+.4f}  (static predicts ~0.00, live predicts ~+0.33)")
    print(f"  verdict: {verdict}")


def siglin_torque(lat_accel: float, params: dict[str, list[float]]) -> float:
  side = "left" if lat_accel >= 0.0 else "right"
  a, b, c, d = params[side]
  sig_input = a * lat_accel
  sig = math.copysign((1.0 / (1.0 + math.exp(-abs(sig_input))) - 0.5), sig_input)
  return (sig * b) + (lat_accel * c) + d


def summarize_control_samples(samples: list[ControlSample]) -> None:
  if not samples:
    print("No lateral torque samples found.")
    return

  v = np.array([s.v_ego for s in samples])
  steering_pressed = np.array([s.steering_pressed for s in samples], dtype=bool)
  lat_active = np.array([s.lat_active for s in samples], dtype=bool)
  saturated = np.array([s.saturated for s in samples], dtype=bool)
  roll = np.array([s.roll_rad for s in samples])
  actual = np.array([s.actual_la for s in samples])
  desired = np.array([s.desired_la for s in samples])
  jerk = np.array([s.desired_jerk for s in samples])
  p_term = np.array([s.p_term for s in samples])
  i_term = np.array([s.i_term for s in samples])
  f_term = np.array([s.f_term for s in samples])
  torque_cmd = np.array([s.torque_cmd for s in samples])

  roll_valid = np.isfinite(roll)
  low_roll = roll_valid & (np.abs(roll) <= math.radians(LOW_ROLL_THRESHOLD_DEG))
  high_roll = roll_valid & (~low_roll)
  base = lat_active & (~steering_pressed) & (v > 8.0)
  transition_base = lat_active & (~steering_pressed) & (v > 4.0) & (~saturated)
  if np.any(roll_valid):
    print("\nRoll context:")
    print(
      f"  valid={int(roll_valid.sum()):5d}/{len(samples):5d} "
      f"abs_mean_deg={np.mean(np.degrees(np.abs(roll[roll_valid]))):.3f} "
      f"abs_p95_deg={np.percentile(np.degrees(np.abs(roll[roll_valid])), 95):.3f} "
      f"low_roll_deg<={LOW_ROLL_THRESHOLD_DEG:.1f}"
    )

  masks = (
    ("all", base),
    ("all_non_sat", base & (~saturated)),
    ("left", base & (~saturated) & (desired >= 0.1)),
    ("right", base & (~saturated) & (desired <= -0.1)),
    ("center", base & (~saturated) & (np.abs(desired) < 0.1)),
    ("steady_left", base & (~saturated) & (desired >= 0.1) & (np.abs(jerk) < 0.2)),
    ("steady_right", base & (~saturated) & (desired <= -0.1) & (np.abs(jerk) < 0.2)),
    ("steady_left_low_roll", base & (~saturated) & low_roll & (desired >= 0.1) & (np.abs(jerk) < 0.2)),
    ("steady_right_low_roll", base & (~saturated) & low_roll & (desired <= -0.1) & (np.abs(jerk) < 0.2)),
    ("center_low_roll", base & (~saturated) & low_roll & (np.abs(desired) < 0.1)),
    ("steady_left_high_roll", base & (~saturated) & high_roll & (desired >= 0.1) & (np.abs(jerk) < 0.2)),
    ("steady_right_high_roll", base & (~saturated) & high_roll & (desired <= -0.1) & (np.abs(jerk) < 0.2)),
    ("low_speed_sharp", transition_base & (v < 14.0) & (np.abs(desired) >= 0.4) & (np.abs(jerk) >= 0.35)),
    ("turn_in", transition_base & (v < 14.0) & (np.abs(desired) >= 0.4) & (np.abs(jerk) >= 0.35) & ((desired * jerk) > 0.0)),
    ("unwind", transition_base & (v < 14.0) & (np.abs(desired) >= 0.4) & (np.abs(jerk) >= 0.35) & ((desired * jerk) < 0.0)),
  )

  print("\nControlsState tracking:")
  for name, mask in masks:
    if not np.any(mask):
      continue
    print(
      f"  {name:12s} n={int(mask.sum()):5d} "
      f"mae={np.mean(np.abs(desired[mask] - actual[mask])):.4f} "
      f"bias={np.mean(actual[mask] - desired[mask]):+.4f} "
      f"|p|={np.mean(np.abs(p_term[mask])):.4f} "
      f"|i|={np.mean(np.abs(i_term[mask])):.4f} "
      f"|f|={np.mean(np.abs(f_term[mask])):.4f} "
      f"torque={np.mean(torque_cmd[mask]):+.4f}"
    )


def summarize_torque_points(car_fingerprint: str, points: np.ndarray) -> None:
  if points.size == 0:
    print("No torque-estimator points found.")
    return

  params = NON_LINEAR_TORQUE_PARAMS.get(car_fingerprint)
  if params is None:
    print(f"No siglin torque params configured for {car_fingerprint}.")
    return

  steer = points[:, 0]
  lat = points[:, 1]
  pred = np.array([siglin_torque(x, params) for x in lat])
  err = pred - steer

  print("\nTorque map residuals:")
  print(f"  all          n={points.shape[0]:5d} mae={np.mean(np.abs(err)):.4f} bias={np.mean(err):+.4f}")
  for name, mask in (("left", lat >= 0.0), ("right", lat < 0.0), ("small", np.abs(lat) < 0.3), ("mid", (np.abs(lat) >= 0.3) & (np.abs(lat) < 0.8))):
    if np.any(mask):
      print(f"  {name:12s} n={int(mask.sum()):5d} mae={np.mean(np.abs(err[mask])):.4f} bias={np.mean(err[mask]):+.4f}")

  print("\nLinearized correction against current siglin:")
  for name, mask in (("all", np.ones_like(lat, dtype=bool)), ("left", lat >= 0.0), ("right", lat < 0.0)):
    x = np.column_stack([pred[mask], np.ones(mask.sum())])
    y = steer[mask]
    scale, offset = np.linalg.lstsq(x, y, rcond=None)[0]
    fit = scale * pred[mask] + offset
    print(
      f"  {name:12s} scale={scale:.4f} offset={offset:+.4f} "
      f"mae_fit={np.mean(np.abs(fit - y)):.4f}"
    )


# required tuning level per key; 5th field of the entry in common/params_keys.h
PARAM_TUNING_LEVELS = {
  "AdvancedLateralTune": 2,
  "ForceAutoTune": 3,
  "ForceAutoTuneOff": 2,
  "SteerFriction": 3,
  "SteerLatAccel": 3,
}


def _get_tuning_level(log_params: dict[str, bytes]) -> int:
  if log_params.get("TuningLevelConfirmed", b"0") == b"1":
    try:
      return int(log_params.get("TuningLevel", b"2").decode())
    except (ValueError, TypeError):
      return 2
  return 2


def _param_bool(log_params: dict[str, bytes], key: str, tuning_level: int = 3) -> bool:
  if tuning_level < PARAM_TUNING_LEVELS.get(key, 0):
    return False
  return log_params.get(key, b"0") == b"1"


def _param_float(log_params: dict[str, bytes], key: str, default: float, tuning_level: int = 3) -> float:
  if tuning_level < PARAM_TUNING_LEVELS.get(key, 0):
    return default
  val = log_params.get(key)
  if val is None:
    return default
  try:
    return float(val.decode())
  except (ValueError, TypeError):
    return default


def clamp(val: float, min_val: float, max_val: float) -> float:
  return max(min_val, min(val, max_val))


def resolve_effective_tune(car_params, log_params: dict[str, bytes], live_snapshot):
  if car_params.lateralTuning.which() != "torque":
    return None

  torque_tune = car_params.lateralTuning.torque
  static_lat_accel_factor = torque_tune.latAccelFactor
  static_lat_accel_offset = torque_tune.latAccelOffset
  static_friction = torque_tune.friction

  tuning_level = _get_tuning_level(log_params)

  has_auto_tune = live_snapshot.useParams if live_snapshot is not None else False
  alt = _param_bool(log_params, "AdvancedLateralTune", tuning_level)
  force_auto_tune = alt and (not has_auto_tune) and _param_bool(log_params, "ForceAutoTune", tuning_level)
  force_auto_tune_off = alt and has_auto_tune and _param_bool(log_params, "ForceAutoTuneOff", tuning_level)

  if alt:
    custom_friction = clamp(_param_float(log_params, "SteerFriction", static_friction, tuning_level), 0.0, 1.0)
    custom_lat_accel = clamp(
      _param_float(log_params, "SteerLatAccel", static_lat_accel_factor, tuning_level),
      0.5 * static_lat_accel_factor,
      1.5 * static_lat_accel_factor,
    )
  else:
    custom_friction = static_friction
    custom_lat_accel = static_lat_accel_factor

  use_custom_friction = (
    round(custom_friction, 2) != round(static_friction, 2) and not force_auto_tune
  ) or force_auto_tune_off
  use_custom_latAccel = (
    round(custom_lat_accel, 2) != round(static_lat_accel_factor, 2) and not force_auto_tune
  ) or force_auto_tune_off

  use_live_params = has_auto_tune or force_auto_tune

  lat_accel_factor = static_lat_accel_factor
  lat_accel_offset = static_lat_accel_offset
  friction = static_friction

  if use_live_params and live_snapshot is not None:
    if not use_custom_latAccel:
      lat_accel_factor = live_snapshot.latAccelFactorFiltered
      lat_accel_offset = live_snapshot.latAccelOffsetFiltered
    if not use_custom_friction:
      friction = live_snapshot.frictionCoefficientFiltered

  if use_custom_latAccel:
    lat_accel_factor = custom_lat_accel
  if use_custom_friction:
    friction = custom_friction

  toggles = {
    "AdvancedLateralTune": int(alt),
    "ForceAutoTune": int(_param_bool(log_params, "ForceAutoTune", tuning_level)),
    "ForceAutoTuneOff": int(_param_bool(log_params, "ForceAutoTuneOff", tuning_level)),
    "hasAutoTune": int(has_auto_tune),
    "tuningLevel": tuning_level,
  }
  resolved = {
    "useLiveParams": int(use_live_params),
    "useCustomLatAccel": int(use_custom_latAccel),
    "useCustomFriction": int(use_custom_friction),
  }
  static_vals = (static_lat_accel_factor, static_lat_accel_offset, static_friction)
  if live_snapshot is not None:
    live_vals = (
      live_snapshot.latAccelFactorFiltered,
      live_snapshot.latAccelOffsetFiltered,
      live_snapshot.frictionCoefficientFiltered,
    )
  else:
    live_vals = None
  effective_vals = (lat_accel_factor, lat_accel_offset, friction)

  return {
    "toggles": toggles,
    "resolved": resolved,
    "static": static_vals,
    "live": live_vals,
    "effective": effective_vals,
  }


def summarize_bolt_effective_tune(car_params) -> None:
  if car_params.carFingerprint not in BOLT_CARS or car_params.lateralTuning.which() != "torque":
    return

  torque_tune = car_params.lateralTuning.torque
  ff_scale_pos = float(getattr(torque_tune, "kp", getattr(torque_tune, "kpDEPRECATED", 1.0)))
  ff_scale_neg = float(getattr(torque_tune, "ki", getattr(torque_tune, "kiDEPRECATED", 1.0)))
  ki_mult = float(getattr(torque_tune, "kd", getattr(torque_tune, "kdDEPRECATED", 1.0)))
  deadzone_boost = float(getattr(torque_tune, "kf", getattr(torque_tune, "kfDEPRECATED", 0.0)))

  if ff_scale_pos == ff_scale_neg:
    asym_str = "symmetric"
  else:
    diff = (ff_scale_neg / ff_scale_pos) - 1.0 if ff_scale_pos != 0.0 else 0.0
    direction = "right" if diff >= 0 else "left"
    asym_str = f"{diff:+.1%} {direction}"

  # Note: With a +-0.05 blend this is effectively a step at ff = 0
  ki_applied_str = "(applied to pid._k_i)" if (ki_mult > 0.0 and ki_mult != 1.0) else "(not applied)"
  dz_str = f"(reach |latAccel|<{DEADZONE_BOOST_LAT_ACCEL:.2f}, unscaled additive)" if deadzone_boost != 0.0 else "(inactive)"

  print(
    f"  bolt: ffAsym left=x{ff_scale_pos:.4f} right=x{ff_scale_neg:.4f} ({asym_str}), "
    f"blended over ff in [-{FF_SCALE_BLEND_LAT_ACCEL:.4f},+{FF_SCALE_BLEND_LAT_ACCEL:.4f}]"
  )
  print(
    f"        kiMult={ki_mult:.4f} {ki_applied_str}  deadzoneBoost={deadzone_boost:.4f} {dz_str}"
  )


def main() -> None:
  # helpers.py:101 subsamples via the global numpy RNG; seed so TorqueEstimator fit is reproducible
  np.random.seed(0)

  parser = argparse.ArgumentParser(description="Analyze a Bolt route for lateral tuning opportunities.")
  parser.add_argument("route", help="Route name, e.g. dongle/route")
  parser.add_argument("--mode", choices=("auto", "qlog", "rlog"), default="auto")
  args = parser.parse_args()

  mode_map = {
    "auto": ReadMode.AUTO,
    "qlog": ReadMode.QLOG,
    "rlog": ReadMode.RLOG,
  }
  log_reader = LogReader(args.route, default_mode=mode_map[args.mode], sort_by_time=True)

  log_params: dict[str, bytes] = {}
  car_params = None
  live_torque_snapshots = []
  torque_estimator = None
  latest = {}
  control_samples: list[ControlSample] = []

  for msg in log_reader:
    try:
      which = msg.which()
    except Exception:
      #print('skipping corrupted msg')
      continue
    if which == "initData":
      log_params = {entry.key: entry.value for entry in msg.initData.params.entries}
      continue

    if which == "carParams" and car_params is None:
      car_params = msg.carParams
      torque_estimator = TorqueEstimator(car_params, track_all_points=True)
      continue

    if car_params is None:
      continue

    if which in ("carState", "carControl"):
      latest[which] = getattr(msg, which)
    elif which == "liveParameters":
      latest[which] = msg.liveParameters
    elif which == "controlsState" and "carState" in latest and "carControl" in latest:
      lateral_state = msg.controlsState.lateralControlState
      if lateral_state.which() == "torqueState":
        torque_state = lateral_state.torqueState
        v = latest["carState"].vEgo
        curvature = torque_state.desiredLateralAccel / (v * v) if v > 1.0 else float("nan")
        control_samples.append(ControlSample(
          v_ego=v,
          steering_pressed=latest["carState"].steeringPressed,
          lat_active=latest["carControl"].latActive,
          saturated=torque_state.saturated,
          roll_rad=float(getattr(latest.get("liveParameters"), "roll", float("nan"))),
          actual_la=torque_state.actualLateralAccel,
          desired_la=torque_state.desiredLateralAccel,
          desired_jerk=torque_state.desiredLateralJerk,
          p_term=torque_state.p,
          i_term=torque_state.i,
          f_term=torque_state.f,
          torque_cmd=latest["carControl"].actuators.torque,
          mono_time=msg.logMonoTime,
          d_term=torque_state.d,
          curvature=curvature,
          torque_active=torque_state.active,
        ))
    elif which == "liveTorqueParameters" and len(live_torque_snapshots) < 8:
      live_torque_snapshots.append(msg.liveTorqueParameters)

    if torque_estimator is not None and which in ("carControl", "carOutput", "carState", "liveCalibration", "livePose", "liveDelay"):
      torque_estimator.handle_log(msg.logMonoTime / 1e9, which, getattr(msg, which))

  if car_params is None:
    raise RuntimeError("No carParams found in route.")

  torque_tune = car_params.lateralTuning.torque
  print(f"carFingerprint={car_params.carFingerprint}")
  print(f"steerRatio={car_params.steerRatio:.4f} steerActuatorDelay={car_params.steerActuatorDelay:.4f}")
  if car_params.carFingerprint in BOLT_CARS:
    ff_scale_pos = float(getattr(torque_tune, "kp", getattr(torque_tune, "kpDEPRECATED", 1.0)))
    ff_scale_neg = float(getattr(torque_tune, "ki", getattr(torque_tune, "kiDEPRECATED", 1.0)))
    ki_mult = float(getattr(torque_tune, "kd", getattr(torque_tune, "kdDEPRECATED", 1.0)))
    deadzone_boost = float(getattr(torque_tune, "kf", getattr(torque_tune, "kfDEPRECATED", 0.0)))
    print(
      "staticTune="
      f"latAccelFactor={torque_tune.latAccelFactor:.4f} "
      f"friction={torque_tune.friction:.4f} "
      f"latAccelOffset={torque_tune.latAccelOffset:.4f} "
      f"ffScalePos={ff_scale_pos:.4f} "
      f"ffScaleNeg={ff_scale_neg:.4f} "
      f"kiMult={ki_mult:.4f} "
      f"deadzoneBoost={deadzone_boost:.4f}"
    )
  else:
    kp = float(getattr(torque_tune, "kp", getattr(torque_tune, "kpDEPRECATED", 0.0)))
    ki = float(getattr(torque_tune, "ki", getattr(torque_tune, "kiDEPRECATED", 0.0)))
    kd = float(getattr(torque_tune, "kd", getattr(torque_tune, "kdDEPRECATED", 0.0)))
    kf = float(getattr(torque_tune, "kf", getattr(torque_tune, "kfDEPRECATED", 0.0)))
    print(
      "staticTune="
      f"latAccelFactor={torque_tune.latAccelFactor:.4f} "
      f"friction={torque_tune.friction:.4f} "
      f"latAccelOffset={torque_tune.latAccelOffset:.4f} "
      f"kp={kp:.4f} "
      f"ki={ki:.4f} "
      f"kd={kd:.4f} "
      f"kf={kf:.4f}"
    )

  if live_torque_snapshots:
    last = live_torque_snapshots[-1]
    print(
      "liveTorqueFiltered="
      f"latAccelFactor={last.latAccelFactorFiltered:.4f} "
      f"latAccelOffset={last.latAccelOffsetFiltered:.4f} "
      f"friction={last.frictionCoefficientFiltered:.4f} "
      f"useParams={last.useParams} liveValid={last.liveValid}"
    )

  last_snapshot = live_torque_snapshots[-1] if live_torque_snapshots else None
  tune_res = resolve_effective_tune(car_params, log_params, last_snapshot)

  print("\nEffective tune:")
  if car_params.lateralTuning.which() != "torque":
    print("  lateralTuning is not torque; skipped.")
  elif tune_res is not None:
    if not log_params:
      print("  params unavailable in log; toggle state unknown")
    else:
      t = tune_res["toggles"]
      print(
        f"  toggles=AdvancedLateralTune={t['AdvancedLateralTune']} "
        f"ForceAutoTune={t['ForceAutoTune']} "
        f"ForceAutoTuneOff={t['ForceAutoTuneOff']} "
        f"hasAutoTune={t['hasAutoTune']}(@route start) "
        f"tuningLevel={t['tuningLevel']}"
      )
    r = tune_res["resolved"]
    print(
      f"  resolved=useLiveParams={r['useLiveParams']} "
      f"useCustomLatAccel={r['useCustomLatAccel']} "
      f"useCustomFriction={r['useCustomFriction']}"
    )

    s_laf, s_lao, s_fr = tune_res["static"]
    s_famp = s_fr * s_laf
    print(
      f"  static=   latAccelFactor={s_laf:.4f} latAccelOffset={s_lao:.4f} "
      f"friction={s_fr:.4f} frictionAmp={s_famp:.4f}"
    )

    if tune_res["live"] is not None:
      l_laf, l_lao, l_fr = tune_res["live"]
      l_famp = l_fr * l_laf
      print(
        f"  liveFilt= latAccelFactor={l_laf:.4f} latAccelOffset={l_lao:.4f} "
        f"friction={l_fr:.4f} frictionAmp={l_famp:.4f}"
      )
    else:
      print("  liveFilt= (no liveTorqueParameters in log)")

    e_laf, e_lao, e_fr = tune_res["effective"]
    e_famp = e_fr * e_laf
    print(
      f"  effective=latAccelFactor={e_laf:.4f} latAccelOffset={e_lao:.4f} "
      f"friction={e_fr:.4f} frictionAmp={e_famp:.4f}"
    )

    if (
      abs(e_laf - s_laf) > 1e-6 or
      abs(e_lao - s_lao) > 1e-6 or
      abs(e_fr - s_fr) > 1e-6
    ):
      print("  WARNING: effective tune differs from static tune; report figures reflect the effective tune.")

    summarize_bolt_effective_tune(car_params)

  summarize_control_samples(control_samples)

  unwind, d_des_rate = reconstruct_unwind(control_samples)
  summarize_unwind_reconstruction(control_samples, unwind, d_des_rate)

  points = np.array(torque_estimator.all_torque_points) if torque_estimator is not None else np.empty((0, 2))
  if torque_estimator is not None and torque_estimator.filtered_points.is_calculable():
    slope, offset, friction = torque_estimator.estimate_params()
    print(
      "\nTorqueEstimator fit:"
      f" latAccelFactor={slope:.4f}"
      f" latAccelOffset={offset:.4f}"
      f" friction={friction:.4f}"
      f" bucket_points={len(torque_estimator.filtered_points)}"
    )
  summarize_torque_points(car_params.carFingerprint, points)


if __name__ == "__main__":
  main()
