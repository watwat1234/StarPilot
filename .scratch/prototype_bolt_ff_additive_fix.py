#!/usr/bin/env python3
"""Open-loop counterfactual for candidate fix (a) from bolt-tuning-916.md's NEXT STEP
section: a new, v_ego-gated ADDITIVE FF correction sized to finding #6's measured
low-speed torque-under-prediction bias, as opposed to finding #9's (ruled out)
multiplicative FF_GAIN_LEFT/TURN_IN_BOOST_LEFT bump.

Correction curve (torque-domain, ADDITIVE, cancels finding #6's measured bias by band):
  v_ego band  measured bias (pred-actual)   correction (= -bias)
  0-5 m/s     -0.067                        +0.067
  5-7 m/s     -0.156                        +0.156
  7-9 m/s     -0.198 (worst)                +0.198
  9-11 m/s    -0.121                        +0.121
  11+ m/s     +0.034 (~0, curve already ok) 0.0 (taper out, don't touch validated range)
Implemented as np.interp over speed breakpoints at band midpoints, tapering to 0 by
v_ego=12. Signed by turn direction (sign of desired_lateral_accel, matching this
controller's convention -- see finding #8's correction in bolt-tuning-916.md).

IMPLEMENTATION-FIDELITY CAVEAT (read before trusting the numbers): the real fix would
add this correction directly to `ff` inside LatControlTorque.update() (a genuine one-line
change). This prototype instead monkeypatches get_bolt_2022_2023_ff_scale -- which is
MULTIPLICATIVE on ff (`ff *= get_bolt_2022_2023_ff_scale(...)`) -- to also fold in the
additive correction as an approximately-equivalent scale bump:
  patched_scale = orig_scale + correction / setpoint
using `setpoint` (desired_lateral_accel) as a stand-in for the pre-scale `ff` value.
These are close but not identical at this speed range (roll-offset compensation is
already ~fully faded in above v_ego=2.5m/s per FF_ROLL_OFFSET_FADE_BP, and setpoint vs.
future_desired_lateral_accel differ only by the delay-buffer lookahead), so results here
are a reasonable first open-loop probe, not proof the exact real-code change lands
identically. Guarded against setpoint==0 (returns orig_scale unchanged, matching the
function's own dead-zone short-circuit).

Still OPEN-LOOP overall, same scope limit as prototype_bolt_ff_tune.py: CS is replayed
verbatim from the log (what the car did under the STOCK tune), so this cannot show
whether the ringdown itself would be damped, only whether the controller's internal
correction signal (p+i+f+d, finding #10's method) would have needed less clipping along
the real trajectory.

Usage:
  uv run python3 .scratch/prototype_bolt_ff_additive_fix.py <rlog_path> <t_start> <t_end>
(needs the x86_64 native-ext rebuild per project_native_ext_testing memory, same as
prototype_bolt_ff_tune.py -- run via the activated venv + PYTHONPATH, not uv run, if the
compiled extensions haven't been rebuilt for x86_64 in this worktree yet)
"""
import math
import sys
from types import SimpleNamespace

import numpy as np

from cereal import custom
from opendbc.car.car_helpers import interfaces
from opendbc.car.vehicle_model import VehicleModel
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.lib import latcontrol_torque as lct_module
from openpilot.tools.lib.logreader import LogReader, ReadMode

LAT_SMOOTH_SECONDS = 0.1  # GM-brand constant, see prototype_bolt_ff_tune.py's note

# Torque ceiling from finding #10 (check_torque_ceiling.py), Bolt 2022/2023 siglin curve
# at steer_max=1.0 -- static, ignores torque_params, so safe to hardcode here.
CEIL_MIN_LA = -3.403
CEIL_MAX_LA = 2.355

CORRECTION_BP = [2.5, 6.0, 8.0, 10.0, 12.0]
CORRECTION_V = [0.067, 0.156, 0.198, 0.121, 0.0]


def correction_magnitude(v_ego):
  return float(np.interp(v_ego, CORRECTION_BP, CORRECTION_V))


path = sys.argv[1]
t_start = float(sys.argv[2])
t_end = float(sys.argv[3])

original_ff_scale = lct_module.get_bolt_2022_2023_ff_scale


def patched_ff_scale(desired_lateral_accel, desired_lateral_jerk, v_ego):
  orig = original_ff_scale(desired_lateral_accel, desired_lateral_jerk, v_ego)
  if desired_lateral_accel == 0.0:
    return orig
  correction = math.copysign(correction_magnitude(v_ego), desired_lateral_accel)
  return orig + correction / desired_lateral_accel


def build_controller(car_fingerprint, CP):
  CarInterface = interfaces[car_fingerprint]
  CI = CarInterface(CP, custom.StarPilotCarParams.new_message())
  controller = LatControlTorque(CP, CI, DT_CTRL)
  VM = VehicleModel(CP)
  return controller, VM


