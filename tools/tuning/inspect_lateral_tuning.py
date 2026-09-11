#!/usr/bin/env python3
import argparse
import sys
from collections import defaultdict, deque
import numpy as np

from openpilot.tools.lib.logreader import LogReader, ReadMode
from cereal.services import SERVICE_LIST
from openpilot.selfdrive.locationd.torqued import (
  TorqueEstimator,
  STEER_BUCKET_BOUNDS,
  MIN_VEL,
  LAT_ACC_THRESHOLD,
  STEER_MIN_THRESHOLD,
  MIN_ENGAGE_BUFFER,
  DT_MDL,
  HISTORY,
)
from openpilot.selfdrive.locationd.lagd import (
  LateralLagEstimator,
  BLOCK_SIZE,
  MIN_NCC,
  MIN_CONFIDENCE,
  MIN_LAT_ACCEL_RANGE,
  MAX_LAG,
  MAX_YAW_RATE_SANITY_CHECK,
  SMOOTH_K,
  SMOOTH_SIGMA,
  masked_symmetric_moving_average,
)
from openpilot.common.constants import ACCELERATION_DUE_TO_GRAVITY
from openpilot.selfdrive.locationd.helpers import Pose, PoseCalibrator


# Services publish valid=False until their own inputs settle. Invalid packets inside
# this window at route start are a normal startup transient, not evidence of gating.
STARTUP_SETTLE_SEC = 10.0

BUCKET_DESCRIPTIONS = [
  "Hard Left        [-0.50 to -0.30]",
  "Moderate Left    [-0.30 to -0.20]",
  "Gentle Left      [-0.20 to -0.10]",
  "Near-Center Left [-0.10 to  0.00]",
  "Near-Center Right[ 0.00 to +0.10]",
  "Gentle Right     [+0.10 to +0.20]",
  "Moderate Right   [+0.20 to +0.30]",
  "Hard Right       [+0.30 to +0.50]",
]


def fit_torque_params(points: list[list[float]] | np.ndarray) -> tuple[float, float, float] | None:
  """Exact SVD Total Least Squares algorithm from torqued.py"""
  pts = np.asarray(points)
  if len(pts) < 30:
    return None
  try:
    _, _, v = np.linalg.svd(pts, full_matrices=False)
    slope, offset = -v.T[0:2, 2] / v.T[2, 2]
    sin = np.sqrt(slope ** 2 / (slope ** 2 + 1))
    cos = np.sqrt(1 / (slope ** 2 + 1))
    rot = np.array([[cos, -sin], [sin, cos]])
    _, spread = np.matmul(pts[:, [0, 2]], rot).T
    friction = float(np.std(spread) * 1.5)
    return float(slope), float(offset), friction
  except Exception:
    return None


def compute_slopes_and_bracket(points: list[list[float]] | np.ndarray) -> dict | None:
  """Computes OLS-low, TLS (SVD), and OLS-high slopes, plus bracket width % and TLS sanity check."""
  pts = np.asarray(points)
  if len(pts) < 30:
    return None
  fit_tls = fit_torque_params(pts)
  if fit_tls is None:
    return None
  laf_tls, lao_tls, fric_tls = fit_tls

  x = pts[:, 0]  # steer torque
  y = pts[:, 2]  # latAccel

  x_c = x - np.mean(x)
  y_c = y - np.mean(y)

  var_x = float(np.var(x))
  var_y = float(np.var(y))
  cov_xy = float(np.mean(x_c * y_c))

  if var_x > 1e-12 and abs(cov_xy) > 1e-12:
    beta_low = cov_xy / var_x
    beta_high = var_y / cov_xy
    if beta_low > beta_high:
      beta_low, beta_high = beta_high, beta_low
    bracket_width = beta_high - beta_low
    bracket_pct = (bracket_width / laf_tls) * 100.0 if laf_tls > 0 else 0.0
  else:
    beta_low = laf_tls
    beta_high = laf_tls
    bracket_width = 0.0
    bracket_pct = 0.0

  tls_in_bracket = (beta_low <= laf_tls <= beta_high)
  # A non-positive slope means the fit did not converge to a physical torque->latAccel
  # relationship. bracket_pct is meaningless in that case and must not read as "narrow".
  degenerate = laf_tls <= 0.0

  return {
    "tls": fit_tls,
    "beta_low": beta_low,
    "beta_high": beta_high,
    "bracket_width": bracket_width,
    "bracket_pct": bracket_pct,
    "tls_in_bracket": tls_in_bracket,
    "degenerate": degenerate,
    "n": len(pts),
  }


