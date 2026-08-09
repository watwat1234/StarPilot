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
  BOLT_2022_2023_CARS,
  DEADZONE_BOOST_LAT_ACCEL,
  FF_SCALE_BLEND_LAT_ACCEL,
  get_bolt_2022_2023_center_output_scale,
  get_bolt_2022_2023_ff_scale,
  get_bolt_2022_2023_friction_scale,
  get_bolt_2022_2023_friction_threshold,
  get_gm_base_friction_threshold,
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

# Chunk 5 - turn-in event detection
TURN_IN_LA_THRESHOLD = 0.4     # m/s^2, event arms on crossing this
TURN_IN_REARM_LA = 0.3         # hysteresis: |desired_la| must fall below this to re-arm
TURN_IN_MAX_V = 14.0           # m/s
EVENT_PRE_S = 1.0              # pre-roll seconds, captures the unwind-freeze approach
EVENT_POST_S = 3.0             # seconds after t0; the overcorrection window
EVENT_MIN_S = 1.0              # discard events truncated shorter than this
EVENT_REFRACTORY_S = 2.0       # minimum spacing between event starts
AT_LIMIT_TORQUE = 0.99         # steer_max = 1.0 (latcontrol.py:17)
WORST_EVENTS_N = 10


def reconstruct_unwind(samples: list[ControlSample]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
  """Reconstruct the integrator-freeze gate both ways.

  unwind_old mirrors the pre-d55b679f5 controller, which tested the signed setpoint rate.
  unwind_new mirrors the shipped controller, which tests the magnitude rate. Both are reported
  so a single run shows the flip directly.

  d_des_rate stays SIGNED: the phase classifier keys entering/exiting off its sign.
  """
  n = len(samples)
  unwind_old = np.zeros(n, dtype=bool)
  unwind_new = np.zeros(n, dtype=bool)
  d_des_rate = np.full(n, np.nan, dtype=float)

  prev_setpoint = 0.0

  for k, s in enumerate(samples):
    if not s.torque_active:
      prev_setpoint = 0.0
      unwind_old[k] = False
      unwind_new[k] = False
      d_des_rate[k] = np.nan
      continue

    # Gap guard: k == 0 or prev sample inactive or time gap > 1.5 * DT_CTRL
    if k == 0 or not samples[k - 1].torque_active or ((s.mono_time - samples[k - 1].mono_time) / 1e9 > 1.5 * DT_CTRL):
      unwind_old[k] = False
      unwind_new[k] = False
      d_des_rate[k] = np.nan
      prev_setpoint = s.desired_la
      continue

    near_zero = abs(s.desired_la) < UNWIND_LAT_ACCEL_NEAR_ZERO
    rate = (s.desired_la - prev_setpoint) / DT_CTRL
    mag_rate = (abs(s.desired_la) - abs(prev_setpoint)) / DT_CTRL
    d_des_rate[k] = rate
    unwind_old[k] = (rate < UNWIND_D_DES_THRESHOLD) and near_zero
    unwind_new[k] = (mag_rate < UNWIND_D_DES_THRESHOLD) and near_zero
    prev_setpoint = s.desired_la

  return unwind_old, unwind_new, d_des_rate


def summarize_unwind_reconstruction(samples: list[ControlSample], unwind_old: np.ndarray, unwind_new: np.ndarray,
                                    d_des_rate: np.ndarray) -> None:
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

  overall_unwind_old = np.mean(unwind_old[torque_active_mask]) if active_count > 0 else 0.0
  overall_unwind_new = np.mean(unwind_new[torque_active_mask]) if active_count > 0 else 0.0

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
    f"  active_samples={active_count:5d} unwind_old={overall_unwind_old:.4f} "
    f"unwind_new={overall_unwind_new:.4f} unclassified={unclassified_count:5d}"
  )
  print("  phase            n      unwind_old  unwind_new   mean|i|     bias  mean|d_des|")

  phases = (
    ("entering_left", entering_left_mask),
    ("entering_right", entering_right_mask),
    ("exiting_left", exiting_left_mask),
    ("exiting_right", exiting_right_mask),
  )

  for name, mask in phases:
    n = int(mask.sum())
    if n == 0:
      uo_str, un_str, i_str, b_str, d_str = "--", "--", "--", "--", "--"
    else:
      uo_str = f"{np.mean(unwind_old[mask]):.4f}"
      un_str = f"{np.mean(unwind_new[mask]):.4f}"
      i_str = f"{np.mean(np.abs(i_term[mask])):.4f}"
      b_str = f"{np.mean(actual_la[mask] - desired_la[mask]):+.4f}"
      d_str = f"{np.mean(np.abs(d_des_rate[mask])):.4f}"
    print(
      f"  {name:16s} {n:5d}  {uo_str:>10s}  {un_str:>10s}  {i_str:>8s}  {b_str:>7s}  {d_str:>10s}"
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


def _param_str(log_params: dict[str, bytes], key: str, default: str = "") -> str:
  val = log_params.get(key)
  if val is None:
    return default
  try:
    return val.decode()
  except (UnicodeDecodeError, AttributeError):
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
  force_auto_tune_off = alt and _param_bool(log_params, "ForceAutoTuneOff", tuning_level)

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


def reconstruct_bolt_2022_2023_gains(
  samples: list[ControlSample], car_fingerprint: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
  n = len(samples)
  if car_fingerprint not in BOLT_2022_2023_CARS or n == 0:
    return (
      np.full(n, np.nan, dtype=float),
      np.full(n, np.nan, dtype=float),
      np.full(n, np.nan, dtype=float),
      np.full(n, np.nan, dtype=float),
      np.full(n, np.nan, dtype=float),
    )

  ff_scale = np.zeros(n, dtype=float)
  friction_scale = np.zeros(n, dtype=float)
  friction_threshold = np.zeros(n, dtype=float)
  threshold_ratio = np.zeros(n, dtype=float)
  center_output_scale = np.zeros(n, dtype=float)

  for k, s in enumerate(samples):
    la = s.desired_la
    jerk = s.desired_jerk
    v = s.v_ego
    ff_scale[k] = get_bolt_2022_2023_ff_scale(la, jerk, v)
    friction_scale[k] = get_bolt_2022_2023_friction_scale(v, la, jerk)
    th = get_bolt_2022_2023_friction_threshold(v, la, jerk)
    friction_threshold[k] = th
    base_th = get_gm_base_friction_threshold(v)
    threshold_ratio[k] = th / base_th if base_th != 0.0 else 1.0
    center_output_scale[k] = get_bolt_2022_2023_center_output_scale(la, v)

  return ff_scale, friction_scale, friction_threshold, threshold_ratio, center_output_scale


def summarize_bolt_dynamic_gains(
  samples: list[ControlSample],
  car_fingerprint: str,
  ff_scale: np.ndarray,
  friction_scale: np.ndarray,
  threshold_ratio: np.ndarray,
  center_output_scale: np.ndarray,
) -> None:
  if car_fingerprint not in BOLT_2022_2023_CARS:
    print("\nBolt dynamic gains:\n  skipped (not a 2022-2023 Bolt)")
    return

  torque_active = np.array([s.torque_active for s in samples], dtype=bool)
  steering_pressed = np.array([s.steering_pressed for s in samples], dtype=bool)
  mask = torque_active & (~steering_pressed)

  print("\nBolt dynamic gains:")
  if not np.any(mask):
    print("  No active non-steering-pressed samples found.")
    return

  ff_m = ff_scale[mask]
  fr_m = friction_scale[mask]
  tr_m = threshold_ratio[mask]
  co_m = center_output_scale[mask]

  print(
    f"  ff_scale [{np.min(ff_m):.4f},{np.max(ff_m):.4f}] med={np.median(ff_m):.4f}  "
    f"friction_scale [{np.min(fr_m):.4f},{np.max(fr_m):.4f}] med={np.median(fr_m):.4f}  "
    f"thresh_ratio [{np.min(tr_m):.4f},{np.max(tr_m):.4f}] med={np.median(tr_m):.4f}  "
    f"center_output_scale [{np.min(co_m):.4f},{np.max(co_m):.4f}] med={np.median(co_m):.4f}"
  )


def summarize_bolt_gain_bands(
  samples: list[ControlSample],
  car_fingerprint: str,
  ff_scale: np.ndarray,
  friction_scale: np.ndarray,
) -> None:
  if not samples or car_fingerprint not in BOLT_2022_2023_CARS:
    return

  torque_active = np.array([s.torque_active for s in samples], dtype=bool)
  steering_pressed = np.array([s.steering_pressed for s in samples], dtype=bool)
  desired_la = np.array([s.desired_la for s in samples])
  v_ego = np.array([s.v_ego for s in samples])

  base_mask = torque_active & (~steering_pressed) & np.isfinite(desired_la)

  la_bins = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.7), (0.7, 1.1), (1.1, 1.6), (1.6, float("inf"))]
  la_labels = ["0-0.2", "0.2-0.4", "0.4-0.7", "0.7-1.1", "1.1-1.6", "1.6+"]

  v_bins = [(0.0, 6.0), (6.0, 10.0), (10.0, 14.0), (14.0, 20.0), (20.0, float("inf"))]
  v_labels = ["<6", "6-10", "10-14", "14-20", "20+"]

  def print_gains_table(title: str, dir_mask: np.ndarray):
    print(f"\n{title}:")
    header = f"  {'|desired_la|':12s}" + "".join(f"{lbl:>18s}" for lbl in v_labels)
    print(header)
    for (la_min, la_max), la_lbl in zip(la_bins, la_labels):
      row_str = f"  {la_lbl:12s}"
      for v_min, v_max in v_bins:
        cell_mask = base_mask & dir_mask & (np.abs(desired_la) >= la_min) & (np.abs(desired_la) < la_max) & (v_ego >= v_min) & (v_ego < v_max)
        n = int(cell_mask.sum())
        if n == 0:
          cell_val = "--"
        else:
          ff_valid = ff_scale[cell_mask][np.isfinite(ff_scale[cell_mask])]
          fr_valid = friction_scale[cell_mask][np.isfinite(friction_scale[cell_mask])]
          ff_str = f"{np.median(ff_valid):.4f}" if ff_valid.size > 0 else "--"
          fr_str = f"{np.median(fr_valid):.4f}" if fr_valid.size > 0 else "--"
          n_str = f"({n})" if n < 50 else f"{n}"
          cell_val = f"{ff_str}/{fr_str}/{n_str}"
        row_str += f"{cell_val:>18s}"
      print(row_str)

  print_gains_table("Dynamic gain bands - left (desired_la > 0) [medFF/medFric/n]", desired_la > 0)
  print_gains_table("Dynamic gain bands - right (desired_la < 0) [medFF/medFric/n]", desired_la < 0)


