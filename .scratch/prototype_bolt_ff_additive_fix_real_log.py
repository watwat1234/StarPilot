#!/usr/bin/env python3
"""Candidate fix (a) prototype, take 2 -- reads REAL logged p/i/f/d directly instead of
replaying LatControlTorque.update(). See finding #11 in bolt-tuning-916.md for why: the
open-loop replay approach (prototype_bolt_ff_additive_fix.py) was tried first and found to
diverge substantially from ground truth even in its own unpatched/stock run (p=5.2 vs the
real logged p=1.67 at t=7.14s) -- a real fidelity bug in that harness, not in the
candidate fix. Abandoned rather than debugged further given time budget; this script
avoids the whole problem.

Method: for each real sample in the window, take the REAL logged p, i, f, d
(torqueState fields -- ground truth, no replay). Compute the candidate's additive FF
correction from the sample's REAL v_ego and desired_la, add it to f only, and recompute
the sum. p and i are provably unaffected by an FF-only change: `error = setpoint -
measurement` depends only on the upstream planner curvature and the real measurement,
neither of which this correction touches (same argument finding #9 used for its own
FF-knob counterfactual). d likewise (`k_d * error_rate`, same error-only dependency).
So `p_real + i_real + (f_real + correction) + d_real` is a faithful reconstruction of
what the real controller's control_sum WOULD have been with this FF correction applied,
without needing to re-derive p/i/d from a possibly-buggy replay.

Caveats (same scope limits as every open-loop counterfactual in this investigation):
- Still open-loop: doesn't show whether the ringdown itself would be damped, only
  whether the internal correction signal would have clipped less along the real
  trajectory that actually happened under the stock tune.
- Does not re-derive p across SAMPLES either -- if a smaller `f` at sample N would have
  changed the error (and hence p) at sample N+1 by changing what the car actually did,
  this doesn't capture that (same limitation finding #9 already flagged and accepted).

Usage:
  uv run python3 .scratch/prototype_bolt_ff_additive_fix_real_log.py <rlog_path> <t_start> <t_end>
"""
import math
import sys

import numpy as np

from openpilot.tools.lib.logreader import LogReader, ReadMode

CEIL_MIN_LA = -3.403
CEIL_MAX_LA = 2.355

CORRECTION_BP = [2.5, 6.0, 8.0, 10.0, 12.0]
CORRECTION_V = [0.067, 0.156, 0.198, 0.121, 0.0]


def correction_magnitude(v_ego):
  return float(np.interp(v_ego, CORRECTION_BP, CORRECTION_V))


def correction_for(desired_la, v_ego):
  if desired_la == 0.0:
    return 0.0
  return math.copysign(correction_magnitude(v_ego), desired_la)


path = sys.argv[1]
t_start = float(sys.argv[2])
t_end = float(sys.argv[3])

print("Correction curve (v_ego breakpoints -> additive torque correction magnitude):")
for bp, v in zip(CORRECTION_BP, CORRECTION_V):
  print(f"  v_ego={bp:5.1f}  correction={v:.3f}")

lr = LogReader(path, default_mode=ReadMode.RLOG, sort_by_time=True)
t0 = None
latest_v_ego = None
rows = []
for msg in lr:
  try:
    which = msg.which()
  except Exception:
    continue
  if which == "carState":
    latest_v_ego = msg.carState.vEgo
  elif which == "controlsState":
    if t0 is None:
      t0 = msg.logMonoTime / 1e9
    t_rel = msg.logMonoTime / 1e9 - t0
    if not (t_start <= t_rel <= t_end):
      continue
    lateral_state = msg.controlsState.lateralControlState
    if lateral_state.which() != "torqueState":
      continue
    ts = lateral_state.torqueState
    if latest_v_ego is None:
      continue
    rows.append(dict(t=t_rel, v_ego=latest_v_ego, des_la=ts.desiredLateralAccel,
                      p=ts.p, i=ts.i, f=ts.f, d=ts.d))

print(f"\nn_samples in [{t_start},{t_end}]: {len(rows)}")
print(f"{'t':>6s} {'v_ego':>6s} {'des_la':>7s} {'f_stk':>7s} {'corr':>6s} {'f_cnd':>7s} "
      f"{'sum_stk':>8s} {'sum_cnd':>8s}")

n_clip_stock = n_clip_cand = 0
max_excess_stock = max_excess_cand = 0.0
worst_stock = worst_cand = None
for r in rows:
  corr = correction_for(r["des_la"], r["v_ego"])
  f_cnd = r["f"] + corr
  sum_stock = r["p"] + r["i"] + r["f"] + r["d"]
  sum_cand = r["p"] + r["i"] + f_cnd + r["d"]

  exc_s = max(0.0, sum_stock - CEIL_MAX_LA, CEIL_MIN_LA - sum_stock)
  exc_c = max(0.0, sum_cand - CEIL_MAX_LA, CEIL_MIN_LA - sum_cand)
  if exc_s > 0.001:
    n_clip_stock += 1
    if exc_s > max_excess_stock:
      max_excess_stock = exc_s
      worst_stock = r["t"]
  if exc_c > 0.001:
    n_clip_cand += 1
    if exc_c > max_excess_cand:
      max_excess_cand = exc_c
      worst_cand = r["t"]

  print(f"{r['t']:6.2f} {r['v_ego']:6.2f} {r['des_la']:7.3f} {r['f']:7.3f} {corr:6.3f} {f_cnd:7.3f} "
        f"{sum_stock:8.3f} {sum_cand:8.3f}")

n = len(rows)
print(f"\nCeiling: [{CEIL_MIN_LA}, {CEIL_MAX_LA}]")
print(f"Control-signal (p+i+f+d) clipping against ceiling over [{t_start},{t_end}]s (n={n}):")
print(f"  stock:     {n_clip_stock}/{n} ({100*n_clip_stock/max(n,1):.1f}%)  max excess={max_excess_stock:.3f} at t={worst_stock}")
print(f"  candidate: {n_clip_cand}/{n} ({100*n_clip_cand/max(n,1):.1f}%)  max excess={max_excess_cand:.3f} at t={worst_cand}")