class InstrumentedLateralLagEstimator(LateralLagEstimator):
  """Subclass of LateralLagEstimator that instruments every gating decision."""
  def __init__(self, CP, dt):
    super().__init__(CP, dt)
    self.stats = {
      # Point admission gates (evaluated every livePose frame at 20 Hz)
      "pose_frames": 0,
      "okay_points": 0,
      "rejected_lat_inactive": 0,
      "rejected_steering_pressed": 0,
      "rejected_steering_saturated": 0,
      "rejected_speed_low": 0,
      "rejected_sensors_invalid": 0,
      "rejected_calib_invalid": 0,
      "rejected_lat_accel_high": 0,
      "rejected_tracking_error_high": 0,
      "rejected_recovery_lockout": 0,

      # Estimate update gates (evaluated at 4 Hz)
      "eval_cycles": 0,
      "eval_rejected_not_enough_points": 0,
      "eval_rejected_not_valid_points": 0,
      "eval_rejected_lat_accel_range": 0,
      "eval_rejected_no_new_points": 0,
      "eval_rejected_low_ncc": 0,
      "eval_rejected_low_confidence": 0,
      "eval_successful_updates": 0,

      # Min / Max observed in estimator window
      "max_corr_seen": -1.0,
      "max_confidence_seen": -1.0,
      "max_window_range_seen": 0.0,
    }

  # Note: The body below is duplicated from LateralLagEstimator.update_points in
  # openpilot/selfdrive/locationd/lagd.py to instrument per-gate rejection statistics.
  # It must be kept in sync if lagd.py changes.
  def update_points(self):
    self.stats["pose_frames"] += 1

    la_desired = self.desired_curvature * self.v_ego * self.v_ego
    la_actual_pose = self.yaw_rate * self.v_ego

    fast = self.v_ego > self.min_vego
    turning = np.abs(self.yaw_rate) >= self.min_yr
    sensors_valid = self.pose_valid and np.abs(self.yaw_rate) < MAX_YAW_RATE_SANITY_CHECK and self.yaw_rate_std < MAX_YAW_RATE_SANITY_CHECK
    la_mag_valid = np.abs(la_actual_pose) <= self.max_lat_accel
    la_diff_valid = np.abs(la_desired - la_actual_pose) <= self.max_lat_accel_diff
    la_valid = la_mag_valid and la_diff_valid
    calib_valid = self.calibrator.calib_valid

    if not self.lat_active:
      self.last_lat_inactive_t = self.t
    if self.steering_pressed:
      self.last_steering_pressed_t = self.t
    if self.steering_saturated:
      self.last_steering_saturated_t = self.t
    if not sensors_valid or not la_valid:
      self.last_pose_invalid_t = self.t

    has_recovered = all(
      self.t - last_t >= self.min_recovery_buffer_sec
      for last_t in [self.last_lat_inactive_t, self.last_steering_pressed_t, self.last_steering_saturated_t, self.last_pose_invalid_t]
    )

    okay = self.lat_active and not self.steering_pressed and not self.steering_saturated and \
           fast and turning and has_recovered and calib_valid and sensors_valid and la_valid

    if okay:
      self.stats["okay_points"] += 1
    else:
      if not self.lat_active:
        self.stats["rejected_lat_inactive"] += 1
      elif self.steering_pressed:
        self.stats["rejected_steering_pressed"] += 1
      elif self.steering_saturated:
        self.stats["rejected_steering_saturated"] += 1
      elif not fast:
        self.stats["rejected_speed_low"] += 1
      elif not calib_valid:
        self.stats["rejected_calib_invalid"] += 1
      elif not sensors_valid:
        self.stats["rejected_sensors_invalid"] += 1
      elif not la_mag_valid:
        self.stats["rejected_lat_accel_high"] += 1
      elif not la_diff_valid:
        self.stats["rejected_tracking_error_high"] += 1
      elif not has_recovered:
        self.stats["rejected_recovery_lockout"] += 1

    self.points.update(self.t, la_desired, la_actual_pose, okay)

  def update_estimate(self):
    self.stats["eval_cycles"] += 1

    if not self.points_enough():
      self.stats["eval_rejected_not_enough_points"] += 1
      return

    times, desired, actual, okay = self.points.get()

    if not self.points_valid():
      self.stats["eval_rejected_not_valid_points"] += 1
      return

    lat_range = float(actual.max() - actual.min())
    self.stats["max_window_range_seen"] = max(self.stats["max_window_range_seen"], lat_range)

    if lat_range < MIN_LAT_ACCEL_RANGE:
      self.stats["eval_rejected_lat_accel_range"] += 1
      return

    is_valid = True
    if self.last_estimate_t != 0 and times[0] <= self.last_estimate_t:
      new_values_start_idx = next(-i for i, t in enumerate(reversed(times)) if t <= self.last_estimate_t)
      is_valid = is_valid and not (new_values_start_idx == 0 or not np.any(okay[new_values_start_idx:]))

    if not is_valid:
      self.stats["eval_rejected_no_new_points"] += 1
      return

    desired_smooth = masked_symmetric_moving_average(desired, okay, SMOOTH_K, SMOOTH_SIGMA)
    actual_smooth = masked_symmetric_moving_average(actual, okay, SMOOTH_K, SMOOTH_SIGMA)

    delay, corr, confidence = self.actuator_delay(desired_smooth, actual_smooth, okay, self.dt, MAX_LAG)
    self.stats["max_corr_seen"] = max(self.stats["max_corr_seen"], corr)
    self.stats["max_confidence_seen"] = max(self.stats["max_confidence_seen"], confidence)

    if corr < self.min_ncc:
      self.stats["eval_rejected_low_ncc"] += 1
      return
    if confidence < self.min_confidence:
      self.stats["eval_rejected_low_confidence"] += 1
      return

    self.block_avg.update(delay)
    self.last_estimate_t = self.t
    self.stats["eval_successful_updates"] += 1