@dataclass
class TurnInEvent:
  t0_idx: int
  t0_mono: int
  t_rel: float
  direction: str
  sign: float
  v0: float
  pre_n: int
  post_n: int
  peak_in: float
  t_in: float
  peak_opp: float
  t_opp: float
  peak_abs: float
  peak_tq: float
  at_limit: float
  pressed: float
  mean_i_pre: float


def detect_turn_in_events(samples: list[ControlSample]) -> tuple[list[TurnInEvent], int, int]:
  events: list[TurnInEvent] = []
  discarded_truncated = 0
  no_preroll_count = 0

  if not samples:
    return events, discarded_truncated, no_preroll_count

  t0_mono_start = samples[0].mono_time
  armed = True
  prev_t0_mono = -float("inf")

  for t0 in range(1, len(samples)):
    s = samples[t0]
    prev_s = samples[t0 - 1]
    abs_la = abs(s.desired_la)

    if not armed:
      if abs_la < TURN_IN_REARM_LA:
        armed = True
      continue

    crossing = (abs_la >= TURN_IN_LA_THRESHOLD) and (abs(prev_s.desired_la) < TURN_IN_LA_THRESHOLD)
    turn_in_dir = (s.desired_la * s.desired_jerk) > 0.0
    speed_ok = s.v_ego < TURN_IN_MAX_V
    active_ok = s.torque_active
    not_pressed = not s.steering_pressed
    refractory_ok = ((s.mono_time - prev_t0_mono) / 1e9) >= EVENT_REFRACTORY_S

    if crossing and turn_in_dir and speed_ok and active_ok and not_pressed and refractory_ok:
      armed = False
      prev_t0_mono = s.mono_time

      post_indices = []
      for k in range(t0, len(samples)):
        dt_t0 = (samples[k].mono_time - s.mono_time) / 1e9
        if dt_t0 > EVENT_POST_S:
          break
        if not samples[k].torque_active:
          break
        if k > t0 and ((samples[k].mono_time - samples[k - 1].mono_time) / 1e9) > (1.5 * DT_CTRL):
          break
        post_indices.append(k)

      post_duration = (samples[post_indices[-1]].mono_time - s.mono_time) / 1e9
      if post_duration < EVENT_MIN_S:
        discarded_truncated += 1
        continue

      pre_indices = []
      for k in range(t0 - 1, -1, -1):
        dt_pre = (s.mono_time - samples[k].mono_time) / 1e9
        if dt_pre > EVENT_PRE_S:
          break
        if not samples[k].torque_active:
          break
        if ((samples[k + 1].mono_time - samples[k].mono_time) / 1e9) > (1.5 * DT_CTRL):
          break
        pre_indices.append(k)

      pre_indices.reverse()
      pre_n = len(pre_indices)
      if pre_n == 0:
        no_preroll_count += 1
        mean_i_pre = float("nan")
      else:
        mean_i_pre = float(np.mean([abs(samples[k].i_term) for k in pre_indices]))

      direction = "left" if s.desired_la > 0.0 else "right"
      sign = 1.0 if s.desired_la > 0.0 else -1.0
      v0 = s.v_ego
      t_rel = (s.mono_time - t0_mono_start) / 1e9

      post_samples = [samples[k] for k in post_indices]
      post_n = len(post_samples)

      errs = [sign * (ps.actual_la - ps.desired_la) for ps in post_samples]
      times = [(ps.mono_time - s.mono_time) / 1e9 for ps in post_samples]

      peak_in = max(errs)
      t_in_idx = errs.index(peak_in)
      t_in = times[t_in_idx]

      post_after_tin = post_samples[t_in_idx + 1 :]
      if post_after_tin:
        opp_errs_after = [-sign * (ps.actual_la - ps.desired_la) for ps in post_after_tin]
        times_after = [(ps.mono_time - s.mono_time) / 1e9 for ps in post_after_tin]
        peak_opp = max(opp_errs_after)
        t_opp = times_after[opp_errs_after.index(peak_opp)]
      else:
        peak_opp = float("nan")
        t_opp = float("nan")

      peak_abs = max(abs(ps.desired_la - ps.actual_la) for ps in post_samples)
      peak_tq = max(abs(ps.torque_cmd) for ps in post_samples)
      at_limit = sum(1 for ps in post_samples if abs(ps.torque_cmd) >= AT_LIMIT_TORQUE) / post_n
      pressed = sum(1 for ps in post_samples if ps.steering_pressed) / post_n

      events.append(TurnInEvent(
        t0_idx=t0,
        t0_mono=s.mono_time,
        t_rel=t_rel,
        direction=direction,
        sign=sign,
        v0=v0,
        pre_n=pre_n,
        post_n=post_n,
        peak_in=peak_in,
        t_in=t_in,
        peak_opp=peak_opp,
        t_opp=t_opp,
        peak_abs=peak_abs,
        peak_tq=peak_tq,
        at_limit=at_limit,
        pressed=pressed,
        mean_i_pre=mean_i_pre,
      ))

  return events, discarded_truncated, no_preroll_count