from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque  # noqa: E402


def process_starpilot_toggles(toggles_text):
  if toggles_text:
    import json
    return SimpleNamespace(**json.loads(toggles_text))
  return SimpleNamespace()


def run(path, t_start, t_end, patched):
  if patched:
    lct_module.get_bolt_2022_2023_ff_scale = patched_ff_scale
  else:
    lct_module.get_bolt_2022_2023_ff_scale = original_ff_scale

  try:
    lr = LogReader(path, default_mode=ReadMode.RLOG, sort_by_time=True)

    car_params = None
    controller = None
    VM = None
    latest = {"carState": None, "liveParameters": None, "liveTorqueParameters": None,
              "latActive": False, "lat_delay_base": 0.1, "starpilot_toggles": SimpleNamespace(),
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
        latest["starpilot_toggles"] = process_starpilot_toggles(msg.starpilotPlan.starpilotToggles)
      elif which == "controlsState":
        if t0 is None:
          t0 = msg.logMonoTime / 1e9
        t_rel = msg.logMonoTime / 1e9 - t0
        if latest["carState"] is None or latest["liveParameters"] is None:
          continue

        CS = latest["carState"]
        lp = latest["liveParameters"]
        VM.update_params(max(lp.stiffnessFactor, 0.1), max(lp.steerRatio, 0.1))

        desired_curvature = msg.controlsState.desiredCurvature
        lat_delay = latest["lat_delay_base"] + LAT_SMOOTH_SECONDS

        output_torque_signed, _, pid_log = controller.update(
          latest["latActive"], CS, VM, lp, steer_limited_by_safety, desired_curvature,
          False, lat_delay, None, None, latest["starpilot_toggles"],
        )
        steer_limited_by_safety = abs(output_torque_signed - latest["carOutput_torque"]) > 1e-2

        if t_start - 0.5 <= t_rel <= t_end + 0.5:
          control_sum = pid_log.p + pid_log.i + pid_log.f + pid_log.d
          rows.append(dict(t=t_rel, v_ego=CS.vEgo, desired_la=pid_log.desiredLateralAccel,
                            actual_la=pid_log.actualLateralAccel, p=pid_log.p, i=pid_log.i,
                            f=pid_log.f, d=pid_log.d, control_sum=control_sum,
                            torque=output_torque_signed, active=pid_log.active))
    return rows
  finally:
    lct_module.get_bolt_2022_2023_ff_scale = original_ff_scale


print("Correction curve (v_ego breakpoints -> additive torque correction magnitude):")
for bp, v in zip(CORRECTION_BP, CORRECTION_V):
  print(f"  v_ego={bp:5.1f}  correction={v:.3f}")

print("\nRunning stock pass...", file=sys.stderr)
stock_rows = run(path, t_start, t_end, patched=False)
print("Running candidate pass...", file=sys.stderr)
cand_rows = run(path, t_start, t_end, patched=True)

stock_by_t = {round(r["t"], 3): r for r in stock_rows}
cand_by_t = {round(r["t"], 3): r for r in cand_rows}
common_t = sorted(set(stock_by_t) & set(cand_by_t))

print(f"\n{'t':>6s} {'v_ego':>6s} {'des_la':>7s} {'f_stk':>7s} {'f_cnd':>7s} "
      f"{'sum_stk':>8s} {'sum_cnd':>8s} {'trq_stk':>8s} {'trq_cnd':>8s}")

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
  print(f"{t:6.2f} {s['v_ego']:6.2f} {s['desired_la']:7.3f} {s['f']:7.3f} {c['f']:7.3f} "
        f"{s['control_sum']:8.3f} {c['control_sum']:8.3f} {s['torque']:8.3f} {c['torque']:8.3f}{marker}")

print(f"\nCeiling: [{CEIL_MIN_LA}, {CEIL_MAX_LA}]")
print(f"Control-signal (p+i+f+d) clipping against ceiling over [{t_start},{t_end}]s:")
print(f"  stock:     {n_clip_stock}/{n_in_window} ({100*n_clip_stock/max(n_in_window,1):.1f}%)  max excess={max_excess_stock:.3f}")
print(f"  candidate: {n_clip_cand}/{n_in_window} ({100*n_clip_cand/max(n_in_window,1):.1f}%)  max excess={max_excess_cand:.3f}")
print(f"Final-torque saturation (|torque|>=0.99):")
print(f"  stock:     {n_sat_stock}/{n_in_window} ({100*n_sat_stock/max(n_in_window,1):.1f}%)")
print(f"  candidate: {n_sat_cand}/{n_in_window} ({100*n_sat_cand/max(n_in_window,1):.1f}%)")
