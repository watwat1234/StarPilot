#!/usr/bin/env python3
"""Scan a Bolt route's segments for the decaying-oscillation ("ringdown")
steering signature found manually in route 00000137--3003b4ee60--10,
t=6.5-11s (see .scratch/bolt-tuning-916.md finding #2/#3):

  - steering_angle_deg local extrema alternate sign
  - magnitude decays across >=3 consecutive extrema
  - half-periods roughly 0.3-1.2s (~1-1.2 Hz)
  - first swing in the sequence is not tiny (skip noise-level wiggle)

Deliberately does NOT reuse analyze_bolt_lateral.py's detect_turn_in_events --
that detector already missed this exact window on route 137 seg 10 (see
Environment section caveat in bolt-tuning-916.md), so it can't be trusted as
the sole scanner here. This is a fresh, angle-based extrema scan instead.

Also does NOT reuse the session's old analyze_window.py extrema logic --
that had a known clustering bug (a bigger extremum arriving slightly later in
the same 0.15s window got dropped). This version instead does a proper
single-pass local-extrema scan over ALL samples first, then a much smaller
min-separation dedupe pass that always keeps the larger-magnitude point when
two extrema of the SAME sign are within the separation window (the actual bug
class from before), rather than blindly keeping whichever came first.

Usage:
  uv run python3 .scratch/scan_ringdown_signature.py <route_dir_containing_segments> [--min-swing DEG] [--min-decay N]

<route_dir_containing_segments> e.g.
  /mnt/c/Users/Kirin/Documents/bolt_routes/00000135--dab4b928bd
It will glob */rlog.zst under <route_dir>--N subdirs matching the route prefix.
"""
import argparse
import glob
import os
import sys

import numpy as np

from openpilot.tools.lib.logreader import LogReader, ReadMode

MIN_SEP_S = 0.15  # dedupe window for same-direction extrema noise, mirrors old script's window
DEFAULT_MIN_SWING_DEG = 15.0  # first excursion must be at least this big to be interesting
DEFAULT_MIN_DECAY_COUNT = 3   # need at least this many alternating-sign extrema
HALF_PERIOD_MIN_S = 0.25
HALF_PERIOD_MAX_S = 1.3


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
      if isinstance(name, bytes):
        name = name.decode(errors="replace")
      if isinstance(model, bytes):
        model = model.decode(errors="replace")
      return f"{model} ({name})"
  return "unknown (no initData)"


def load_series(path: str):
  """Returns t_rel (s, since first controlsState), steer_angle_deg, v_ego, torque_cmd, steering_pressed."""
  lr = LogReader(path, default_mode=ReadMode.RLOG, sort_by_time=True)
  t0 = None
  latest = {}
  ts, angs, vs, torqs, presseds = [], [], [], [], []
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
      cs = latest.get("carState")
      cc = latest.get("carControl")
      if cs is None:
        continue
      ts.append(t_rel)
      angs.append(cs.steeringAngleDeg)
      vs.append(cs.vEgo)
      torqs.append(cc.actuators.torque if cc else float("nan"))
      presseds.append(cs.steeringPressed)
  return (np.array(ts), np.array(angs), np.array(vs), np.array(torqs), np.array(presseds))


def find_extrema(t: np.ndarray, ang: np.ndarray):
  """Single-pass local extrema (strict sign change in derivative), then a
  same-sign dedupe pass keeping the larger-magnitude candidate within MIN_SEP_S."""
  if len(ang) < 3:
    return []
  d = np.diff(ang)
  raw_idx = []
  for i in range(1, len(d)):
    if d[i - 1] == 0:
      continue
    if np.sign(d[i - 1]) != np.sign(d[i]) and np.sign(d[i]) != 0:
      raw_idx.append(i)
  if not raw_idx:
    return []

  # dedupe: walk sorted by time, merge same-type (max/min) extrema within MIN_SEP_S,
  # always keeping the more extreme one, and correctly extending the merge window
  # from the KEPT point's time (not the first point's time) so a bigger extremum
  # arriving slightly later still gets folded in correctly.
  extrema = [(t[i], ang[i], i) for i in raw_idx]
  extrema.sort(key=lambda x: x[0])

  deduped = []
  for cand in extrema:
    if deduped:
      last = deduped[-1]
      is_max_last = last[1] >= 0
      is_max_cand = cand[1] >= 0
      # only merge candidates of the same extremum "type" (both local max-ish or both min-ish
      # in the sense of sharing sign); real alternating extrema of opposite sign are never merged.
      if is_max_last == is_max_cand and (cand[0] - last[0]) <= MIN_SEP_S:
        if abs(cand[1]) > abs(last[1]):
          deduped[-1] = cand
        continue
    deduped.append(cand)
  return deduped


