#!/usr/bin/env python3
"""Replay the real LatControlTorque controller against a logged route, streaming one
segment at a time (same memory-bounded pattern as inspect_torque_buckets_streaming.py).

Purpose: get objective chatter/hug metrics without guessing which of the ~15
IONIQ_6_* gain-scheduling terms is responsible. This drives the actual
LatControlTorque.update() with reconstructed per-cycle inputs and checks its output
against the on-device logged values (fidelity check) before trusting any derived
metric from it.

Per-cycle inputs are reconstructed as follows (see controlsd.py for the ground truth
this mirrors):
  - active            <- carControl.latActive (held from last carControl message)
  - CS                <- the carState reader itself (controlsd passes sm['carState'] directly)
  - VM                <- VehicleModel(CP), with VM.update_params(stiffnessFactor, steerRatio)
                         refreshed from liveParameters every cycle, exactly as controlsd's
                         state_control() does before calling LaC.update()
  - params            <- the liveParameters reader itself (only .roll/.angleOffsetDeg are
                         read by LatControlTorque.update())
  - steer_limited_by_safety <- reconstructed one cycle late, from comparing the *previous*
                         cycle's replayed output torque against that cycle's logged
                         carOutput.actuatorsOutput.torque (mirrors controlsd's publish()
                         computing it for the next cycle's update())
  - desired_curvature <- controlsState.desiredCurvature (this is controlsd's own
                         self.desired_curvature, logged verbatim -- no need to
                         re-derive the upstream planner/clip_curvature pipeline)
  - curvature_limited <- NOT reconstructed (see below), always False
  - lat_delay         <- liveDelay.lateralDelay + get_control_lateral_smooth_seconds(...)
                         (imported directly from controlsd; for hyundai this is always
                         + 0.1s, NOT a bare passthrough of liveDelay as a naive reading
                         of the source would suggest)
  - calibrated_pose, model_data -- unused by LatControlTorque.update() itself; passed as
                         None (confirmed by reading the function body: neither name is
                         referenced inside it, only in the abstract base signature)
  - starpilot_toggles <- SimpleNamespace(**json.loads(starpilotPlan.starpilotToggles)),
                         held from the last such message seen, matching controlsd's own
                         process_starpilot_toggles(). All fields LatControlTorque reads
                         from this go through getattr(..., <default>), so an empty
                         SimpleNamespace() before the first starpilotPlan message is safe.

Known, unresolved fidelity gap -- pid_log.error/p/i diverge from the logged values by a
large, inconsistent factor in some windows (confirmed not caused by driver override,
an FLM/code-version mismatch, or the is_ioniq_6_2025 branch -- root cause not found).
CRITICALLY, desiredLateralAccel/actualLateralAccel/output (the actual commanded torque)
match the logged values almost exactly everywhere sampled -- those are the fields the
chatter/hug metrics below are built from. Per project decision, this replay is used for
those two metrics only; error/p/i are reported for visibility but are NOT a fidelity
gate for Phase 2. Revisit if a future symptom investigation needs PID-internal state.

Separately, on the full 169-segment route the max|diff| on the gating fields
(desiredLateralAccel/actualLateralAccel/output) spikes far above the near-zero baseline
seen on any interior segment -- traced to 8 cycles total (the report used to double-count
these as 16 by summing two separate "active" mismatch counters into one key; fixed),
all `active` boolean disagreements between the replay and the logged
controlsState.lateralControlState.torqueState.active. CORRECTION (caught by independent
review, not confirmed by initial testing): these are NOT confined to one boundary --
enumerating every mismatch across a true continuous full-route run puts them in five
different segments spread across the whole drive (indices 1, 4, 62, 167, 168), each with
1-2 mismatched cycles. This is consistent with the held `carControl.latActive`
reconstruction lagging the real per-cycle value by a cycle or two at MULTIPLE
engage/disengage transitions throughout the drive, not just a single final-disengage
edge as originally (incorrectly) claimed. 8 mismatched cycles out of ~954k still cannot
meaningfully move an aggregate metric built from tens of thousands of samples, so the
aggregate chatter/hug numbers are still trusted -- but do not trust a claim that this
tool's fidelity gap is isolated to one place in the route; it recurs. Not fixed because
it doesn't affect the metrics either way; revisit only if a future investigation needs
cycle-accurate fidelity around engage/disengage transitions specifically. NOTE:
segment-slice testing (e.g. running "route/1:2" in isolation) will show DIFFERENT
mismatches than the continuous full-route run does, because an isolated slice starts
from cold default state instead of state carried over from prior segments -- always
enumerate mismatches from one continuous run when characterizing this, not from
isolated segment slices.

Known simplification -- curvature_limited is always passed as False:
  Reconstructing it exactly requires replaying clip_curvature()'s full state (previous
  desired_curvature, jerk_factor from the lane-change-arrest state machine, etc.), which
  pulls in most of controlsd's turn-signal/lane-change logic for a flag that
  LatControlTorque only forwards to LatControl._check_saturation() -- it does not affect
  output_torque, ff, or any starpilot_lateral_state field. It only affects the replayed
  pid_log.saturated flag's fidelity (a stateful, low-speed-gated timer), which is noted
  as approximate in the fidelity report below.

Also NOT reconstructed: the sm.all_checks(['liveTorqueParameters']) freshness check
controlsd applies before trusting a liveTorqueParameters snapshot -- this replay treats
any snapshot as usable once seen, same as inspect_torque_buckets_streaming.py already
does for its own liveTorqueParameters reporting.
"""
import argparse
import gc
import json
import sys
from collections import deque
from types import SimpleNamespace

