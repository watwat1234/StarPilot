#!/usr/bin/env python3
"""Scan Bolt route segments for the CONTROLLER-BEHAVIOR mechanism found in
route 00000137--3003b4ee60--10, t=6.5-11s (see .scratch/bolt-tuning-916.md
findings #3/#4/#6), rather than the exact steering-angle ringdown shape (the
user notes that specific waveform is road-geometry-specific to that one
driveway and won't recur elsewhere).

The mechanism, not the waveform:
  - actuator torque_cmd pinned near the +-1.0 limit for a sustained stretch
    (finding #3: 46+ consecutive 100Hz samples during the worst dip)
  - happening in the ~5-9 m/s low/transition speed band, where finding #6
    showed the FF (siglin) curve has its worst under-prediction bias and is
    structurally unvalidated by the live torque estimator (MIN_VEL=15)
  - P term working hard to compensate (finding #4: mean|p|=0.57, peak 1.87
    during the event, versus i staying modest -- so this is a P/FF story,
    not a windup story)

This scanner finds contiguous saturation runs (|torque_cmd| >= 0.99) of at
least MIN_SAT_S, reports their speed band / P-term / tracking-error stats,
and flags ones that land in the FF-under-prediction speed band with driver
hands off the wheel (steering not pressed, so it's not a manual override).

Usage:
  uv run python3 .scratch/scan_saturation_events.py <route_dir> [--min-sat-s 0.15]

<route_dir> e.g. /mnt/c/Users/Kirin/Documents/bolt_routes/00000135--dab4b928bd
(a dir containing nested segment subdirs <basename>--N, each with rlog.zst)
"""
import argparse
import glob
import os
import sys

import numpy as np

from openpilot.tools.lib.logreader import LogReader, ReadMode

AT_LIMIT_TORQUE = 0.99  # steer_max = 1.0, latcontrol.py:17
FF_UNDERPREDICT_BAND = (5.0, 9.0)  # m/s, worst bias band from finding #6
DEFAULT_MIN_SAT_S = 0.15

# Shape-classification knobs, added after finding that a plain saturation
# scan also fires on ordinary large monotonic unwinds (e.g. a 90-degree turn
# onto a straight road spans hundreds of degrees of steering angle with no
# direction reversal at all -- see 138/seg0 t~34-36s and 135/seg6, 138/seg10
# t~43-45s, all confirmed by the user to be 90-degree-left-into-straight
# turns, NOT the 137/10 continuing-curve ringing pattern). These try to tell
# the two apart:
SHAPE_PRE_S = 0.5
SHAPE_POST_S = 2.0
MAX_RINGING_SPAN_DEG = 150.0   # 137/10's swings were 30-115 deg; a >150deg span is a big unwind, not ringing
MIN_REVERSALS = 2              # need at least 2 genuine direction reversals to call it "ringing"
CURVE_CONTINUES_LA_MIN = 0.3   # m/s^2; desired_la after the window must still be meaningfully nonzero


def find_extrema_robust(t: np.ndarray, ang: np.ndarray):
  """Local-extrema scan that correctly handles flat plateaus (repeated
  identical samples at a true peak/trough, common due to angle-sensor
  quantization) by working on the compressed sequence of NONZERO diffs
  instead of comparing only immediate neighbors -- a plain adjacent-diff
  scan silently drops any extremum sitting on such a plateau."""
  if len(ang) < 3:
    return []
  d = np.diff(ang)
  nz_idx = np.nonzero(d)[0]
  if len(nz_idx) < 2:
    return []
  nz_signs = np.sign(d[nz_idx])
  extrema = []
  for k in range(1, len(nz_idx)):
    if nz_signs[k] != nz_signs[k - 1]:
      sample_idx = nz_idx[k]  # first sample of the (possibly flat) plateau at the extreme
      extrema.append((t[sample_idx], ang[sample_idx]))
  return extrema


def analyze_shape(t, ang, desired_la, t_start, t_end):
  """Distinguish 'ringing around a continuing curve' (137/10-style) from a
  plain large monotonic unwind (the 90-degree-turn false positives)."""
  win_mask = (t >= t_start - SHAPE_PRE_S) & (t <= t_end + SHAPE_POST_S)
  t_win, ang_win = t[win_mask], ang[win_mask]
  extrema = find_extrema_robust(t_win, ang_win)
  net_span = float(ang_win.max() - ang_win.min()) if len(ang_win) else float("nan")

  pre_mask = (t >= t_start - SHAPE_PRE_S) & (t < t_start)
  post_end = t_end + SHAPE_POST_S
  post_mask = (t > post_end - 0.5) & (t <= post_end)
  la_before = float(np.mean(desired_la[pre_mask])) if pre_mask.any() else float("nan")
  la_after = float(np.mean(desired_la[post_mask])) if post_mask.any() else float("nan")
  continues_curving = (
    not np.isnan(la_before) and not np.isnan(la_after)
    and np.sign(la_before) == np.sign(la_after)
    and abs(la_after) >= CURVE_CONTINUES_LA_MIN
  )

  return dict(
    n_reversals=len(extrema),
    net_span=net_span,
    la_before=la_before, la_after=la_after,
    continues_curving=continues_curving,
    is_ringing_candidate=(len(extrema) >= MIN_REVERSALS and net_span < MAX_RINGING_SPAN_DEG and continues_curving),
  )


def get_driving_model(path: str) -> str:
  lr = LogReader(path, default_mode=ReadMode.RLOG, sort_by_time=True)
  for msg in lr:
    try:
      which = msg.which()
    except Exception:
      continue
    if which == "initData":
      params = {e.key: e.value for e in msg.initData.params.entries}
      name = params.get("DrivingModelName", "?")
      model = params.get("DrivingModel", "?")
      return f"{model} ({name})"
  return "unknown (no initData)"


