#!/usr/bin/env python3
"""Shared per-frame replay loop, extracted from `.scratch/ab_tests/ab_test_chatter_bands.py`'s
`run()`. This is the boilerplate every chatter/tuning A/B script duplicates: bootstrap the
controller from `carParams`, track the latest state messages, call `LatControlTorque.update()`
per `controlsState`, and yield the per-frame result for the caller to gate/window/accumulate.

Deliberately narrow in scope: this does NOT dedupe all `.scratch/ab_tests/*.py` scripts (several
are historical records of already-shipped decisions where byte-for-byte reproducibility matters
more than DRY) -- it only extracts enough to avoid adding another copy-pasted replay loop for the
amplitude-metric work. See `.scratch/ioniq_amplitude_chatter_metric_plan.md`.
"""
import os
import json
from types import SimpleNamespace

from openpilot.tools.lib.logreader import LogReader, ReadMode
from opendbc.car.car_helpers import interfaces
from opendbc.car.vehicle_model import VehicleModel
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.controlsd import get_control_lateral_smooth_seconds, get_torque_control_params
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.selfdrive.controls.lib import latcontrol_vehicle_tunes as tunes
from cereal import custom

from openpilot.tools.tuning.chatter_metrics import band_for

# Sentinel yielded on a non-contiguous segment boundary (e.g. a route subset skipping segments).
# Callers that keep their own sliding-window state across calls must clear it on this sentinel,
# so a window never silently splices frames from two disconnected moments of a drive together.
SEGMENT_GAP = object()


def iter_controls_frames(paths, extra_patches=None):
  """Yields `(CS, pid_log, out, band)` per `controlsState` message across `paths`, plus
  `SEGMENT_GAP` sentinels at non-contiguous segment boundaries.

  `extra_patches`: optional dict of `latcontrol_vehicle_tunes` attribute names to temporarily
  override for the duration of the generator (e.g. `{"IONIQ_6_FRICTION_CENTER_FADE_SPEED": 15.0}`)
  -- restored in a `finally` block regardless of how the generator exits. Does NOT handle the
  friction-curve monkeypatch or center_fade_max/highway_taper_max overrides that
  `ab_test_chatter_bands.py` needs -- those are specific to that script's A/B harness and stay in
  its own `run()`, applied before/around this generator.
  """
  extra_patches = extra_patches or {}
  orig_extra = {name: getattr(tunes, name) for name in extra_patches}
  for name, value in extra_patches.items():
    setattr(tunes, name, value)
  try:
    car_params = None
    controller = None
    VM = None
    latest = {"carState": None, "liveParameters": None, "liveTorqueParameters": None,
              "latActive": False, "lat_delay_base": 0.1, "starpilot_toggles": SimpleNamespace(),
              "carOutput_torque": 0.0}
    steer_limited_by_safety = False

    prev_seg_num = None
    for path in paths:
      seg_num = int(os.path.basename(os.path.dirname(path)).rsplit("--", 1)[-1])
      if prev_seg_num is not None and seg_num != prev_seg_num + 1:
        yield SEGMENT_GAP
      prev_seg_num = seg_num
      lr = LogReader([path], default_mode=ReadMode.RLOG, sort_by_time=True)
      for msg in lr:
        which = msg.which()
        if which == "carParams" and car_params is None:
          car_params = msg.carParams
          CI = interfaces[car_params.carFingerprint](car_params, custom.StarPilotCarParams.new_message())
          controller = LatControlTorque(car_params, CI, DT_CTRL)
          VM = VehicleModel(car_params)
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
          t = msg.starpilotPlan.starpilotToggles
          latest["starpilot_toggles"] = SimpleNamespace(**json.loads(t)) if t else SimpleNamespace()
        elif which == "controlsState":
          if latest["carState"] is None or latest["liveParameters"] is None:
            continue
          CS = latest["carState"]
          lp = latest["liveParameters"]
          VM.update_params(max(lp.stiffnessFactor, 0.1), max(lp.steerRatio, 0.1))
          tp = latest["liveTorqueParameters"]
          if tp is not None and tp.useParams:
            laf, lao, fric = get_torque_control_params(car_params, tp, latest["starpilot_toggles"], True)
            controller.update_live_torque_params(laf, lao, fric)
          desired_curvature = msg.controlsState.desiredCurvature
          lat_delay = latest["lat_delay_base"] + get_control_lateral_smooth_seconds(
            car_params.brand, CS.vEgo, car_params.lateralSmoothSeconds)
          out, _, pid_log = controller.update(
            latest["latActive"], CS, VM, lp, steer_limited_by_safety, desired_curvature,
            False, lat_delay, None, None, latest["starpilot_toggles"])
          steer_limited_by_safety = abs(out - latest["carOutput_torque"]) > 1e-2

          yield CS, pid_log, out, band_for(CS.vEgo)
  finally:
    for name, value in orig_extra.items():
      setattr(tunes, name, value)
