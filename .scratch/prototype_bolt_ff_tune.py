#!/usr/bin/env python3
"""Quick open-loop counterfactual for a candidate Bolt right-turn FF tune (finding #8
in bolt-tuning-916.md): rerun the real LatControlTorque.update() against segment 10's
logged inputs twice -- once with the stock BOLT_2022_2023_FF_GAIN_RIGHT /
TURN_IN_BOOST_RIGHT constants, once with a monkeypatched candidate -- and diff the
recomputed torque command / ff term over the 6.5-11s oscillation window.

IMPORTANT SCOPE LIMIT (read before trusting the output): this is OPEN-LOOP. CS
(steeringAngleDeg, vEgo, ...) is replayed verbatim from the log -- it's what the real
car actually did under the STOCK tune. Changing the FF constant changes the computed
torque *command* at each instant, but there is no plant model here to resimulate what
the car would then have actually done in response to a different command. So this can
show "would this tune have commanded meaningfully more torque / less saturation at each
instant along the real trajectory", which is informative (bigger FF should mean P has
less residual gap to close, so less saturation-prone), but it CANNOT show whether the
ringdown itself would actually be damped, since the measured lateral accel in later
cycles doesn't feed back through a changed physical response. Full validation requires
an actual on-vehicle or simulated-plant test, discussed separately.

Why P/I are unaffected (so this counterfactual is only isolating the FF-driven part,
not conflating it with the controller's error dynamics): `error = setpoint -
measurement` depends only on the upstream planner curvature and the logged measurement,
neither of which the FF gain/turn-in-boost constants touch. The PID's proportional and
integral terms are therefore byte-identical between the stock and candidate runs; only
`f` (feedforward) and the summed `output`/`torque_cmd` differ (output limiting rules can
also differ slightly on saturated cycles).
"""
import sys
from types import SimpleNamespace

from cereal import custom, log
from opendbc.car.car_helpers import interfaces
from opendbc.car.vehicle_model import VehicleModel
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.selfdrive.controls.lib import latcontrol_vehicle_tunes as tunes
from openpilot.tools.lib.logreader import LogReader, ReadMode

# Not importing get_control_lateral_smooth_seconds/get_torque_control_params from
# controlsd.py: that module transitively imports selfdrive/modeld/modeld.py ->
# msgq.visionipc, which needs a device-side compiled extension not worth building for
# this prototype (see project_native_ext_testing memory). Hardcoded instead:
# LAT_SMOOTH_SECONDS=0.1 is the GM-brand constant (confirmed in finding #7 of
# bolt-tuning-916.md). Live torque-param (liveTorqueParameters) application is also
# skipped -- both stock and candidate runs use CP's static torque_params, which is fine
# for an apples-to-apples relative diff even though it won't match the on-device
# absolute FF magnitude exactly (that fidelity is what replay_latcontrol_torque.py is
# for, not this script).
LAT_SMOOTH_SECONDS = 0.1

path = sys.argv[1]
t_start = float(sys.argv[2])
t_end = float(sys.argv[3])

# CORRECTED (see finding #8 addendum in bolt-tuning-916.md): positive desired_lateral_accel
# in this controller's sign convention is a PHYSICAL RIGHT turn (measured_curvature is the
# NEGATION of VM.calc_curvature, which itself is same-signed with steeringAngleDeg -- a
# positive/left steeringAngleDeg produces a negative measured_curvature). 137/10's right-turn
# oscillation therefore runs on the "_LEFT"-named constants (FF_GAIN_LEFT, TURN_IN_BOOST_LEFT),
# which are already the LARGER of the two knob pairs -- not the smaller "_RIGHT" ones. Rough
# sizing: finding #6's residual bias on this side was ~-0.2 to -0.24 with the *existing* boost
# already applied (peak ff_scale only 1.03-1.12x on a ~0.99-magnitude f term, i.e. ~3-12% extra,
# while the gap wants roughly ~20% more) -- so try roughly doubling both constants as a first
# probe, not a final tuned value.
CANDIDATE_FF_GAIN_LEFT = 0.20            # from 0.11
CANDIDATE_TURN_IN_BOOST_LEFT = 0.32      # from 0.18