def main():
  parser = argparse.ArgumentParser(
    description="Inspect openpilot lateral tuning, torque buckets, and steering delay (lagd) with per-gate diagnostics."
  )
  parser.add_argument("route", help="Route name (dongle/route), segment (dongle/route--0), or path to rlog/qlog file")
  parser.add_argument("--mode", choices=("auto", "qlog", "rlog"), default="rlog", help="Log reading mode (default: rlog)")
  parser.add_argument("--decimated", action="store_true", help="Evaluate torque bucket requirements using decimated limits")
  parser.add_argument("--max-lat-accel", type=float, default=LAT_ACC_THRESHOLD,
                      help="Lateral acceleration cutoff for rejection statistics only (default: 1.0 m/s^2; does not affect bucket replay)")
  parser.add_argument("--lag-sweep", action="store_true", help="Run offline alignment lag sweep from -0.15s to +0.15s")
  args = parser.parse_args()

  mode_map = {
    "auto": ReadMode.AUTO,
    "qlog": ReadMode.QLOG,
    "rlog": ReadMode.RLOG,
  }

  print(f"Loading route: {args.route} (mode={args.mode})...")
  try:
    log_reader = LogReader(args.route, default_mode=mode_map[args.mode], sort_by_time=True)
  except Exception as e:
    print(f"Error opening route '{args.route}': {e}", file=sys.stderr)
    sys.exit(1)

  car_params = None
  torque_estimator = None
  lag_estimator = None
  live_torque_snapshots = []
  live_delay_snapshots = []
  init_params = {}

  # Service health and arrival timing tracking
  service_counts = defaultdict(int)
  service_valid_counts = defaultdict(int)
  service_invalid_late = defaultdict(int)
  service_first_time = {}
  prev_mono_time = {}
  service_gaps = defaultdict(int)
  route_start_mono = None

  # livePose rate tracking
  pose_mono_times = []

  # Telemetry and post-processing collectors
  hist_len = int(HISTORY / DT_MDL)
  raw_points = defaultdict(lambda: deque(maxlen=hist_len))
  calibrator = PoseCalibrator()
  lag = 0.0

  # SVD post-processing collections (strictly fixed G-windows)
  post_proc_points_standard = []   # lat_acc <= 1.0
  post_proc_points_relaxed = []    # lat_acc <= 1.5
  post_proc_points_all = []        # lat_acc <= 2.5

  # Retained arrays for --lag-sweep
  all_steer_times = []
  all_steer_torques = []
  retained_pose_frames = []

  torque_stats = {
    "total_pose_frames": 0,
    "rejected_buffer_len": 0,
    "rejected_speed_low": 0,
    "rejected_lat_inactive": 0,
    "rejected_driver_override": 0,
    "rejected_steer_center": 0,
    "rejected_lat_accel_high": 0,
    "accepted_points": 0,
    "max_steer_seen": -float("inf"),
    "min_steer_seen": float("inf"),
    "max_lat_accel_seen": -float("inf"),
    "min_lat_accel_seen": float("inf"),
  }

  pose_frame_count = 0

  for msg in log_reader:
    try:
      which = msg.which()
    except Exception:
      continue

    cur_mono = msg.logMonoTime
    if route_start_mono is None:
      route_start_mono = cur_mono
    service_counts[which] += 1
    if hasattr(msg, "valid") and msg.valid:
      service_valid_counts[which] += 1
    elif (cur_mono - route_start_mono) * 1e-9 > STARTUP_SETTLE_SEC:
      service_invalid_late[which] += 1

    if which in prev_mono_time:
      delta_s = (cur_mono - prev_mono_time[which]) * 1e-9
      # Track gaps only for 20 Hz services where gap threshold = 1.0 / 8.0 = 125 ms
      if which in SERVICE_LIST and SERVICE_LIST[which].frequency == 20:
        gap_threshold = 1.0 / (SERVICE_LIST[which].frequency * 0.5 * 0.8)  # 0.125s (8 Hz floor)
        if delta_s > gap_threshold:
          service_gaps[which] += 1
    else:
      service_first_time[which] = cur_mono
    prev_mono_time[which] = cur_mono

    if which == "initData":
      init_params = {entry.key: entry.value for entry in msg.initData.params.entries}
      continue

    if which == "carParams" and car_params is None:
      car_params = msg.carParams
      dt_pose = 1.0 / SERVICE_LIST['livePose'].frequency
      torque_estimator = TorqueEstimator(car_params, decimated=args.decimated, track_all_points=True)
      lag_estimator = InstrumentedLateralLagEstimator(car_params, dt_pose)
      continue

    if car_params is None:
      continue

    t = msg.logMonoTime * 1e-9

    # Replay through official TorqueEstimator
    if torque_estimator is not None and which in (
      "carControl", "carOutput", "carState", "liveCalibration", "livePose", "liveDelay"
    ):
      torque_estimator.handle_log(t, which, getattr(msg, which))

    # Replay through InstrumentedLateralLagEstimator
    if lag_estimator is not None and which in (
      "carControl", "carState", "controlsState", "liveCalibration", "livePose"
    ):
      lag_estimator.handle_log(t, which, getattr(msg, which))

    # Detailed rejection telemetry and point collection
    if which == "carControl":
      raw_points["carControl_t"].append(t + lag)
      raw_points["lat_active"].append(msg.carControl.latActive)
    elif which == "carOutput":
      raw_points["carOutput_t"].append(t + lag)
      torque_val = -msg.carOutput.actuatorsOutput.torque
      raw_points["steer_torque"].append(torque_val)
      all_steer_times.append(t)
      all_steer_torques.append(torque_val)
    elif which == "carState":
      raw_points["carState_t"].append(t + lag)
      raw_points["vego"].append(msg.carState.vEgo)
      raw_points["steer_override"].append(msg.carState.steeringPressed)
    elif which == "liveCalibration":
      calibrator.feed_live_calib(msg.liveCalibration)
    elif which == "liveDelay":
      lag = msg.liveDelay.lateralDelay
      live_delay_snapshots.append(msg.liveDelay)
    elif which == "liveTorqueParameters":
      live_torque_snapshots.append(msg.liveTorqueParameters)
    elif which == "livePose":
      pose_mono_times.append(cur_mono)
      pose_frame_count += 1
      torque_stats["total_pose_frames"] += 1

      # Update lag estimator points at 20 Hz
      if lag_estimator is not None:
        lag_estimator.update_points()
        # 4 Hz estimate evaluation
        if pose_frame_count % 5 == 0:
          lag_estimator.update_estimate()

      if len(raw_points["steer_torque"]) == hist_len:
        device_pose = Pose.from_live_pose(msg.livePose)
        calibrated_pose = calibrator.build_calibrated_pose(device_pose)
        yaw_rate = calibrated_pose.angular_velocity.yaw
        roll = device_pose.orientation.roll

        lat_active = np.interp(
          np.arange(t - MIN_ENGAGE_BUFFER, t + lag, DT_MDL),
          raw_points["carControl_t"], raw_points["lat_active"]
        ).astype(bool)
        steer_override = np.interp(
          np.arange(t - MIN_ENGAGE_BUFFER, t + lag, DT_MDL),
          raw_points["carState_t"], raw_points["steer_override"]
        ).astype(bool)
        vego = float(np.interp(t, raw_points["carState_t"], raw_points["vego"]))
        steer = float(np.interp(t, raw_points["carOutput_t"], raw_points["steer_torque"]).item())
        lateral_acc = float((vego * yaw_rate) - (np.sin(roll) * ACCELERATION_DUE_TO_GRAVITY).item())

        torque_stats["max_steer_seen"] = max(torque_stats["max_steer_seen"], steer)
        torque_stats["min_steer_seen"] = min(torque_stats["min_steer_seen"], steer)
        torque_stats["max_lat_accel_seen"] = max(torque_stats["max_lat_accel_seen"], lateral_acc)
        torque_stats["min_lat_accel_seen"] = min(torque_stats["min_lat_accel_seen"], lateral_acc)

        # Full kinematic rejection chain
        if not all(lat_active):
          torque_stats["rejected_lat_inactive"] += 1
        elif any(steer_override):
          torque_stats["rejected_driver_override"] += 1
        elif vego <= MIN_VEL:
          torque_stats["rejected_speed_low"] += 1
        else:
          # Passed first 3 kinematic gates: retain for lag sweep before deadband evaluation
          retained_pose_frames.append((t, vego, yaw_rate, roll))

          if abs(steer) <= STEER_MIN_THRESHOLD:
            torque_stats["rejected_steer_center"] += 1
          else:
            # Point passed all 4 kinematic gates!
            # 1. Rejection statistics driven by --max-lat-accel (default: LAT_ACC_THRESHOLD)
            if abs(lateral_acc) > args.max_lat_accel:
              torque_stats["rejected_lat_accel_high"] += 1
            else:
              torque_stats["accepted_points"] += 1

            # 2. SVD post-processing collection strictly constant across 1.0, 1.5, 2.5 windows
            pt = [steer, 1.0, lateral_acc]
            if abs(lateral_acc) <= 1.0:
              post_proc_points_standard.append(pt)
            if abs(lateral_acc) <= 1.5:
              post_proc_points_relaxed.append(pt)
            if abs(lateral_acc) <= 2.5:
              post_proc_points_all.append(pt)
      else:
        torque_stats["rejected_buffer_len"] += 1

  if car_params is None:
    print("Error: No carParams packet found in log.", file=sys.stderr)
    sys.exit(1)

  # Compute observed livePose rate
  observed_livepose_hz = 0.0
  if len(pose_mono_times) > 1:
    duration_s = (pose_mono_times[-1] - pose_mono_times[0]) * 1e-9
    if duration_s > 0:
      observed_livepose_hz = (len(pose_mono_times) - 1) / duration_s

  # Prominent warning if qlog decimation or heavy frame loss is detected
  if observed_livepose_hz < 15.0:
    print("\n" + "!" * 78)
    print(" WARNING: LOW LIVEPOSE RATE DETECTED (qlog data or heavy frame loss likely)")
    print(f" Observed livePose rate: {observed_livepose_hz:.1f} Hz (expected ~20 Hz)")
    print(" ALL STEERING DELAY (lagd) RESULTS IN SECTION 4 ARE INVALID.")
    print(" lagd requires un-decimated 20 Hz rlog data for cross-correlation.")
    print("!" * 78 + "\n")

  # ==========================================
  # REPORT OUTPUT
  # ==========================================
  print("\n" + "=" * 78)
  print(" LATERAL TUNING & ESTIMATORS COMPREHENSIVE TELEMETRY REPORT")
  print("=" * 78)
  print(f"Car Fingerprint:      {car_params.carFingerprint}")
  print(f"Brand:                {car_params.brand}")
  print(f"Lateral Tuning Type:  {car_params.lateralTuning.which()}")
  stock_laf, stock_lao, stock_fric = 0.0, 0.0, 0.0
  if car_params.lateralTuning.which() == "torque":
    tq = car_params.lateralTuning.torque
    stock_laf, stock_lao, stock_fric = tq.latAccelFactor, tq.latAccelOffset, tq.friction
    print(f"Stock Torque Tune:    latAccelFactor={stock_laf:.4f}, friction={stock_fric:.4f}, latAccelOffset={stock_lao:.4f}")
  print(f"Stock Steer Delay:    {car_params.steerActuatorDelay:.4f}s")
  print(f"Observed livePose (post-carParams): {observed_livepose_hz:.1f} Hz")

  if init_params:
    def _get_b(k):
      return init_params.get(k, b"0") == b"1"
    print(f"Toggles at Init:      AdvancedLateralTune={_get_b('AdvancedLateralTune')}, "
          f"ForceAutoTune={_get_b('ForceAutoTune')}, "
          f"ForceAutoTuneOff={_get_b('ForceAutoTuneOff')}")

  # --------------------------------------------------------------------------
  # 1. ON-DEVICE LOGGED SNAPSHOTS (liveTorqueParameters & liveDelay)
  # --------------------------------------------------------------------------
  print("\n" + "-" * 78)
  print(" 1. ON-DEVICE LOGGED TELEMETRY SNAPSHOTS")
  print("-" * 78)
  print("[ liveTorqueParameters ]")
  first_tq, last_tq = None, None
  if live_torque_snapshots:
    first_tq = live_torque_snapshots[0]
    last_tq = live_torque_snapshots[-1]
    print(f"  Messages Logged:    {len(live_torque_snapshots)}")
    print(f"  Route Start State:  totalBucketPoints={first_tq.totalBucketPoints}, calPerc={first_tq.calPerc}%, liveValid={first_tq.liveValid}")
    print(f"  Route End State:    totalBucketPoints={last_tq.totalBucketPoints}, calPerc={last_tq.calPerc}%, liveValid={last_tq.liveValid}")
    print(f"  Filtered Output:    latAccelFactor={last_tq.latAccelFactorFiltered:.4f}, friction={last_tq.frictionCoefficientFiltered:.4f}, offset={last_tq.latAccelOffsetFiltered:.4f}")
    if hasattr(last_tq, "latAccelFactorRaw"):
      print(f"  Raw Fitted Output:  latAccelFactorRaw={last_tq.latAccelFactorRaw:.4f}, frictionRaw={last_tq.frictionCoefficientRaw:.4f}, offsetRaw={last_tq.latAccelOffsetRaw:.4f}")
  else:
    print("  No liveTorqueParameters messages logged in this route.")

  print("\n[ liveDelay ]")
  first_ld, last_ld = None, None
  if live_delay_snapshots:
    first_ld = live_delay_snapshots[0]
    last_ld = live_delay_snapshots[-1]
    print(f"  Messages Logged:    {len(live_delay_snapshots)}")
    print(f"  Route Start State:  calPerc={first_ld.calPerc}%, validBlocks={first_ld.validBlocks}, status={first_ld.status}")
    print(f"  Route End State:    calPerc={last_ld.calPerc}%, validBlocks={last_ld.validBlocks}, status={last_ld.status}")
    print(f"  Commanded Delay:    {last_ld.lateralDelay:.4f}s")
    print(f"  Estimate (Mean):    {last_ld.lateralDelayEstimate:.4f}s ± {last_ld.lateralDelayEstimateStd:.4f}s")
  else:
    print("  No liveDelay messages logged in this route.")

  # --------------------------------------------------------------------------
  # 2. SERVICE HEALTH & ALL_CHECKS() GATING AUDIT
  # --------------------------------------------------------------------------
  print("\n" + "-" * 78)
  print(" 2. SERVICE HEALTH & ALL_CHECKS() GATING AUDIT")
  print("    (Gaps: 20 Hz services only, freq_ok band [8.0, 24.0] Hz, interval > 125 ms)")
  print(f"    (Inv>{int(STARTUP_SETTLE_SEC)}s: invalid packets after the startup transient — startup ones are normal)")
  print("-" * 78)
  print(f"{'Service':<16} {'Pkts':<7} {'Valid%':<7} {'Inv>10s':<8} {'MeanHz':<8} {'Gaps':<12} {'Status'}")
  print("-" * 78)
  critical_services = ["livePose", "liveCalibration", "carState", "controlsState", "carControl", "starpilotPlan", "radarState"]
  for s in critical_services:
    cnt = service_counts[s]
    if cnt == 0:
      print(f"{s:<16} {'0':<7} {'-':<7} {'-':<8} {'-':<8} {'-':<12} NOT IN LOG")
      continue

    vcnt = service_valid_counts[s]
    inv_late = service_invalid_late[s]
    pct = (vcnt / cnt * 100.0)
    dur = (prev_mono_time[s] - service_first_time[s]) * 1e-9 if s in service_first_time and s in prev_mono_time else 0.0
    mean_hz = (cnt - 1) / dur if (cnt > 1 and dur > 0) else 0.0

    service_freq = SERVICE_LIST[s].frequency if s in SERVICE_LIST else 0
    if service_freq == 20:
      gaps = service_gaps[s]
      gap_str = str(gaps)
      has_gap_risk = (gaps > 0)
    elif service_freq == 100:
      # Consumer polls at 20 Hz with conflate=True, so it never receives the logged
      # 100 Hz rate. Log inter-arrival says nothing about the consumer's freq_ok.
      gap_str = "n/a (100Hz)"
      has_gap_risk = False
    elif service_freq == 4:
      gap_str = "n/a (4Hz)"
      has_gap_risk = False
    else:
      gap_str = "n/a"
      has_gap_risk = False

    status_str = "GATING RISK" if (inv_late > 0 or has_gap_risk) else "OK"
    print(f"{s:<16} {cnt:<7} {pct:<7.1f} {inv_late:<8} {mean_hz:<8.1f} {gap_str:<12} {status_str}")

  print("\nCaveat: 'valid' is only one of three all_checks() conditions. 'alive' and 'freq_ok'")
  print("as seen by the consuming process are not recoverable from a log. Invalid packets or")
  print("gaps are evidence of gating; a clean table does not rule it out.")

  # --------------------------------------------------------------------------
  # 3. REPLAY vs DEVICE FIDELITY
  # --------------------------------------------------------------------------
  print("\n" + "-" * 78)
  print(" 3. REPLAY vs DEVICE FIDELITY")
  print("-" * 78)

  total_pts = len(torque_estimator.filtered_points) if torque_estimator is not None else 0
  b_blocks = lag_estimator.block_avg.valid_blocks if lag_estimator is not None else 0
  b_idx = lag_estimator.block_avg.idx if lag_estimator is not None else 0

  if first_tq is not None and last_tq is not None:
    tq_delta = last_tq.totalBucketPoints - first_tq.totalBucketPoints
    dev_tq_str = f"{first_tq.totalBucketPoints} -> {last_tq.totalBucketPoints} ({tq_delta:+d})"
  else:
    dev_tq_str = "N/A (no snapshots)"

  if first_ld is not None and last_ld is not None:
    ld_delta = last_ld.validBlocks - first_ld.validBlocks
    dev_ld_str = f"{first_ld.validBlocks} -> {last_ld.validBlocks} ({ld_delta:+d})"
  else:
    dev_ld_str = "N/A (no snapshots)"

  print(f"{'Metric':<24} {'Device Start -> End (Delta)':<35} {'Replay This Route'}")
  print("-" * 78)
  print(f"{'Torque bucket points':<24} {dev_tq_str:<35} {total_pts}")
  print(f"{'Lag blocks':<24} {dev_ld_str:<35} {b_blocks}")
  print("-" * 78)
  print("Caveats:")
  print("- The replay bypasses all_checks(); the device does not. A replay total far above")
  print("  the device delta is consistent with on-device gating.")
  print("- Device totalBucketPoints is not zero at route start — torqued restores cached")
  print("  points from the LiveTorqueParameters param. Use the delta, not the end value.")
  print("- Each bucket saturates at POINTS_PER_BUCKET = 1500, so once buckets are full")
  print("  the device delta compresses toward zero for reasons unrelated to gating.")

  # --------------------------------------------------------------------------
  # 4. STEERING DELAY (lagd) REPLAY & GATE BREAKDOWN
  # --------------------------------------------------------------------------
  print("\n" + "-" * 78)
  print(" 4. STEERING DELAY (lagd) REPLAY & GATE BREAKDOWN")
  print("-" * 78)
  if lag_estimator is not None:
    ls = lag_estimator.stats
    valid_mean_lag, valid_std, cur_mean, cur_std = lag_estimator.block_avg.get()

    print(f"Blocks earned on THIS ROUTE:  {b_blocks} (replay starts from zero; device carries progress across drives)")
    print(f"Samples toward next block:    {b_idx} / {BLOCK_SIZE} ({b_idx / BLOCK_SIZE * 100:.1f}%)")
    print(f"Replayed Estimate:            {cur_mean:.4f}s ± {cur_std:.4f}s (valid mean={valid_mean_lag:.4f}s ± {valid_std:.4f}s)")
    print(f"Total Pose Cycles:            {ls['pose_frames']}")
    print(f"Points Accepted:              {ls['okay_points']} (passed all kinematic criteria)")

    print("\n[ Point Admission Gating (20 Hz) ]")
    print(f"  - Speed <= 15 m/s (33.5 mph):             {ls['rejected_speed_low']:<8} (City turns / slow driving)")
    print(f"  - Steering Pressed (driver hand on wheel):{ls['rejected_steering_pressed']:<8} (Driver intervention)")
    print(f"  - Steering Saturated (torque limit hit):  {ls['rejected_steering_saturated']:<8} (Actuator limit reached)")
    print(f"  - Lateral Inactive (< 2s buffer):          {ls['rejected_lat_inactive']:<8} (Disengaged or paused)")
    print(f"  - Tracking Error > 0.6 m/s^2:             {ls['rejected_tracking_error_high']:<8} (Car not following model path)")
    print(f"  - Excessive Lat Accel (> 2.0 m/s^2):      {ls['rejected_lat_accel_high']:<8} (Over lateral threshold)")
    print(f"  - Post-Event 2.0s Lockout Window:         {ls['rejected_recovery_lockout']:<8} (Cooling down after touch/sat/error)")
    print(f"  - Calibration Invalid:                    {ls['rejected_calib_invalid']:<8} (Pose uncalibrated)")

    print("\n[ 4 Hz Evaluation Gating (Cross-Correlation & Blocks) ]")
    print(f"  - Total Evaluation Cycles (4 Hz):         {ls['eval_cycles']}")
    print(f"  - Successful Block Updates:               {ls['eval_successful_updates']:<8} ({ls['eval_successful_updates'] / BLOCK_SIZE:.2f} blocks)")
    print(f"  - Rejected: Insufficient Window (< 25s):  {ls['eval_rejected_not_enough_points']}")
    print(f"  - Rejected: Low Okay Points (< 25s okay): {ls['eval_rejected_not_valid_points']}")
    print(f"  - Rejected: Lat Accel Flat (< 0.5 m/s^2): {ls['eval_rejected_lat_accel_range']:<8} (Straight highway driving!)")
    print(f"  - Rejected: No New Points Since Last:     {ls['eval_rejected_no_new_points']}")
    print(f"  - Rejected: Low NCC Cross-Corr (< 0.95):  {ls['eval_rejected_low_ncc']:<8} (Shape mismatch)")
    print(f"  - Rejected: Low Confidence (< 0.70):      {ls['eval_rejected_low_confidence']:<8} (Flat plateau / circular curve!)")
    print(f"\n  Peak Normalized Cross-Corr Seen:          {ls['max_corr_seen']:.4f} (Threshold: {MIN_NCC})")
    print(f"  Peak Confidence Peak Sharpness Seen:      {ls['max_confidence_seen']:.4f} (Threshold: {MIN_CONFIDENCE})")
    print(f"  Max Lat Accel Range in 60s Window Seen:   {ls['max_window_range_seen']:.4f} m/s^2 (Threshold: {MIN_LAT_ACCEL_RANGE})")

  # --------------------------------------------------------------------------
  # 5. 8-BUCKET TORQUE REPLAY BREAKDOWN
  # --------------------------------------------------------------------------
  print("\n" + "-" * 78)
  mode_label = "DECIMATED MODE (/10)" if args.decimated else "STANDARD MODE"
  print(f" 5. 8-BUCKET TORQUE REPLAY BREAKDOWN ({mode_label})")
  print(f"    (Note: Bucket replay uses fixed LAT_ACC_THRESHOLD={LAT_ACC_THRESHOLD} m/s^2 from torqued.py)")
  print(f"    (Rejection statistics below use cutoff={args.max_lat_accel} m/s^2 from --max-lat-accel)")
  print("-" * 78)

  buckets = torque_estimator.filtered_points.buckets
  min_bucket_pts = torque_estimator.filtered_points.buckets_min_points
  min_total = torque_estimator.filtered_points.min_points_total

  print(f"{'Idx':<4} {'Description':<32} {'Points':<8} {'Required':<10} {'Fill %':<10} {'Status'}")
  print("-" * 78)

  lowest_pct = 100.0
  limiting_bucket_idx = 0
  empty_buckets = []

  for idx, ((low, high), desc) in enumerate(zip(STEER_BUCKET_BOUNDS, BUCKET_DESCRIPTIONS)):
    count = len(buckets[(low, high)])
    req = min_bucket_pts[(low, high)]
    pct = min(count / req * 100.0, 100.0) if req > 0 else 100.0
    if pct < lowest_pct:
      lowest_pct = pct
      limiting_bucket_idx = idx

    if count == 0:
      status = "EMPTY (0 pts) <--- BLOCKING"
      empty_buckets.append(idx)
    elif count < req:
      status = f"IN PROGRESS ({count}/{int(req)})"
    else:
      status = "SATISFIED"

    print(f"{idx:<4} {desc:<32} {count:<8} {int(req):<10} {pct:>5.1f}%     {status}")

  print("-" * 78)
  total_pct = min(total_pts / min_total * 100.0, 100.0)
  calc_cal_perc = int((total_pct + lowest_pct) / 2)

  print(f"Total Points:         {total_pts} / {min_total} required ({total_pct:.1f}%)")
  print(f"Lowest Bucket:        Bucket {limiting_bucket_idx} ({lowest_pct:.1f}% filled)")
  print(f"Calculated calPerc:   ({total_pct:.1f}% total + {lowest_pct:.1f}% bucket) / 2 = {calc_cal_perc}%")
  print(f"Calculable (All > 0): {torque_estimator.filtered_points.is_calculable()}")
  print(f"Valid (All Met):      {torque_estimator.filtered_points.is_valid()} -> liveValid={torque_estimator.filtered_points.is_valid()}")

  print("\nTorque Point Rejections:")
  print(f"  - Speed <= 15 m/s (33.5 mph):             {torque_stats['rejected_speed_low']:<8}")
  print(f"  - Lat Accel > Cutoff ({args.max_lat_accel} m/s^2):      {torque_stats['rejected_lat_accel_high']:<8}")
  print(f"  - Steering Near Center (|torque|<=0.02):  {torque_stats['rejected_steer_center']:<8}")
  print(f"  - Driver Override (wheel touched < 2s):   {torque_stats['rejected_driver_override']:<8}")
  print(f"  - Lateral Inactive (< 2s buffer):         {torque_stats['rejected_lat_inactive']:<8}")
  print(f"  Steering Torque Range Observed:           [{torque_stats['min_steer_seen']:+.4f} to {torque_stats['max_steer_seen']:+.4f}]")
  print(f"  Lateral Accel Range Observed:             [{torque_stats['min_lat_accel_seen']:+.4f} to {torque_stats['max_lat_accel_seen']:+.4f}] m/s^2")

  # --------------------------------------------------------------------------
  # 6. POST-PROCESSING PARAMETER ESTIMATION (OFFLINE SVD & OLS BRACKETS)
  # --------------------------------------------------------------------------
  print("\n" + "=" * 78)
  print(" 6. POST-PROCESSING PARAMETER ESTIMATION (OFFLINE SVD & OLS BRACKETS)")
  print("=" * 78)

  bracket_std = compute_slopes_and_bracket(post_proc_points_standard)
  bracket_rel = compute_slopes_and_bracket(post_proc_points_relaxed)
  bracket_all = compute_slopes_and_bracket(post_proc_points_all)

  fit_std = bracket_std["tls"] if bracket_std is not None else None
  fitted_offset = fit_std[1] if fit_std is not None else 0.0

  # Directional split offset-corrected by fitted offset with margin 0.20
  left_pts = [p for p in post_proc_points_standard if (p[2] - fitted_offset) > 0.20]
  right_pts = [p for p in post_proc_points_standard if (p[2] - fitted_offset) < -0.20]
  fit_left = fit_torque_params(left_pts)
  fit_right = fit_torque_params(right_pts)

  def _fmt_bracket(title, b_res):
    if b_res is None:
      print(f"{title:<38} [Insufficient points to fit]")
      return
    laf, lao, fr = b_res["tls"]
    f_amp = fr * laf
    diff_pct = ((laf / stock_laf) - 1.0) * 100.0 if stock_laf > 0 else 0.0
    print(f"{title:<38} n={b_res['n']:<5} -> latAccelFactor={laf:6.4f} ({diff_pct:+.1f}%), offset={lao:+7.4f}, friction={fr:6.4f}, fricAmp={f_amp:6.4f}")
    if b_res.get("degenerate", False):
      print(f"  DEGENERATE: TLS factor is non-positive ({laf:.4f}); the fit did not converge. Bracket below is meaningless.")
    print(f"  OLS Bracket: OLS-low = {b_res['beta_low']:6.4f} | TLS = {laf:6.4f} | OLS-high = {b_res['beta_high']:6.4f} (bracket width: {b_res['bracket_width']:.4f}, {b_res['bracket_pct']:.1f}% of TLS)")
    if not b_res.get("tls_in_bracket", True):
      print(f"  WARNING: TLS factor ({laf:.4f}) fell outside OLS bracket [{b_res['beta_low']:.4f}, {b_res['beta_high']:.4f}]. Intercept term is likely distorting the fit.")

  print(f"Stock Baseline Config:                       -> latAccelFactor={stock_laf:6.4f}, offset={stock_lao:+7.4f}, friction={stock_fric:6.4f}")
  print("-" * 78)
  _fmt_bracket("Standard Filtered (latAccel <= 1.0 m/s^2):", bracket_std)
  _fmt_bracket("Relaxed Filtered  (latAccel <= 1.5 m/s^2):", bracket_rel)
  _fmt_bracket("All Curve Data    (latAccel <= 2.5 m/s^2):", bracket_all)

  print("-" * 78)
  print(f"Directional Asymmetry Breakdown (Standard window <= 1.0 m/s^2, offset-corrected by {fitted_offset:+.4f} m/s^2, margin ±0.20):")
  def _fmt_fit(title, fit_res, n_pts):
    if fit_res is None:
      print(f"{title:<48} n={n_pts:<5}  [Insufficient points to fit]")
      return
    laf, lao, fr = fit_res
    f_amp = fr * laf
    diff_pct = ((laf / stock_laf) - 1.0) * 100.0 if stock_laf > 0 else 0.0
    print(f"{title:<46} n={n_pts:<5} -> latAccelFactor={laf:6.4f} ({diff_pct:+.1f}%), offset={lao:+7.4f}, friction={fr:6.4f}, fricAmp={f_amp:6.4f}")

  _fmt_fit("  Left Curves  (latAccel - offset > +0.20 m/s^2):", fit_left, len(left_pts))
  _fmt_fit("  Right Curves (latAccel - offset < -0.20 m/s^2):", fit_right, len(right_pts))

  # --------------------------------------------------------------------------
  # 7. OPT-IN LAG SWEEP ANALYSIS (--lag-sweep)
  # --------------------------------------------------------------------------
  if args.lag_sweep:
    print("\n" + "=" * 78)
    if live_delay_snapshots:
      print(f" 7. LAG SWEEP ANALYSIS (-0.15s to +0.15s relative to route delay {lag:.4f}s)")
      print("    (Note: Sweep effective lag is built from the last liveDelay.lateralDelay seen in log,")
      print("     applied retroactively to the whole route)")
    else:
      print(" 7. LAG SWEEP ANALYSIS (-0.15s to +0.15s relative to 0.0s)")
      print("    (Note: No liveDelay logged in route; sweep is centred on 0.0s)")
    print("=" * 78)
    if len(retained_pose_frames) >= 30 and len(all_steer_times) >= 30:
      steer_t_arr = np.array(all_steer_times)
      steer_v_arr = np.array(all_steer_torques)
      pose_t_arr = np.array([p[0] for p in retained_pose_frames])
      vego_arr = np.array([p[1] for p in retained_pose_frames])
      yaw_arr = np.array([p[2] for p in retained_pose_frames])
      roll_arr = np.array([p[3] for p in retained_pose_frames])
      lat_acc_arr = (vego_arr * yaw_arr) - np.sin(roll_arr) * ACCELERATION_DUE_TO_GRAVITY

      candidate_offsets = np.round(np.arange(-0.150, 0.1501, 0.025), 3)
      sweep_results = []

      print(f"{'Lag Offset':<12} {'Effective Lag':<15} {'Points (n)':<12} {'TLS Factor':<12} {'Offset':<10} {'Friction':<10} {'Bracket Width':<15} {'Bracket %'}")
      print("-" * 78)

      for off in candidate_offsets:
        raw_eff_lag = lag + off
        eff_lag = max(0.0, raw_eff_lag)
        # A clamped candidate duplicates the eff_lag=0 row and would otherwise look like
        # a flat, insensitive region of the sweep. Print it, but keep it out of selection.
        clamped = raw_eff_lag < 0.0
        steer_at_pose = np.interp(pose_t_arr - eff_lag, steer_t_arr, steer_v_arr)
        mask = (np.abs(lat_acc_arr) <= 1.0) & (np.abs(steer_at_pose) > STEER_MIN_THRESHOLD)
        n_pts = int(np.sum(mask))

        if clamped:
          print(f"{off:+7.3f}s     {eff_lag:6.3f}s          {n_pts:<12} [CLAMPED at 0.0s - excluded from selection]")
          continue

        if n_pts >= 30:
          pts_cand = np.column_stack([steer_at_pose[mask], np.ones(n_pts), lat_acc_arr[mask]])
          b_res = compute_slopes_and_bracket(pts_cand)
          if b_res is not None and b_res["degenerate"]:
            # Same reasoning as the summary: a non-positive slope has a meaningless
            # bracket width and must not be able to win the min() below.
            print(f"{off:+7.3f}s     {eff_lag:6.3f}s          {n_pts:<12} [DEGENERATE fit ({b_res['tls'][0]:.4f}) - excluded from selection]")
            continue
          if b_res is not None:
            laf_s, lao_s, fr_s = b_res["tls"]
            bw_s = b_res["bracket_width"]
            bp_s = b_res["bracket_pct"]
            sweep_results.append({
              "offset": off,
              "eff_lag": eff_lag,
              "n": n_pts,
              "laf": laf_s,
              "lao": lao_s,
              "fr": fr_s,
              "bracket_width": bw_s,
              "bracket_pct": bp_s,
            })
            print(f"{off:+7.3f}s     {eff_lag:6.3f}s          {n_pts:<12} {laf_s:6.4f}       {lao_s:+7.4f}    {fr_s:6.4f}     {bw_s:8.4f}        {bp_s:5.1f}%")
            continue
        print(f"{off:+7.3f}s     {eff_lag:6.3f}s          {n_pts:<12} [Insufficient Points / Non-converged]")

      # Permanent self-check: off = +0.000s vs Section 6 Standard TLS factor
      zero_res = next((r for r in sweep_results if abs(r["offset"]) < 1e-6), None)
      if zero_res is not None and fit_std is not None:
        laf_std = fit_std[0]
        laf_zero = zero_res["laf"]
        abs_diff = abs(laf_zero - laf_std)
        rel_diff = abs_diff / laf_std if laf_std > 0 else float('inf')
        # Relative, not absolute: an absolute tolerance is far tighter on a factor of 2.5
        # than on 5.6. Disagreement beyond 0.1% means the alignment is wrong.
        check_status = "PASS (agreement within tolerance)" if rel_diff < 1e-3 else "MISMATCH (investigate alignment)"
        print("-" * 78)
        print("Self-check (off = +0.000s vs Section 6 Standard TLS):")
        print(f"  Section 6 Standard TLS: {laf_std:6.4f}")
        print(f"  Sweep off = +0.000s:    {laf_zero:6.4f} (abs diff: {abs_diff:.4e}, rel: {rel_diff:.2%}) -> {check_status}")

      if sweep_results:
        best_sweep = min(sweep_results, key=lambda r: r["bracket_width"])
        print("-" * 78)
        print("Optimal alignment lag (selected by min absolute OLS bracket width):")
        print(f"  offset {best_sweep['offset']:+.3f}s (effective {best_sweep['eff_lag']:.3f}s) with bracket width {best_sweep['bracket_width']:.4f} ({best_sweep['bracket_pct']:.1f}% of TLS)")
    else:
      print("Insufficient retained pose or steer data to execute lag sweep.")

  # --------------------------------------------------------------------------
  # 8. SUMMARY DIAGNOSTIC TAKEAWAYS
  # --------------------------------------------------------------------------
  print("\n" + "=" * 78)
  print(" SUMMARY DIAGNOSTIC TAKEAWAYS")
  print("=" * 78)

  # 1. Dominant lagd rejection cause (always printed)
  if lag_estimator is not None:
    ls = lag_estimator.stats
    eval_rejections = {
      "Insufficient window (< 25s total points)": ls["eval_rejected_not_enough_points"],
      "Low okay points (< 25s clean points)": ls["eval_rejected_not_valid_points"],
      "Road too straight (lateral accel range < 0.5 m/s^2)": ls["eval_rejected_lat_accel_range"],
      "No new kinematic points since last evaluation": ls["eval_rejected_no_new_points"],
      "Low normalized cross-correlation (< 0.95)": ls["eval_rejected_low_ncc"],
      "Low peak confidence (< 0.70, constant-radius flat plateau)": ls["eval_rejected_low_confidence"],
    }
    dominant_cause, max_count = max(eval_rejections.items(), key=lambda item: item[1])
    successful_updates = ls["eval_successful_updates"]
    blocks_earned = successful_updates / BLOCK_SIZE
    print(f"-> STEER DELAY (lagd): Route produced {successful_updates} sample updates ({blocks_earned:.2f} blocks).")
    if max_count > 0:
      print(f"   Dominant rejection cause: {dominant_cause} ({max_count} windows).")

  # 2. Torqued replay bucket status (non-assertive measurement language)
  if torque_estimator is not None:
    if empty_buckets:
      print(f"-> TORQUE (torqued): Replay shows empty buckets: {empty_buckets}.")
      print("   Note: Replay bypasses on-device all_checks(); compare against the device delta in Section 3 above.")
    else:
      print("-> TORQUE (torqued): All 8 buckets received points in replay.")

  # 3. OLS bracket certainty
  if bracket_std is not None:
    width_pct = bracket_std["bracket_pct"]
    if bracket_std.get("degenerate", False):
      print(f"-> FIT DEGENERATE: Standard-window TLS factor is non-positive ({bracket_std['tls'][0]:.4f}).")
      print("   The fit did not converge; the bracket width is not meaningful.")
    elif width_pct > 30.0:
      print(f"-> FIT UNCERTAINTY: Standard-window OLS bracket width is {width_pct:.1f}% of TLS (> 30%).")
      print("   The data does not constrain latAccelFactor well enough to justify writing it into CarParams.")
    else:
      print(f"-> FIT CERTAINTY: Standard-window OLS bracket width is {width_pct:.1f}% of TLS (<= 30%, well-constrained).")

  print("=" * 78 + "\n")


if __name__ == "__main__":
  main()
