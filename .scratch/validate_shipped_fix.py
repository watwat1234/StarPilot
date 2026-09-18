#!/usr/bin/env python3
"""Validate the SHIPPED fix (get_bolt_2022_2023_low_speed_ff_correction in
latcontrol_vehicle_tunes.py + its call site in latcontrol_torque.py) via a corrected
open-loop replay -- corrected relative to prototype_bolt_ff_additive_fix.py, which was
found (finding #12 in bolt-tuning-916.md) to never apply the StarPilot "SteerKP"
advanced-tuning override (controlsd.py:448: `self.LaC.pid._k_p =
self.starpilot_toggles.steerKp`), silently leaving the replay's P-gain 15x too high at
low speed relative to what the real device actually ran (confirmed from this route's own
log: starpilotPlan.starpilotToggles carries steerKp=[[0],[0.6]], flat).

This script applies that override every cycle, matching controlsd.py, before calling
controller.update() -- the fix finding #12 identified but didn't implement.

Still open-loop (same scope limit as every replay in this investigation): CS is replayed
verbatim from the log, so this shows what the ACTUAL shipped code would have commanded at
each instant along the real (pre-fix) trajectory, not whether the ringdown itself would
be damped. "stock" pass disables the new correction (monkeypatches it to return 0.0,
simulating pre-fix code); "candidate" pass uses the real shipped function unpatched.

Usage:
  uv run python3 .scratch/validate_shipped_fix.py <rlog_path> <t_start> <t_end>
(needs the x86_64 native-ext rebuild per project_native_ext_testing memory; run via the
activated venv + PYTHONPATH, not uv run, once building is done)
"""
import json
import sys
from types import SimpleNamespace

from cereal import custom
from opendbc.car.car_helpers import interfaces
from opendbc.car.vehicle_model import VehicleModel
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.lib import latcontrol_torque as lct_module
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.tools.lib.logreader import LogReader, ReadMode

LAT_SMOOTH_SECONDS = 0.1

CEIL_MIN_LA = -3.403
CEIL_MAX_LA = 2.355

path = sys.argv[1]
t_start = float(sys.argv[2])
t_end = float(sys.argv[3])

original_correction = lct_module.get_bolt_2022_2023_low_speed_ff_correction


def zero_correction(desired_lateral_accel, v_ego):
  return 0.0


def build_controller(car_fingerprint, CP):
  CarInterface = interfaces[car_fingerprint]
  CI = CarInterface(CP, custom.StarPilotCarParams.new_message())
  controller = LatControlTorque(CP, CI, DT_CTRL)
  VM = VehicleModel(CP)
  return controller, VM


def process_starpilot_toggles(toggles_text):
  if toggles_text:
    return json.loads(toggles_text)
  return {}


def run(path, t_start, t_end, patched):
  lct_module.get_bolt_2022_2023_low_speed_ff_correction = original_correction if patched else zero_correction

  try:
    lr = LogReader(path, default_mode=ReadMode.RLOG, sort_by_time=True)

    car_params = None
    controller = None
    VM = None
    latest = {"carState": None, "liveParameters": None, "liveTorqueParameters": None,
              "latActive": False, "lat_delay_base": 0.1, "steer_kp": [[0], [0.6]],
              "carOutput_torque": 0.0}
    steer_limited_by_safety = False
    t0 = None
    rows = []

    for msg in lr:
      try:
        which = msg.which()
      except Exception:
        continue

      if which == "controlsState" and t0 is None:
        t0 = msg.logMonoTime / 1e9

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
        toggles = process_starpilot_toggles(msg.starpilotPlan.starpilotToggles)
        if "steerKp" in toggles:
          latest["steer_kp"] = toggles["steerKp"]
      elif which == "controlsState":
        if t0 is None:
          t0 = msg.logMonoTime / 1e9
        t_rel = msg.logMonoTime / 1e9 - t0
        if latest["carState"] is None or latest["liveParameters"] is None:
          continue

        CS = latest["carState"]
        lp = latest["liveParameters"]
        VM.update_params(max(lp.stiffnessFactor, 0.1), max(lp.steerRatio, 0.1))

        # matches controlsd.py:448 -- applied every cycle, unconditionally, for any
        # torque-tuned car. This is the fix for finding #12's replay bug.
        controller.pid._k_p = latest["steer_kp"]

        desired_curvature = msg.controlsState.desiredCurvature
        lat_delay = latest["lat_delay_base"] + LAT_SMOOTH_SECONDS

        output_torque_signed, _, pid_log = controller.update(
          latest["latActive"], CS, VM, lp, steer_limited_by_safety, desired_curvature,
          False, lat_delay, None, None, SimpleNamespace(),
        )
        steer_limited_by_safety = abs(output_torque_signed - latest["carOutput_torque"]) > 1e-2

        if t_start - 0.5 <= t_rel <= t_end + 0.5:
          control_sum = pid_log.p + pid_log.i + pid_log.f + pid_log.d
          rows.append(dict(t=t_rel, v_ego=CS.vEgo, desired_la=pid_log.desiredLateralAccel,
                            actual_la=pid_log.actualLateralAccel, p=pid_log.p, i=pid_log.i,
                            f=pid_log.f, d=pid_log.d, control_sum=control_sum,
                            torque=output_torque_signed))
    return rows
  finally:
    lct_module.get_bolt_2022_2023_low_speed_ff_correction = original_correction