import numpy as np

from cereal import log
from opendbc.car.car_helpers import interfaces
from opendbc.car.vehicle_model import VehicleModel
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.controlsd import get_control_lateral_smooth_seconds, get_torque_control_params
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.tools.tuning.inspect_torque_buckets import resolve_local_paths
from openpilot.tools.lib.logreader import LogReader, ReadMode

# custom.StarPilotCarParams -- CarInterface's second constructor arg. Only affects
# CarState/CarController construction (DBC parsing, quirk flags), not the torque/lateral
# accel conversion callbacks LatControlTorque uses, so an empty default (as the project's
# own test_latcontrol.py::_build_torque_controller already does) is safe here.
from cereal import custom

PID_LOG_FIELDS = ["active", "error", "errorRate", "p", "i", "d", "f", "output", "saturated",
                   "actualLateralAccel", "desiredLateralAccel", "desiredLateralJerk"]
STARPILOT_STATE_FIELDS = ["active", "frictionThreshold", "frictionScale", "feedforward",
                          "frictionJerk", "frictionJerkDeadzone", "lowSpeedFactor", "unwindDetected"]

# Chatter metric: highway speed, near-zero commanded path -> count sign changes in the
# replayed commanded torque within a rolling window.
CHATTER_MIN_SPEED = 20.0        # m/s, "highway" per FLM's own speed banding
CHATTER_SETPOINT_ABS_MAX = 0.05  # m/s^2, "near zero" desired lateral accel
CHATTER_WINDOW_FRAMES = 100      # 1s at 100Hz


def build_controller(car_fingerprint, CP):
  CarInterface = interfaces[car_fingerprint]
  CI = CarInterface(CP, custom.StarPilotCarParams.new_message())
  controller = LatControlTorque(CP, CI, DT_CTRL)
  VM = VehicleModel(CP)
  return controller, VM


