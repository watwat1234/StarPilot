#!/usr/bin/env python3
"""Plot torqued's live parameters (lat accel factor, offset, friction) over one or more drives.

Each source is a local folder of segment folders (`<route>--<n>/qlog.zst`, as copied from the device's realdata),
or anything LogReader accepts (a route name, a segment range, a log file). Sources plot back to back in the order
given, with a dashed line where each one starts.

  ./dev python tools/tuning/plot_torque_learner.py ~/ioniq_qlogs --route 000000fc--e071479e87 -o fc.png
"""
import argparse
import glob
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from openpilot.tools.lib.logreader import LogReader

PARAMS = [  # (label, filtered field, raw field)
  ("Lat accel factor", "latAccelFactorFiltered", "latAccelFactorRaw"),
  ("Offset", "latAccelOffsetFiltered", "latAccelOffsetRaw"),
  ("Friction", "frictionCoefficientFiltered", "frictionCoefficientRaw"),
]


def local_segments(folder: str, route: str | None) -> dict[str, list[str]]:
  # {route: [log paths in segment order]}; qlogs preferred, rlogs if a segment has no qlog.
  routes: dict[str, list[tuple[int, str]]] = {}
  for seg in glob.glob(os.path.join(folder, "*--*")):
    match = re.fullmatch(r"(.+)--(\d+)", os.path.basename(seg))
    if not match or (route and match.group(1) != route):
      continue
    logs = sorted(glob.glob(os.path.join(seg, "qlog*"))) or sorted(glob.glob(os.path.join(seg, "rlog*")))
    if logs:
      routes.setdefault(match.group(1), []).append((int(match.group(2)), logs[0]))
  return {name: [path for _, path in sorted(segs)] for name, segs in sorted(routes.items())}


def read_source(name: str, logs) -> tuple[str, list[int], list[dict[str, float]]]:
  times, rows = [], []
  for msg in LogReader(logs, sort_by_time=True):
    if msg.which() == "liveTorqueParameters":
      ltp = msg.liveTorqueParameters
      times.append(msg.logMonoTime)
      rows.append({field: getattr(ltp, field) for _, *fields in PARAMS for field in fields})
  return name, times, rows


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("sources", nargs="+", help="local folder of segment folders, or a LogReader identifier")
  parser.add_argument("--route", help="with a local folder: only this route (default: every route in it)")
  parser.add_argument("-o", "--out", default="torque_learner.png", help="output image (default: %(default)s)")
  args = parser.parse_args()

  drives = []
  for source in args.sources:
    if os.path.isdir(source):
      routes = local_segments(source, args.route)
      if not routes:
        parser.error(f"no segment folders with logs in {source}" + (f" for route {args.route}" if args.route else ""))
      drives += [read_source(name, logs) for name, logs in routes.items()]
    else:
      drives.append(read_source(source, source))

  fig, axes = plt.subplots(len(PARAMS), 1, figsize=(11, 9), sharex=True)
  offset_min = 0.0
  for i, (name, times, rows) in enumerate(drives):
    if not rows:
      print(f"{name}: no liveTorqueParameters")
      continue
    minutes = [offset_min + (t - times[0]) / 60e9 for t in times]
    for ax, (_, filtered, raw) in zip(axes, PARAMS, strict=True):
      ax.plot(minutes, [r[raw] for r in rows], color="0.75", lw=0.8, label="raw fit" if i == 0 else None)
      ax.plot(minutes, [r[filtered] for r in rows], color="C0", lw=2, label="filtered (in use)" if i == 0 else None)
      if len(drives) > 1:
        ax.axvline(offset_min, color="0.4", ls="--", lw=0.8)
    print(f"{name}: " + ", ".join(f"{label} {rows[0][f]:.3f} -> {rows[-1][f]:.3f}" for label, f, _ in PARAMS))
    offset_min = minutes[-1]

  for ax, (label, *_) in zip(axes, PARAMS, strict=True):
    ax.set_ylabel(label)
    ax.grid(alpha=0.3)
  axes[0].legend(loc="lower right")
  axes[-1].set_xlabel("minutes of logs (drives back to back)" if len(drives) > 1 else "minutes since route start")
  fig.suptitle("Torque learner: " + ", ".join(name for name, _, _ in drives))
  fig.tight_layout()
  fig.savefig(args.out, dpi=110)
  print(f"wrote {args.out}")


if __name__ == "__main__":
  main()