def load_series(path: str):
  lr = LogReader(path, default_mode=ReadMode.RLOG, sort_by_time=True)
  t0 = None
  latest = {}
  rows = []
  for msg in lr:
    try:
      which = msg.which()
    except Exception:
      continue
    if which in ("carState", "carControl"):
      latest[which] = getattr(msg, which)
    elif which == "controlsState":
      if t0 is None:
        t0 = msg.logMonoTime / 1e9
      t_rel = msg.logMonoTime / 1e9 - t0
      lateral_state = msg.controlsState.lateralControlState
      if lateral_state.which() != "torqueState":
        continue
      ts = lateral_state.torqueState
      cs = latest.get("carState")
      cc = latest.get("carControl")
      if cs is None:
        continue
      rows.append(dict(
        t=t_rel,
        v_ego=cs.vEgo,
        steer_angle=cs.steeringAngleDeg,
        steer_pressed=cs.steeringPressed,
        desired_la=ts.desiredLateralAccel,
        actual_la=ts.actualLateralAccel,
        p=ts.p, i=ts.i, f=ts.f, d=ts.d,
        torque_cmd=cc.actuators.torque if cc else float("nan"),
        active=ts.active,
      ))
  return rows


def find_saturation_runs(rows, min_sat_s: float):
  runs = []
  cur = []
  for r in rows:
    if r["active"] and abs(r["torque_cmd"]) >= AT_LIMIT_TORQUE:
      cur.append(r)
    else:
      if cur:
        runs.append(cur)
      cur = []
  if cur:
    runs.append(cur)

  t_all = np.array([r["t"] for r in rows])
  ang_all = np.array([r["steer_angle"] for r in rows])
  la_all = np.array([r["desired_la"] for r in rows])

  events = []
  for run in runs:
    duration = run[-1]["t"] - run[0]["t"]
    if duration < min_sat_s:
      continue
    v = np.array([r["v_ego"] for r in run])
    p = np.array([r["p"] for r in run])
    f = np.array([r["f"] for r in run])
    err = np.array([r["actual_la"] - r["desired_la"] for r in run])
    pressed_any = any(r["steer_pressed"] for r in run)
    in_ff_band = bool(np.any((v >= FF_UNDERPREDICT_BAND[0]) & (v <= FF_UNDERPREDICT_BAND[1])))
    shape = analyze_shape(t_all, ang_all, la_all, run[0]["t"], run[-1]["t"])
    events.append(dict(
      t_start=run[0]["t"], t_end=run[-1]["t"], duration=duration,
      n_samples=len(run),
      v_range=(float(v.min()), float(v.max())),
      p_mean=float(np.mean(np.abs(p))), p_peak=float(np.max(np.abs(p))),
      f_mean=float(np.mean(np.abs(f))),
      err_range=(float(err.min()), float(err.max())),
      steer_pressed_any=pressed_any,
      in_ff_underpredict_band=in_ff_band,
      torque_sign=int(np.sign(run[len(run) // 2]["torque_cmd"])),
      shape=shape,
    ))
  return events


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("route_dir")
  ap.add_argument("--min-sat-s", type=float, default=DEFAULT_MIN_SAT_S)
  args = ap.parse_args()

  route_dir = args.route_dir.rstrip("/")
  seg_glob = os.path.join(route_dir, os.path.basename(route_dir) + "--*")
  seg_dirs = sorted(
    glob.glob(seg_glob),
    key=lambda p: int(p.rsplit("--", 1)[-1]) if p.rsplit("--", 1)[-1].isdigit() else 999,
  )
  if not seg_dirs:
    print(f"No segment dirs found matching {seg_glob}", file=sys.stderr)
    sys.exit(1)

  for seg_dir in seg_dirs:
    seg_num = seg_dir.rsplit("--", 1)[-1]
    rlog = os.path.join(seg_dir, "rlog.zst")
    if not os.path.exists(rlog):
      print(f"segment {seg_num}: no rlog.zst, skipping")
      continue
    model = get_driving_model(rlog)
    rows = load_series(rlog)
    events = find_saturation_runs(rows, args.min_sat_s)
    interesting = [e for e in events if not e["steer_pressed_any"]]
    print(f"\n=== segment {seg_num}  model={model}  n_samples={len(rows)}  "
          f"sat_events={len(events)} (hands-off={len(interesting)}) ===")
    for e in events:
      s = e["shape"]
      flag = " <-- RINGING candidate: continues curving, bounded swing, matches 137/10 mechanism" \
        if e["in_ff_underpredict_band"] and not e["steer_pressed_any"] and s["is_ringing_candidate"] \
        else ""
      print(
        f"  t={e['t_start']:.2f}-{e['t_end']:.2f}s  dur={e['duration']:.2f}s  "
        f"v_range=[{e['v_range'][0]:.1f},{e['v_range'][1]:.1f}]  "
        f"p_mean={e['p_mean']:.2f} p_peak={e['p_peak']:.2f}  f_mean={e['f_mean']:.2f}  "
        f"err_range=[{e['err_range'][0]:.2f},{e['err_range'][1]:.2f}]  "
        f"pressed={e['steer_pressed_any']}  sign={e['torque_sign']:+d}  "
        f"shape[reversals={s['n_reversals']} span={s['net_span']:.0f}deg "
        f"la_before={s['la_before']:.2f} la_after={s['la_after']:.2f} "
        f"continues_curving={s['continues_curving']}]{flag}"
      )


if __name__ == "__main__":
  main()