def summarize_turn_in_events(
  events: list[TurnInEvent],
  discarded_truncated: int,
  no_preroll_count: int,
) -> None:
  print("\nTurn-in events:")
  if not events and discarded_truncated == 0:
    print("  no turn-in events detected")
    return

  n_total = len(events)
  n_left = sum(1 for e in events if e.direction == "left")
  n_right = sum(1 for e in events if e.direction == "right")

  print(
    f"  total={n_total:d}  left={n_left:d}  right={n_right:d}  "
    f"discarded_truncated={discarded_truncated:d}  no_preroll={no_preroll_count:d}"
  )
  print(
    f"  {'dir':6s} {'n':>4s}   {'peak_in med/p90':17s}  {'peak_opp med/p90':17s}  {'peak_abs med/p90':17s}  {'at_limit med/p90':17s}  {'t_in med':>8s}"
  )

  for d_lbl in ("left", "right"):
    d_events = [e for e in events if e.direction == d_lbl]
    n = len(d_events)
    n_str = f"({n})" if n < 5 else f"{n:d}"
    if n == 0:
      print(f"  {d_lbl:6s} {n_str:>4s}   {'--':17s}  {'--':17s}  {'--':17s}  {'--':17s}  {'--':>8s}")
    else:
      in_arr = np.array([e.peak_in for e in d_events])
      opp_arr = np.array([e.peak_opp for e in d_events])
      abs_arr = np.array([e.peak_abs for e in d_events])
      lim_arr = np.array([e.at_limit for e in d_events])
      tin_arr = np.array([e.t_in for e in d_events])

      in_str = f"{np.median(in_arr):+.4f}/{np.percentile(in_arr, 90):+.4f}"
      opp_valid = opp_arr[np.isfinite(opp_arr)]
      if opp_valid.size > 0:
        opp_str = f"{np.median(opp_valid):+.4f}/{np.percentile(opp_valid, 90):+.4f}"
      else:
        opp_str = "--"
      abs_str = f"{np.median(abs_arr):.4f}/{np.percentile(abs_arr, 90):.4f}"
      lim_str = f"{np.median(lim_arr):.4f}/{np.percentile(lim_arr, 90):.4f}"
      tin_str = f"{np.median(tin_arr):.4f}"

      print(
        f"  {d_lbl:6s} {n_str:>4s}   {in_str:17s}  {opp_str:17s}  {abs_str:17s}  {lim_str:17s}  {tin_str:>8s}"
      )

  if events:
    worst_events = sorted(events, key=lambda e: e.peak_abs, reverse=True)[:WORST_EVENTS_N]
    print("\n  worst events (by peak_abs); t_rel is relative to the first controlsState sample:")
    print(
      f"  {'t_rel':9s} {'dir':6s} {'v0':>5s} {'peak_in':>7s} {'t_in':>5s} {'peak_opp':>8s} {'t_opp':>5s} {'peak_abs':>8s} {'peak_tq':>7s} {'at_limit':>8s} {'pressed':>7s} {'mean_i_pre':>10s}"
    )
    for e in worst_events:
      mins = int(e.t_rel // 60)
      secs = e.t_rel % 60
      t_rel_str = f"{mins:02d}:{secs:04.1f}"
      opp_str = f"{e.peak_opp:+8.4f}" if np.isfinite(e.peak_opp) else "      --"
      topp_str = f"{e.t_opp:5.2f}" if np.isfinite(e.t_opp) else "   --"
      i_pre_str = f"{e.mean_i_pre:.4f}" if np.isfinite(e.mean_i_pre) else "--"
      print(
        f"  {t_rel_str:9s} {e.direction:6s} {e.v0:5.2f} "
        f"{e.peak_in:+7.4f} {e.t_in:5.2f} {opp_str:>8s} {topp_str:>5s} "
        f"{e.peak_abs:8.4f} {e.peak_tq:7.4f} {e.at_limit:8.4f} {e.pressed:7.4f} {i_pre_str:>10s}"
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
  build_info: dict[str, object] | None = None
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
      build_info = {
        "commit": str(getattr(msg.initData, "gitCommit", "") or "unknown"),
        "date": str(getattr(msg.initData, "gitCommitDate", "") or "unknown"),
        "branch": str(getattr(msg.initData, "gitBranch", "") or "unknown"),
        "dirty": bool(getattr(msg.initData, "dirty", False)),
      }
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
  print(f"route={args.route}")
  if build_info is None:
    print("build=(no initData in log)")
  else:
    print(
      "build="
      f"commit={str(build_info['commit'])[:12]} "
      f"date={build_info['date']} "
      f"branch={build_info['branch']} "
      f"dirty={build_info['dirty']}"
    )
  print(f"carFingerprint={car_params.carFingerprint}")
  print(
    f"steerRatio={car_params.steerRatio:.4f} "
    f"steerActuatorDelay={car_params.steerActuatorDelay:.4f} (CP.steerActuatorDelay, not full_lateral_delay)"
  )
  if build_info is None:
    print("flm=(no initData in log)")
  else:
    flm_profile = _param_str(log_params, "FLMActiveProfileId")
    flm_applied = log_params.get("FLMTrialApplied", b"0") == b"1"
    flm_overrides = _param_str(log_params, "FLMActiveOverrides", "{}")
    print(
      f"flm=trialApplied={int(flm_applied)} "
      f"activeProfile={flm_profile or '(none)'} "
      f"overrides={flm_overrides}"
    )
    if flm_applied or flm_overrides not in ("", "{}"):
      print("  WARNING: an FLM trial was active on this route — tune values below may not be the static tune.")
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

  unwind_old, unwind_new, d_des_rate = reconstruct_unwind(control_samples)
  summarize_unwind_reconstruction(control_samples, unwind_old, unwind_new, d_des_rate)

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

  ff_scale, friction_scale, _, threshold_ratio, center_output_scale = reconstruct_bolt_2022_2023_gains(
    control_samples, car_params.carFingerprint
  )
  summarize_bolt_dynamic_gains(control_samples, car_params.carFingerprint, ff_scale, friction_scale, threshold_ratio,
                               center_output_scale)
  summarize_bolt_gain_bands(control_samples, car_params.carFingerprint, ff_scale, friction_scale)

  events, disc_trunc, no_pre = detect_turn_in_events(control_samples)
  summarize_turn_in_events(events, disc_trunc, no_pre)


if __name__ == "__main__":
  main()