def build_controller(car_fingerprint, CP):
  CarInterface = interfaces[car_fingerprint]
  CI = CarInterface(CP, custom.StarPilotCarParams.new_message())
  controller = LatControlTorque(CP, CI, DT_CTRL)
  VM = VehicleModel(CP)
  return controller, VM


def process_starpilot_toggles(toggles_text):
  if toggles_text:
    import json
    return SimpleNamespace(**json.loads(toggles_text))
  return SimpleNamespace()


def run(path, t_start, t_end, patch=None):
  """patch: optional dict of {module_attr_name: value} applied to latcontrol_vehicle_tunes
  before/while replaying. Reverted after the run."""
  originals = {}
  if patch:
    for k, v in patch.items():
      originals[k] = getattr(tunes, k)
      setattr(tunes, k, v)

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
        # t0 anchor must NOT depend on carParams having arrived yet -- matches the
        # convention used throughout bolt-tuning-916.md (t0 = first controlsState
        # message in the segment, full stop). Gating this on car_params (as an earlier
        # version of this script did) anchors t0 to whichever controlsState happens to
        # follow the first carParams message instead, which arrives ~2.3s later in this
        # file -- silently shifts every reported time in this script by that offset.
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
          rows.append(dict(t=t_rel, v_ego=CS.vEgo, desired_la=pid_log.desiredLateralAccel,
                            actual_la=pid_log.actualLateralAccel, p=pid_log.p, i=pid_log.i,
                            f=pid_log.f, torque=output_torque_signed, active=pid_log.active))
    return rows
  finally:
    for k, v in originals.items():
      setattr(tunes, k, v)


print(f"Candidate: FF_GAIN_LEFT {tunes.BOLT_2022_2023_FF_GAIN_LEFT} -> {CANDIDATE_FF_GAIN_LEFT}   "
      f"TURN_IN_BOOST_LEFT {tunes.BOLT_2022_2023_TURN_IN_BOOST_LEFT} -> {CANDIDATE_TURN_IN_BOOST_LEFT}")
print("Running stock pass...", file=sys.stderr)
stock_rows = run(path, t_start, t_end, patch=None)
print("Running candidate pass...", file=sys.stderr)
cand_rows = run(path, t_start, t_end, patch={
  "BOLT_2022_2023_FF_GAIN_LEFT": CANDIDATE_FF_GAIN_LEFT,
  "BOLT_2022_2023_TURN_IN_BOOST_LEFT": CANDIDATE_TURN_IN_BOOST_LEFT,
})

stock_by_t = {round(r["t"], 3): r for r in stock_rows}
cand_by_t = {round(r["t"], 3): r for r in cand_rows}
common_t = sorted(set(stock_by_t) & set(cand_by_t))

print(f"\n{'t':>6s} {'v_ego':>6s} {'des_la':>7s} {'act_la':>7s} "
      f"{'f_stk':>7s} {'f_cnd':>7s} {'trq_stk':>8s} {'trq_cnd':>8s} {'d_trq':>7s}")
n_sat_stock = n_sat_cand = n_in_window = 0
for t in common_t:
  s, c = stock_by_t[t], cand_by_t[t]
  marker = ""
  if t_start <= t <= t_end:
    marker = ""
    n_in_window += 1
    if abs(s["torque"]) >= 0.99:
      n_sat_stock += 1
    if abs(c["torque"]) >= 0.99:
      n_sat_cand += 1
  else:
    marker = " (pad)"
  print(f"{t:6.2f} {s['v_ego']:6.2f} {s['desired_la']:7.3f} {s['actual_la']:7.3f} "
        f"{s['f']:7.3f} {c['f']:7.3f} {s['torque']:8.3f} {c['torque']:8.3f} "
        f"{c['torque'] - s['torque']:7.3f}{marker}")

print(f"\nSaturation (|torque|>=0.99) fraction over [{t_start},{t_end}]s: "
      f"stock={n_sat_stock}/{n_in_window} ({100*n_sat_stock/max(n_in_window,1):.1f}%)  "
      f"candidate={n_sat_cand}/{n_in_window} ({100*n_sat_cand/max(n_in_window,1):.1f}%)")