print("Running stock pass (fix disabled)...", file=sys.stderr)
stock_rows = run(path, t_start, t_end, patched=False)
print("Running candidate pass (real shipped fix)...", file=sys.stderr)
cand_rows = run(path, t_start, t_end, patched=True)

stock_by_t = {round(r["t"], 3): r for r in stock_rows}
cand_by_t = {round(r["t"], 3): r for r in cand_rows}
common_t = sorted(set(stock_by_t) & set(cand_by_t))

print(f"\n{'t':>6s} {'v_ego':>6s} {'des_la':>7s} {'p':>7s} {'i':>7s} {'f_stk':>7s} {'f_cnd':>7s} "
      f"{'sum_stk':>8s} {'sum_cnd':>8s}")

n_in_window = 0
n_clip_stock = n_clip_cand = 0
max_excess_stock = max_excess_cand = 0.0
n_sat_stock = n_sat_cand = 0
for t in common_t:
  s, c = stock_by_t[t], cand_by_t[t]
  marker = ""
  if t_start <= t <= t_end:
    n_in_window += 1
    exc_s = max(0.0, s["control_sum"] - CEIL_MAX_LA, CEIL_MIN_LA - s["control_sum"])
    exc_c = max(0.0, c["control_sum"] - CEIL_MAX_LA, CEIL_MIN_LA - c["control_sum"])
    if exc_s > 0.001:
      n_clip_stock += 1
      max_excess_stock = max(max_excess_stock, exc_s)
    if exc_c > 0.001:
      n_clip_cand += 1
      max_excess_cand = max(max_excess_cand, exc_c)
    if abs(s["torque"]) >= 0.99:
      n_sat_stock += 1
    if abs(c["torque"]) >= 0.99:
      n_sat_cand += 1
  else:
    marker = " (pad)"
  print(f"{t:6.2f} {s['v_ego']:6.2f} {s['desired_la']:7.3f} {s['p']:7.3f} {s['i']:7.3f} "
        f"{s['f']:7.3f} {c['f']:7.3f} {s['control_sum']:8.3f} {c['control_sum']:8.3f}{marker}")

print(f"\nCeiling: [{CEIL_MIN_LA}, {CEIL_MAX_LA}]")
print(f"Control-signal (p+i+f+d) clipping against ceiling over [{t_start},{t_end}]s:")
print(f"  stock (fix off): {n_clip_stock}/{n_in_window} ({100*n_clip_stock/max(n_in_window,1):.1f}%)  max excess={max_excess_stock:.3f}")
print(f"  candidate (fix on): {n_clip_cand}/{n_in_window} ({100*n_clip_cand/max(n_in_window,1):.1f}%)  max excess={max_excess_cand:.3f}")
print(f"Final-torque saturation (|torque|>=0.99):")
print(f"  stock (fix off):   {n_sat_stock}/{n_in_window} ({100*n_sat_stock/max(n_in_window,1):.1f}%)")
print(f"  candidate (fix on): {n_sat_cand}/{n_in_window} ({100*n_sat_cand/max(n_in_window,1):.1f}%)")