def process_starpilot_toggles(toggles_text):
  if toggles_text:
    return SimpleNamespace(**json.loads(toggles_text))
  return SimpleNamespace()


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("route")
  parser.add_argument("--data-dir", required=True)
  parser.add_argument("--mode", choices=("auto", "qlog", "rlog"), default="rlog",
                       help="rlog is required -- controlsState/starpilotLateralState/carOutput are not in qlog")
  args = parser.parse_args()

  mode_map = {"auto": ReadMode.AUTO, "qlog": ReadMode.QLOG, "rlog": ReadMode.RLOG}
  paths = resolve_local_paths(args.route, args.data_dir, args.mode)
  print(f"Streaming {len(paths)} segment(s) one at a time from {args.data_dir}...", file=sys.stderr)

  car_params = None
  controller = None
  VM = None

  # held "latest seen" state, carried across messages within a segment and across segments
  latest = {
    "carState": None, "liveParameters": None, "liveTorqueParameters": None,
    "latActive": False, "lat_delay_base": 0.1, "starpilot_toggles": SimpleNamespace(),
    "carOutput_torque": 0.0,
  }
  steer_limited_by_safety = False

  fidelity_max_abs_diff = {f: 0.0 for f in PID_LOG_FIELDS if f != "active" and f != "saturated"}
  fidelity_max_abs_diff.update({f: 0.0 for f in STARPILOT_STATE_FIELDS if f not in ("active", "unwindDetected")})
  fidelity_mismatches = {"active": 0, "saturated": 0, "unwindDetected": 0, "starpilotStateActive": 0}
  cycles_compared = 0
  pending_pid_log = None  # torqueState read from this cycle's controlsState, awaiting the paired starpilotLateralState

  chatter_window = deque(maxlen=CHATTER_WINDOW_FRAMES)
  chatter_sign_changes_total = 0
  chatter_windows_seen = 0
  hug_error_by_direction = {"left": [], "right": []}  # signed (setpoint - measurement) at cycles with |setpoint| > 0.3, split by sign
  hug_error_by_direction_phase = {(d, p): [] for d in ("left", "right") for p in ("turn_in", "steady", "unwind")}

  for seg_i, path in enumerate(paths):
    log_reader = LogReader([path], default_mode=mode_map[args.mode], sort_by_time=True)

    for msg in log_reader:
      try:
        which = msg.which()
      except Exception:
        continue

      if which == "carParams" and car_params is None:
        car_params = msg.carParams
        controller, VM = build_controller(car_params.carFingerprint, car_params)
        continue

      if car_params is None:
        continue

      if which == "carState":
        latest["carState"] = msg.carState
      elif which == "liveParameters":
        latest["liveParameters"] = msg.liveParameters
      elif which == "liveDelay":
        latest["lat_delay_base"] = msg.liveDelay.lateralDelay
      elif which == "liveTorqueParameters":
        latest["liveTorqueParameters"] = msg.liveTorqueParameters
      elif which == "carControl":
        latest["latActive"] = msg.carControl.latActive
      elif which == "carOutput":
        latest["carOutput_torque"] = msg.carOutput.actuatorsOutput.torque
      elif which == "starpilotPlan":
        latest["starpilot_toggles"] = process_starpilot_toggles(msg.starpilotPlan.starpilotToggles)

      elif which == "starpilotLateralState":
        # paired with the controlsState this cycle produced; compare and clear
        if pending_pid_log is not None:
          logged_state = msg.starpilotLateralState
          replayed_state = controller.starpilot_lateral_state
          for f in STARPILOT_STATE_FIELDS:
            if f in ("active", "unwindDetected"):
              if getattr(logged_state, f) != getattr(replayed_state, f):
                # "active" here is a distinct counter from the torqueState.active one below --
                # they mismatch on the same cycles in practice, but sharing one key previously
                # double-counted every real mismatch.
                mismatch_key = "starpilotStateActive" if f == "active" else f
                fidelity_mismatches[mismatch_key] += 1
            else:
              diff = abs(getattr(logged_state, f) - getattr(replayed_state, f))
              fidelity_max_abs_diff[f] = max(fidelity_max_abs_diff[f], diff)
          pending_pid_log = None

      elif which == "controlsState":
        if latest["carState"] is None or latest["liveParameters"] is None:
          continue  # can't replay a cycle before we've seen at least one of each

        CS = latest["carState"]
        lp = latest["liveParameters"]

        # mirrors controlsd.state_control()'s VM refresh, done every cycle before update()
        VM.update_params(max(lp.stiffnessFactor, 0.1), max(lp.steerRatio, 0.1))

        # mirrors controlsd.state_control()'s live torque-params application
        tp = latest["liveTorqueParameters"]
        if tp is not None:
          force_auto_tune = bool(getattr(latest["starpilot_toggles"], "force_auto_tune", False))
          use_live_params = tp.useParams or force_auto_tune
          use_custom = (bool(getattr(latest["starpilot_toggles"], "use_custom_latAccelFactor", False)) or
                       bool(getattr(latest["starpilot_toggles"], "use_custom_friction", False)))
          if use_live_params or use_custom:
            laf, lao, fric = get_torque_control_params(car_params, tp, latest["starpilot_toggles"], use_live_params)
            controller.update_live_torque_params(laf, lao, fric)

        desired_curvature = msg.controlsState.desiredCurvature
        lat_smooth_seconds = get_control_lateral_smooth_seconds(car_params.brand, CS.vEgo, car_params.lateralSmoothSeconds)
        lat_delay = latest["lat_delay_base"] + lat_smooth_seconds

        output_torque_signed, _, pid_log = controller.update(
          latest["latActive"], CS, VM, lp, steer_limited_by_safety, desired_curvature,
          False,  # curvature_limited -- see module docstring
          lat_delay, None, None, latest["starpilot_toggles"],
        )

        # controlsd computes this AFTER publishing, from this cycle's actual carOutput --
        # reconstruct it for the *next* cycle the same lagged way.
        steer_limited_by_safety = abs(output_torque_signed - latest["carOutput_torque"]) > 1e-2

        logged_torque_state = msg.controlsState.lateralControlState.torqueState
        cycles_compared += 1
        for f in PID_LOG_FIELDS:
          if f == "active":
            if logged_torque_state.active != pid_log.active:
              fidelity_mismatches["active"] += 1
          elif f == "saturated":
            if logged_torque_state.saturated != pid_log.saturated:
              fidelity_mismatches["saturated"] += 1
          else:
            diff = abs(getattr(logged_torque_state, f) - getattr(pid_log, f))
            fidelity_max_abs_diff[f] = max(fidelity_max_abs_diff[f], diff)
        pending_pid_log = pid_log

        # --- chatter metric ---
        if pid_log.active and CS.vEgo >= CHATTER_MIN_SPEED and abs(pid_log.desiredLateralAccel) <= CHATTER_SETPOINT_ABS_MAX:
          chatter_window.append(output_torque_signed)
          if len(chatter_window) == CHATTER_WINDOW_FRAMES:
            signs = np.sign(chatter_window)
            signs = signs[signs != 0]
            if len(signs) > 1:
              chatter_sign_changes_total += int(np.sum(np.diff(signs) != 0))
              chatter_windows_seen += 1
        else:
          chatter_window.clear()

        # --- hug metric: signed tracking error split by curve direction AND turn phase ---
        # phase mirrors FLM's own turn-in/unwind split: |setpoint| growing (jerk same sign
        # as setpoint) is turn-in, shrinking (opposite sign) is unwind, near-zero jerk is steady.
        if pid_log.active and abs(pid_log.desiredLateralAccel) > 0.3:
          direction = "left" if pid_log.desiredLateralAccel > 0 else "right"
          jerk = pid_log.desiredLateralJerk
          if abs(jerk) < 0.05:
            phase = "steady"
          elif np.sign(jerk) == np.sign(pid_log.desiredLateralAccel):
            phase = "turn_in"
          else:
            phase = "unwind"
          err = pid_log.desiredLateralAccel - pid_log.actualLateralAccel
          hug_error_by_direction[direction].append(err)
          hug_error_by_direction_phase[(direction, phase)].append(err)

    del log_reader
    gc.collect()
    print(f"  processed segment {seg_i + 1}/{len(paths)}: {path}", file=sys.stderr)

  if car_params is None:
    print("Error: No carParams packet found in any segment.", file=sys.stderr)
    sys.exit(1)

  print("\n" + "=" * 78)
  print(" LatControlTorque REPLAY REPORT (STREAMED, FULL ROUTE)")
  print("=" * 78)
  print(f"Car Fingerprint: {car_params.carFingerprint}   Cycles replayed: {cycles_compared}")

  print("\n" + "-" * 78)
  print(" FIDELITY CHECK vs on-device logged values")
  print("-" * 78)
  print("Gating fields for the metrics below (must be ~0):")
  for f in ("desiredLateralAccel", "actualLateralAccel", "output"):
    print(f"  max|diff| {f:<22} {fidelity_max_abs_diff[f]:.6g}")
  print(f"  active mismatches:    {fidelity_mismatches['active']} / {cycles_compared}")
  print("\nNon-gating (KNOWN UNRESOLVED divergence -- do not use for tuning decisions,")
  print("see module docstring; not caused by driver override, code-version mismatch,")
  print("or the is_ioniq_6_2025 branch, root cause not found):")
  for f in ("error", "p", "i", "d", "f"):
    print(f"  max|diff| {f:<22} {fidelity_max_abs_diff[f]:.6g}")
  print(f"  saturated mismatches: {fidelity_mismatches['saturated']} / {cycles_compared}  "
        f"(also approximate -- curvature_limited is not reconstructed, see module docstring)")
  print("\nstarpilotLateralState:")
  for f in STARPILOT_STATE_FIELDS:
    if f in ("active", "unwindDetected"):
      mismatch_key = "starpilotStateActive" if f == "active" else f
      print(f"  {f} mismatches: {fidelity_mismatches.get(mismatch_key, 'n/a')} / {cycles_compared}")
    else:
      print(f"  max|diff| {f:<22} {fidelity_max_abs_diff[f]:.6g}")

  print("\n" + "-" * 78)
  print(f" CHATTER METRIC (highway, |setpoint|<={CHATTER_SETPOINT_ABS_MAX} m/s^2, {CHATTER_WINDOW_FRAMES}-frame windows)")
  print("-" * 78)
  if chatter_windows_seen:
    print(f"Windows evaluated: {chatter_windows_seen}")
    print(f"Total commanded-torque sign changes: {chatter_sign_changes_total}")
    print(f"Mean sign changes / window: {chatter_sign_changes_total / chatter_windows_seen:.2f}")
  else:
    print("No qualifying near-zero-setpoint highway windows found.")

  print("\n" + "-" * 78)
  print(" HUG METRIC: signed tracking error (setpoint - measurement), |setpoint| > 0.3 m/s^2")
  print("-" * 78)
  for direction, errors in hug_error_by_direction.items():
    if errors:
      arr = np.array(errors)
      print(f"  {direction:<6} curves: n={len(arr):<7} mean={arr.mean():+.4f}  std={arr.std():.4f}")
    else:
      print(f"  {direction:<6} curves: no qualifying samples")

  print("\n" + "-" * 78)
  print(" HUG METRIC BY TURN PHASE (turn_in: |setpoint| growing, unwind: shrinking, steady: |jerk|<0.05)")
  print("-" * 78)
  for direction in ("left", "right"):
    for phase in ("turn_in", "steady", "unwind"):
      errors = hug_error_by_direction_phase[(direction, phase)]
      if errors:
        arr = np.array(errors)
        print(f"  {direction:<6} {phase:<8} n={len(arr):<7} mean={arr.mean():+.4f}  std={arr.std():.4f}")
      else:
        print(f"  {direction:<6} {phase:<8} no qualifying samples")
  print("=" * 78 + "\n")


if __name__ == "__main__":
  main()
