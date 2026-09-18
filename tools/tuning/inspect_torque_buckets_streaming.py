#!/usr/bin/env python3
"""Memory-bounded version of tools/tuning/inspect_torque_buckets.py for long routes.

inspect_torque_buckets.py's LogReader caches every decoded segment forever in
LogReader.__lrs (never evicted), so a multi-hour route holds every decompressed
rlog in memory simultaneously. This script processes one segment at a time with
a fresh LogReader per segment (so the old one is garbage-collected before the
next is decoded) and only carries the lightweight running state forward.
"""
import argparse
import gc
import sys
from collections import defaultdict, deque

import numpy as np

from openpilot.tools.tuning.inspect_torque_buckets import (
  resolve_local_paths, BUCKET_DESCRIPTIONS, fit_torque_params,
)
from openpilot.selfdrive.locationd.torqued import (
  TorqueEstimator, STEER_BUCKET_BOUNDS, MIN_VEL, LAT_ACC_THRESHOLD,
  STEER_MIN_THRESHOLD, MIN_ENGAGE_BUFFER, DT_MDL, HISTORY,
)
from openpilot.common.constants import ACCELERATION_DUE_TO_GRAVITY
from openpilot.selfdrive.locationd.helpers import Pose, PoseCalibrator
from openpilot.tools.lib.logreader import LogReader, ReadMode


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("route")
  parser.add_argument("--data-dir", required=True)
  parser.add_argument("--mode", choices=("auto", "qlog", "rlog"), default="auto")
  parser.add_argument("--decimated", action="store_true")
  args = parser.parse_args()

  mode_map = {"auto": ReadMode.AUTO, "qlog": ReadMode.QLOG, "rlog": ReadMode.RLOG}
  paths = resolve_local_paths(args.route, args.data_dir, args.mode)
  print(f"Streaming {len(paths)} segment(s) one at a time from {args.data_dir}...")

  car_params = None
  torque_estimator = None
  init_params = {}

  first_snapshot = None
  last_snapshot = None
  snapshot_count = 0

  hist_len = int(HISTORY / DT_MDL)
  calibrator = PoseCalibrator()
  lag = 0.0

  post_proc_points_standard = []
  post_proc_points_relaxed = []
  post_proc_points_all = []

  stats = {
    "total_pose_frames": 0, "rejected_buffer_len": 0, "rejected_speed_low": 0,
    "rejected_lat_inactive": 0, "rejected_driver_override": 0,
    "rejected_steer_center": 0, "rejected_lat_accel_high": 0, "accepted_points": 0,
    "max_steer_seen": -float("inf"), "min_steer_seen": float("inf"),
    "max_lat_accel_seen": -float("inf"), "min_lat_accel_seen": float("inf"),
  }

  for seg_i, path in enumerate(paths):
    raw_points = defaultdict(lambda: deque(maxlen=hist_len))
    log_reader = LogReader([path], default_mode=mode_map[args.mode], sort_by_time=True)

    for msg in log_reader:
      try:
        which = msg.which()
      except Exception:
        continue

      if which == "initData":
        init_params = {entry.key: entry.value for entry in msg.initData.params.entries}
        continue

      if which == "carParams" and car_params is None:
        car_params = msg.carParams
        from opendbc.car.hyundai.values import CAR
        allow_empty_buckets = car_params.carFingerprint == CAR.HYUNDAI_IONIQ_6
        torque_estimator = TorqueEstimator(car_params, decimated=args.decimated, track_all_points=True,
                                            allow_empty_buckets=allow_empty_buckets)
        continue

      if car_params is None:
        continue

      t = msg.logMonoTime * 1e-9

      if torque_estimator is not None and which in (
        "carControl", "carOutput", "carState", "liveCalibration", "livePose", "liveDelay"
      ):
        torque_estimator.handle_log(t, which, getattr(msg, which))

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
        stats["total_pose_frames"] += 1
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

          stats["max_steer_seen"] = max(stats["max_steer_seen"], steer)
          stats["min_steer_seen"] = min(stats["min_steer_seen"], steer)
          stats["max_lat_accel_seen"] = max(stats["max_lat_accel_seen"], lateral_acc)
          stats["min_lat_accel_seen"] = min(stats["min_lat_accel_seen"], lateral_acc)

          if not all(lat_active):
            stats["rejected_lat_inactive"] += 1
          elif any(steer_override):
            stats["rejected_driver_override"] += 1
          elif vego <= MIN_VEL:
            stats["rejected_speed_low"] += 1
          elif abs(steer) <= STEER_MIN_THRESHOLD:
            stats["rejected_steer_center"] += 1
          elif abs(lateral_acc) > LAT_ACC_THRESHOLD:
            stats["rejected_lat_accel_high"] += 1
            if abs(lateral_acc) <= 1.5:
              post_proc_points_relaxed.append([steer, 1.0, lateral_acc])
            if abs(lateral_acc) <= 2.5:
              post_proc_points_all.append([steer, 1.0, lateral_acc])
          else:
            stats["accepted_points"] += 1
            pt = [steer, 1.0, lateral_acc]
            post_proc_points_standard.append(pt)
            post_proc_points_relaxed.append(pt)
            post_proc_points_all.append(pt)
        else:
          stats["rejected_buffer_len"] += 1

      elif which == "liveTorqueParameters":
        snap = {
          "totalBucketPoints": msg.liveTorqueParameters.totalBucketPoints,
          "calPerc": msg.liveTorqueParameters.calPerc,
          "liveValid": msg.liveTorqueParameters.liveValid,
          "useParams": msg.liveTorqueParameters.useParams,
          "latAccelFactorFiltered": msg.liveTorqueParameters.latAccelFactorFiltered,
          "frictionCoefficientFiltered": msg.liveTorqueParameters.frictionCoefficientFiltered,
          "latAccelOffsetFiltered": msg.liveTorqueParameters.latAccelOffsetFiltered,
        }
        if hasattr(msg.liveTorqueParameters, "latAccelFactorRaw"):
          snap["latAccelFactorRaw"] = msg.liveTorqueParameters.latAccelFactorRaw
          snap["frictionCoefficientRaw"] = msg.liveTorqueParameters.frictionCoefficientRaw
          snap["latAccelOffsetRaw"] = msg.liveTorqueParameters.latAccelOffsetRaw
        if first_snapshot is None:
          first_snapshot = snap
        last_snapshot = snap
        snapshot_count += 1

    # drop this segment's decoded log + per-segment buffers before moving on
    del log_reader, raw_points
    gc.collect()
    print(f"  processed segment {seg_i + 1}/{len(paths)}: {path}", file=sys.stderr)

  if car_params is None:
    print("Error: No carParams packet found in any segment.", file=sys.stderr)
    sys.exit(1)

  print("\n" + "=" * 78)
  print(" TORQUE ESTIMATOR BUCKET & POST-PROCESSING REPORT (STREAMED, FULL ROUTE)")
  print("=" * 78)
  print(f"Car Fingerprint:      {car_params.carFingerprint}")
  print(f"Brand:                {car_params.brand}")
  print(f"Lateral Tuning Type:  {car_params.lateralTuning.which()}")
  stock_laf, stock_lao, stock_fric = 0.0, 0.0, 0.0
  if car_params.lateralTuning.which() == "torque":
    tq = car_params.lateralTuning.torque
    stock_laf, stock_lao, stock_fric = tq.latAccelFactor, tq.latAccelOffset, tq.friction
    print(f"Stock Torque Tune:    latAccelFactor={stock_laf:.4f}, friction={stock_fric:.4f}, latAccelOffset={stock_lao:.4f}")

  if init_params:
    def _get_b(k):
      return init_params.get(k, b"0") == b"1"
    print(f"Toggles at Init:      AdvancedLateralTune={_get_b('AdvancedLateralTune')}, "
          f"ForceAutoTune={_get_b('ForceAutoTune')}, "
          f"ForceAutoTuneOff={_get_b('ForceAutoTuneOff')}")

  print("\n" + "-" * 78)
  print(" 1. ON-DEVICE LOGGED liveTorqueParameters")
  print("-" * 78)
  if first_snapshot is not None:
    print(f"Messages Recorded:    {snapshot_count}")
    print(f"Route Start State:    totalBucketPoints={first_snapshot['totalBucketPoints']}, "
          f"calPerc={first_snapshot['calPerc']}%, liveValid={first_snapshot['liveValid']}, "
          f"useParams={first_snapshot['useParams']}")
    print(f"Route End State:      totalBucketPoints={last_snapshot['totalBucketPoints']}, "
          f"calPerc={last_snapshot['calPerc']}%, liveValid={last_snapshot['liveValid']}, "
          f"useParams={last_snapshot['useParams']}")
    print(f"Filtered Values:      latAccelFactor={last_snapshot['latAccelFactorFiltered']:.4f}, "
          f"friction={last_snapshot['frictionCoefficientFiltered']:.4f}, "
          f"latAccelOffset={last_snapshot['latAccelOffsetFiltered']:.4f}")
    if "latAccelFactorRaw" in last_snapshot:
      print(f"Raw Fitted Values:    latAccelFactorRaw={last_snapshot['latAccelFactorRaw']:.4f}, "
            f"frictionRaw={last_snapshot['frictionCoefficientRaw']:.4f}, "
            f"latAccelOffsetRaw={last_snapshot['latAccelOffsetRaw']:.4f}")
  else:
    print("No liveTorqueParameters messages logged in this route.")

  print("\n" + "-" * 78)
  mode_label = "DECIMATED MODE (/10)" if args.decimated else "STANDARD MODE"
  print(f" 2. 8-BUCKET REPLAY BREAKDOWN ({mode_label})")
  print("-" * 78)

  buckets = torque_estimator.filtered_points.buckets
  min_bucket_pts = torque_estimator.filtered_points.buckets_min_points
  total_pts = len(torque_estimator.filtered_points)
  min_total = torque_estimator.filtered_points.min_points_total

  print(f"{'Idx':<4} {'Description':<32} {'Points':<8} {'Required':<10} {'Fill %':<10} {'Status'}")
  print("-" * 78)

  lowest_pct = 100.0
  limiting_bucket_idx = 0

  for idx, ((low, high), desc) in enumerate(zip(STEER_BUCKET_BOUNDS, BUCKET_DESCRIPTIONS)):
    bound = (low, high)
    count = len(buckets[bound])
    req = min_bucket_pts[bound]
    pct = min(count / req * 100.0, 100.0) if req > 0 else 100.0
    excused = torque_estimator.filtered_points._is_excusable(bound)
    if not excused and pct < lowest_pct:
      lowest_pct = pct
      limiting_bucket_idx = idx

    if excused:
      status = "EXCUSED (allow_empty_buckets)" if count == 0 else "EXCUSED, partial (allow_empty_buckets)"
    elif count == 0:
      status = "EMPTY (0 pts) <--- BLOCKING"
    elif count < req:
      status = f"IN PROGRESS ({count}/{int(req)})"
    else:
      status = "SATISFIED"

    print(f"{idx:<4} {desc:<32} {count:<8} {int(req):<10} {pct:>5.1f}%     {status}")

  print("-" * 78)
  total_pct = min(total_pts / min_total * 100.0, 100.0)
  calc_cal_perc = torque_estimator.filtered_points.get_valid_percent()

  print(f"Total Points:         {total_pts} / {min_total} required ({total_pct:.1f}%)")
  print(f"Lowest Bucket:        Bucket {limiting_bucket_idx} ({lowest_pct:.1f}% filled)")
  print(f"Calculated calPerc:   ({total_pct:.1f}% total + {lowest_pct:.1f}% bucket) / 2 = {calc_cal_perc}%")
  print(f"Calculable (All > 0): {torque_estimator.filtered_points.is_calculable()}")
  print(f"Valid (All Met):      {torque_estimator.filtered_points.is_valid()} -> liveValid={torque_estimator.filtered_points.is_valid()}")

  print("\n" + "-" * 78)
  print(" 3. POINT REJECTION ROOT-CAUSE ANALYSIS")
  print("-" * 78)
  print(f"Total Pose Evaluation Cycles:          {stats['total_pose_frames']}")
  print(f"Points Accepted into Buckets:          {stats['accepted_points']}")
  print("\nPoints Rejected by Filter Condition:")
  print(f"  - Speed <= 15 m/s (33.5 mph):        {stats['rejected_speed_low']:<8} (City turns / slow driving discarded)")
  print(f"  - Lat Accel > 1.0 m/s^2:             {stats['rejected_lat_accel_high']:<8} (Curvature too sharp / high-g)")
  print(f"  - Steering Near Center (|torque|<=0.02): {stats['rejected_steer_center']:<8} (Dead straight highway driving)")
  print(f"  - Driver Override (wheel touched < 2s):{stats['rejected_driver_override']:<8} (Driver hands detected)")
  print(f"  - Lateral Inactive (< 2s buffer):     {stats['rejected_lat_inactive']:<8} (Disengaged or paused)")

  print(f"\nSteering Torque Range Observed:        [{stats['min_steer_seen']:+.4f} to {stats['max_steer_seen']:+.4f}]")
  print(f"Lateral Accel Range Observed:          [{stats['min_lat_accel_seen']:+.4f} to {stats['max_lat_accel_seen']:+.4f}] m/s^2")

  print("\n" + "=" * 78)
  print(" 4. POST-PROCESSING PARAMETER ESTIMATION (OFFLINE SVD FITS)")
  print("=" * 78)
  print("Unconstrained SVD Total Least Squares fits across your actual drive telemetry:\n")

  fit_std = fit_torque_params(post_proc_points_standard)
  fit_rel = fit_torque_params(post_proc_points_relaxed)
  fit_all = fit_torque_params(post_proc_points_all)

  left_pts = [p for p in post_proc_points_standard if p[2] > 0.05]
  right_pts = [p for p in post_proc_points_standard if p[2] < -0.05]
  fit_left = fit_torque_params(left_pts)
  fit_right = fit_torque_params(right_pts)

  def _fmt_fit(title, fit_res, n_pts):
    if fit_res is None:
      print(f"{title:<40} n={n_pts:<5}  [Insufficient points to fit]")
      return
    laf, lao, fr = fit_res
    f_amp = fr * laf
    diff_pct = ((laf / stock_laf) - 1.0) * 100.0 if stock_laf > 0 else 0.0
    print(f"{title:<38} n={n_pts:<5} -> latAccelFactor={laf:6.4f} ({diff_pct:+.1f}%), offset={lao:+7.4f}, friction={fr:6.4f}, fricAmp={f_amp:6.4f}")

  print(f"Stock Baseline Config:                       -> latAccelFactor={stock_laf:6.4f}, offset={stock_lao:+7.4f}, friction={stock_fric:6.4f}")
  print("-" * 78)
  _fmt_fit("Standard Filtered (latAccel <= 1.0 m/s^2):", fit_std, len(post_proc_points_standard))
  _fmt_fit("Relaxed Filtered  (latAccel <= 1.5 m/s^2):", fit_rel, len(post_proc_points_relaxed))
  _fmt_fit("All Curve Data    (latAccel <= 2.5 m/s^2):", fit_all, len(post_proc_points_all))

  print("-" * 78)
  print("Directional Asymmetry Breakdown (Standard window):")
  _fmt_fit("  Left Curves  (latAccel > +0.05 m/s^2):", fit_left, len(left_pts))
  _fmt_fit("  Right Curves (latAccel < -0.05 m/s^2):", fit_right, len(right_pts))

  print("\n" + "=" * 78)
  print(" TUNING RECOMMENDATION")
  print("=" * 78)
  if fit_std is not None:
    laf, lao, fr = fit_std
    print(f"-> Fitted latAccelFactor: {laf:.4f} (vs stock {stock_laf:.4f})")
    print(f"-> Fitted latAccelOffset: {lao:+.4f} (center steering bias)")
    print(f"-> Fitted friction:       {fr:.4f} (vs stock {stock_fric:.4f})")
    if abs(lao) > 0.05:
      print(f"-> Center Bias Alert: The steering rack has a {lao:+.4f} m/s^2 bias ({'left' if lao > 0 else 'right'}).")
      print("   Setting latAccelOffset to this value eliminates asymmetric drift/pull on straightaways.")
  else:
    print("-> Insufficient data points across the drive to compute fit.")
  print("=" * 78 + "\n")


if __name__ == "__main__":
  main()
