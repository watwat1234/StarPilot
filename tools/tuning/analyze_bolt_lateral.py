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
  get_bolt_2022_2023_ff_scale,
  get_bolt_2022_2023_friction_scale,
  get_bolt_2022_2023_friction_threshold,
  get_friction_threshold,
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

OSC_MIN_CROSSINGS = 6     # sign changes of detrended error per ~2 s window (~1.5 Hz floor)
OSC_MIN_P2P = 0.15        # m/s^2, peak-to-peak, so sensor noise is not counted

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


def reconstruct_bolt_2022_2023_gains(
  samples: list[ControlSample], car_fingerprint: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
  n = len(samples)
  if car_fingerprint not in BOLT_2022_2023_CARS or n == 0:
    return (
      np.full(n, np.nan, dtype=float),
      np.full(n, np.nan, dtype=float),
      np.full(n, np.nan, dtype=float),
      np.full(n, np.nan, dtype=float),
    )

  ff_scale = np.zeros(n, dtype=float)
  friction_scale = np.zeros(n, dtype=float)
  friction_threshold = np.zeros(n, dtype=float)
  threshold_ratio = np.zeros(n, dtype=float)

  for k, s in enumerate(samples):
    la = s.desired_la
    jerk = s.desired_jerk
    v = s.v_ego
    ff_scale[k] = get_bolt_2022_2023_ff_scale(la, jerk, v)
    friction_scale[k] = get_bolt_2022_2023_friction_scale(v, la, jerk)
    th = get_bolt_2022_2023_friction_threshold(v, la, jerk)
    friction_threshold[k] = th
    base_th = get_friction_threshold(v)
    threshold_ratio[k] = th / base_th if base_th != 0.0 else 1.0

  return ff_scale, friction_scale, friction_threshold, threshold_ratio


def summarize_bolt_dynamic_gains(
  samples: list[ControlSample],
  car_fingerprint: str,
  ff_scale: np.ndarray,
  friction_scale: np.ndarray,
  threshold_ratio: np.ndarray,
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

  print(
    f"  ff_scale [{np.min(ff_m):.4f},{np.max(ff_m):.4f}] med={np.median(ff_m):.4f}  "
    f"friction_scale [{np.min(fr_m):.4f},{np.max(fr_m):.4f}] med={np.median(fr_m):.4f}  "
    f"thresh_ratio [{np.min(tr_m):.4f},{np.max(tr_m):.4f}] med={np.median(tr_m):.4f}"
  )


def detect_oscillations(samples: list[ControlSample]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
  n = len(samples)
  osc = np.zeros(n, dtype=bool)
  osc_hz = np.full(n, np.nan, dtype=float)
  evaluated = np.zeros(n, dtype=bool)

  if n == 0:
    return osc, osc_hz, evaluated

  segments = []
  seg_start = None

  for k, s in enumerate(samples):
    active = s.torque_active and (not s.steering_pressed)
    if active:
      if seg_start is None:
        seg_start = k
      elif ((s.mono_time - samples[k - 1].mono_time) / 1e9) > 1.5 * DT_CTRL:
        segments.append((seg_start, k))
        seg_start = k
    else:
      if seg_start is not None:
        segments.append((seg_start, k))
        seg_start = None

  if seg_start is not None:
    segments.append((seg_start, n))

  window_len = 201
  half_win = 50
  hop_step = 25

  freqs = np.fft.rfftfreq(window_len, d=DT_CTRL)
  hanning_win = np.hanning(window_len)

  for start_i, end_i in segments:
    seg_len = end_i - start_i
    if seg_len < window_len:
      continue

    err = np.array([samples[k].desired_la - samples[k].actual_la for k in range(start_i, end_i)])
    detrended = np.full(seg_len, np.nan, dtype=float)

    for i in range(half_win, seg_len - half_win):
      detrended[i] = err[i] - np.mean(err[i - half_win : i + half_win + 1])

    for w_start in range(0, seg_len - window_len + 1, hop_step):
      w_end = w_start + window_len
      sub = detrended[w_start:w_end]

      if np.any(np.isnan(sub)):
        continue

      c0 = start_i + w_start + (window_len - hop_step) // 2
      c_end = min(c0 + hop_step, end_i)
      evaluated[c0:c_end] = True

      crossings = int(np.sum((sub[:-1] * sub[1:]) < 0))
      p2p = float(np.ptp(sub))

      if crossings >= OSC_MIN_CROSSINGS and p2p >= OSC_MIN_P2P:
        fft_vals = np.abs(np.fft.rfft(sub * hanning_win))
        dom_idx = 1 + int(np.argmax(fft_vals[1:]))
        dom_freq = float(freqs[dom_idx])

        osc[c0:c_end] = True
        osc_hz[c0:c_end] = dom_freq

  return osc, osc_hz, evaluated


def summarize_band_tables(
  samples: list[ControlSample],
  car_fingerprint: str,
  osc: np.ndarray,
  osc_hz: np.ndarray,
  evaluated: np.ndarray,
  ff_scale: np.ndarray,
  friction_scale: np.ndarray,
) -> None:
  if not samples:
    return

  torque_active = np.array([s.torque_active for s in samples], dtype=bool)
  steering_pressed = np.array([s.steering_pressed for s in samples], dtype=bool)
  desired_la = np.array([s.desired_la for s in samples])
  v_ego = np.array([s.v_ego for s in samples])

  base_mask = torque_active & (~steering_pressed) & evaluated & np.isfinite(desired_la)

  la_bins = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.7), (0.7, 1.1), (1.1, 1.6), (1.6, float("inf"))]
  la_labels = ["0-0.2", "0.2-0.4", "0.4-0.7", "0.7-1.1", "1.1-1.6", "1.6+"]

  v_bins = [(0.0, 6.0), (6.0, 10.0), (10.0, 14.0), (14.0, 20.0), (20.0, float("inf"))]
  v_labels = ["<6", "6-10", "10-14", "14-20", "20+"]

  def print_osc_table(title: str, dir_mask: np.ndarray):
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
          o_cell = osc[cell_mask]
          hz_cell = osc_hz[cell_mask]
          frac = float(np.mean(o_cell))
          hz_vals = hz_cell[o_cell & np.isfinite(hz_cell)]
          hz_str = f"{np.median(hz_vals):.1f}" if hz_vals.size > 0 else "--"
          n_str = f"({n})" if n < 50 else f"{n}"
          cell_val = f"{frac:.4f}/{hz_str}/{n_str}"
        row_str += f"{cell_val:>18s}"
      print(row_str)

  def print_gains_table(title: str, dir_mask: np.ndarray):
    print(f"\n{title}:")
    if car_fingerprint not in BOLT_2022_2023_CARS:
      print("  skipped (not a 2022-2023 Bolt)")
      return
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
          ff_cell = ff_scale[cell_mask]
          fr_cell = friction_scale[cell_mask]
          ff_valid = ff_cell[np.isfinite(ff_cell)]
          fr_valid = fr_cell[np.isfinite(fr_cell)]
          ff_str = f"{np.median(ff_valid):.4f}" if ff_valid.size > 0 else "--"
          fr_str = f"{np.median(fr_valid):.4f}" if fr_valid.size > 0 else "--"
          cell_val = f"{ff_str}/{fr_str}"
        row_str += f"{cell_val:>18s}"
      print(row_str)

  print_osc_table("Oscillation — Left (desired_la > 0) [frac/medHz/n]", desired_la > 0)
  print_osc_table("Oscillation — Right (desired_la < 0) [frac/medHz/n]", desired_la < 0)
  print_gains_table("Dynamic Gains — Left (desired_la > 0) [medFF/medFric]", desired_la > 0)
  print_gains_table("Dynamic Gains — Right (desired_la < 0) [medFF/medFric]", desired_la < 0)


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

  ff_scale, friction_scale, _, threshold_ratio = reconstruct_bolt_2022_2023_gains(
    control_samples, car_params.carFingerprint
  )
  summarize_bolt_dynamic_gains(control_samples, car_params.carFingerprint, ff_scale, friction_scale, threshold_ratio)
  osc, osc_hz, evaluated = detect_oscillations(control_samples)
  summarize_band_tables(control_samples, car_params.carFingerprint, osc, osc_hz, evaluated, ff_scale, friction_scale)


if __name__ == "__main__":
  main()
