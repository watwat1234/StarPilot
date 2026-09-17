#!/usr/bin/env python3
"""Dump controlsState/carState samples in a specific time window of a segment."""
import sys

from openpilot.tools.lib.logreader import LogReader, ReadMode

path = sys.argv[1]
t_start = float(sys.argv[2])
t_end = float(sys.argv[3])

lr = LogReader(path, default_mode=ReadMode.RLOG, sort_by_time=True)

t0 = None
latest = {}
rows = []

for msg in lr:
  try:
    which = msg.which()
  except Exception:
    continue
  if t0 is None and which == "controlsState":
    t0 = msg.logMonoTime / 1e9
  if t0 is None:
    continue
  t_rel = msg.logMonoTime / 1e9 - t0

  if which in ("carState", "carControl"):
    latest[which] = getattr(msg, which)
  elif which == "controlsState":
    lateral_state = msg.controlsState.lateralControlState
    if lateral_state.which() != "torqueState":
      continue
    if not (t_start - 0.5 <= t_rel <= t_end + 0.5):
      continue
    ts = lateral_state.torqueState
    cs = latest.get("carState")
    cc = latest.get("carControl")
    rows.append(dict(
      t=t_rel,
      v_ego=cs.vEgo if cs else float("nan"),
      steer_angle=cs.steeringAngleDeg if cs else float("nan"),
      steer_pressed=cs.steeringPressed if cs else None,
      desired_la=ts.desiredLateralAccel,
      actual_la=ts.actualLateralAccel,
      desired_jerk=ts.desiredLateralJerk,
      p=ts.p, i=ts.i, f=ts.f, d=ts.d,
      torque_cmd=cc.actuators.torque if cc else float("nan"),
      active=ts.active,
      saturated=ts.saturated,
    ))

print(f"n_rows(with 0.5s pad)={len(rows)}")
print(f"{'t':>6s} {'v_ego':>6s} {'ang':>7s} {'press':>5s} {'des_la':>7s} {'act_la':>7s} {'jerk':>7s} {'p':>7s} {'i':>7s} {'f':>7s} {'d':>7s} {'torq':>7s} {'act':>4s} {'sat':>4s}")
for r in rows:
  marker = "" if t_start <= r["t"] <= t_end else " (pad)"
  print(
    f"{r['t']:6.2f} {r['v_ego']:6.2f} {r['steer_angle']:7.2f} {str(r['steer_pressed'])[:1]:>5s} "
    f"{r['desired_la']:7.3f} {r['actual_la']:7.3f} {r['desired_jerk']:7.3f} "
    f"{r['p']:7.3f} {r['i']:7.3f} {r['f']:7.3f} {r['d']:7.3f} {r['torque_cmd']:7.3f} "
    f"{str(r['active'])[:1]:>4s} {str(r['saturated'])[:1]:>4s}{marker}"
  )