def scan_segment(path: str, min_swing: float, min_decay: int):
  t, ang, v, torq, pressed = load_series(path)
  if len(t) == 0:
    return []
  extrema = find_extrema(t, ang)
  events = []
  i = 0
  while i < len(extrema) - 1:
    # try to grow a decaying alternating-sign run starting at i
    run = [extrema[i]]
    j = i + 1
    while j < len(extrema):
      prev_t, prev_a, _ = run[-1]
      cur_t, cur_a, _ = extrema[j]
      dt = cur_t - prev_t
      alternates = (prev_a >= 0) != (cur_a >= 0)
      # decay condition: magnitude of successive swings should not increase
      swing_prev = abs(run[-1][1] - (run[-2][1] if len(run) >= 2 else 0.0))
      if len(run) == 1:
        ok_decay = True
      else:
        swing_cur = abs(cur_a - prev_a)
        ok_decay = swing_cur <= swing_prev * 1.05  # allow tiny noise tolerance
      if alternates and HALF_PERIOD_MIN_S <= dt <= HALF_PERIOD_MAX_S and ok_decay:
        run.append(extrema[j])
        j += 1
      else:
        break
    if len(run) - 1 >= min_decay:
      first_swing = abs(run[1][1] - run[0][1])
      if first_swing >= min_swing:
        t_start = run[0][0]
        t_end = run[-1][0]
        mask = (t >= t_start - 0.1) & (t <= t_end + 0.1)
        sat_frac = float(np.mean(np.abs(torq[mask]) >= 0.99)) if mask.any() else 0.0
        v_range = (float(v[mask].min()), float(v[mask].max())) if mask.any() else (float("nan"),) * 2
        pressed_any = bool(np.any(pressed[mask])) if mask.any() else False
        events.append(dict(
          t_start=t_start, t_end=t_end,
          n_extrema=len(run),
          amplitudes=[round(float(a), 1) for _, a, _ in run],
          first_swing=round(first_swing, 1),
          sat_frac=sat_frac,
          v_range=v_range,
          steering_pressed=pressed_any,
        ))
      i = j - 1 if j > i + 1 else i + 1
    else:
      i += 1
  return events


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("route_dir", help="e.g. /mnt/c/.../bolt_routes/00000135--dab4b928bd "
                                     "(a route dir CONTAINING segment subdirs named <route_dir_basename>--N)")
  ap.add_argument("--min-swing", type=float, default=DEFAULT_MIN_SWING_DEG)
  ap.add_argument("--min-decay", type=int, default=DEFAULT_MIN_DECAY_COUNT)
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
    events = scan_segment(rlog, args.min_swing, args.min_decay)
    print(f"\n=== segment {seg_num}  model={model}  events_found={len(events)} ===")
    for ev in events:
      print(
        f"  t={ev['t_start']:.2f}-{ev['t_end']:.2f}s  n_extrema={ev['n_extrema']}  "
        f"amps={ev['amplitudes']}  first_swing={ev['first_swing']}deg  "
        f"sat_frac={ev['sat_frac']:.2f}  v_range=[{ev['v_range'][0]:.1f},{ev['v_range'][1]:.1f}]  "
        f"steering_pressed_during={ev['steering_pressed']}"
      )


if __name__ == "__main__":
  main()
