"""Torque learner (torqued) progress for the Galaxy Troubleshoot page, read from the LiveTorqueParameters cache.

torqued writes the cache once a minute onroad, so these rows are as fresh as that. Negative torque is a left curve
(torqued negates carOutput torque; checked against the GPS track on both cars, 2026-10-25).
"""
import inspect
import os
from datetime import UTC, datetime

from cereal import car, log


def _row(row_id, label, value):
  return {"id": row_id, "label": label, "value": value, "resettable": False}


def bucket_counts(points, bounds):
  counts = [0] * len(bounds)
  for point in points:
    for i, (lo, hi) in enumerate(bounds):
      if lo <= point[0] < hi:
        counts[i] += 1
        break
  return counts


def valid_percent(counts, min_points, min_total, exempt):
  # Mirrors PointBuckets.get_valid_percent (helpers.py), with wat-ioniq's excusable outer buckets.
  total_perc = min(sum(counts) / min_total * 100, 100)
  bucket_percs = [c / m * 100 for c, m, e in zip(counts, min_points, exempt, strict=True) if not e]
  return int((total_perc + (min(min(bucket_percs), 100) if bucket_percs else 0)) / 2)


def exempt_buckets(estimator_cls, fingerprint, n):
  # wat-ioniq only: torqued.main passes allow_empty_buckets for the Ioniq 6, excusing the two outer buckets.
  if "allow_empty_buckets" not in inspect.signature(estimator_cls.__init__).parameters:
    return [False] * n
  from opendbc.car.hyundai.values import CAR
  allowed = fingerprint == CAR.HYUNDAI_IONIQ_6
  return [allowed and i in (0, n - 1) for i in range(n)]


def bucket_label(lo, hi):
  side = "Left curves" if hi <= 0 else "Right curves"
  a, b = sorted((abs(lo), abs(hi)))
  return f"Torque learner: {side}, torque {a:g}–{b:g}"


def bucket_text(count, need, normal_need, exempt, decimated):
  if count >= need:
    return f"{count} · done"
  if exempt:
    return f"{count} / {need:g} · optional on this car"
  normal = f" (normal {normal_need:g})" if decimated else ""
  return f"{count} / {need:g}{normal} · short {need - count:g}"


def _age_text(params):
  try:
    age = datetime.now(UTC).timestamp() - os.path.getmtime(params.get_param_path("LiveTorqueParameters"))
  except Exception:
    return None
  if not 0 <= age < 30 * 86400:
    return None
  if age < 3600:
    return f"{int(age // 60)} min ago"
  return f"{int(age // 3600)} h ago" if age < 86400 else f"{int(age // 86400)} d ago"


def _build(params):
  from openpilot.selfdrive.locationd import torqued

  cache = params.get("LiveTorqueParameters")
  if not cache:
    return [_row("torque_learner", "Torque learner", "Unavailable")]
  with log.Event.from_bytes(cache) as evt:
    ltp = evt.liveTorqueParameters
    points = [list(p) for p in ltp.points]
    cached = {k: getattr(ltp, k) for k in ("liveValid", "calPerc", "version", "latAccelFactorFiltered", "latAccelFactorRaw",
                                           "latAccelOffsetFiltered", "frictionCoefficientFiltered")}

  current_cp, prev_cp = params.get("CarParamsPersistent"), params.get("CarParamsPrevRoute")
  fingerprint, stale = "", prev_cp is None
  if current_cp:
    with car.CarParams.from_bytes(current_cp) as cp:
      fingerprint = str(cp.carFingerprint)
      if prev_cp:
        with car.CarParams.from_bytes(prev_cp) as prev:
          stale = (torqued.TorqueEstimator.get_restore_key(prev, cached["version"]) !=
                   torqued.TorqueEstimator.get_restore_key(cp, torqued.VERSION))

  bounds, normal_min = torqued.STEER_BUCKET_BOUNDS, torqued.MIN_BUCKET_POINTS
  counts = bucket_counts(points, bounds)
  exempt = exempt_buckets(torqued.TorqueEstimator, fingerprint, len(bounds))
  modes = {"decimated": (normal_min / 10, torqued.MIN_POINTS_TOTAL_QLOG), "normal": (normal_min, torqued.MIN_POINTS_TOTAL)}
  last = [m for m, (mins, total) in modes.items() if valid_percent(counts, mins, total, exempt) == cached["calPerc"]]
  next_mode = "normal" if cached["liveValid"] else "decimated"
  mins, min_total = modes[next_mode]

  parts = [f"Valid ({cached['calPerc']}%)" if cached["liveValid"] else f"Learning {cached['calPerc']}%",
           f"points {sum(counts)} / {min_total}", f"next drive: {next_mode}"]
  if len(last) == 1:
    parts.append(f"last drive: {last[0]}")
  raw = f" (raw {cached['latAccelFactorRaw']:.2f})" if cached["latAccelFactorRaw"] else ""
  parts += [f"factor {cached['latAccelFactorFiltered']:.2f}{raw}", f"offset {cached['latAccelOffsetFiltered']:+.2f}",
            f"friction {cached['frictionCoefficientFiltered']:.3f}"]
  age = _age_text(params)
  if age:
    parts.append(age)
  if stale:
    parts.append("cache is from another tune or car: torqued will start fresh")
  rows = [_row("torque_learner", "Torque learner", " · ".join(parts))]
  if stale:
    return rows

  for i, (lo, hi) in enumerate(bounds):
    text = bucket_text(counts[i], mins[i], normal_min[i], exempt[i], next_mode == "decimated")
    rows.append(_row(f"torque_learner_bucket_{i}", bucket_label(lo, hi), text))
  return rows


def snapshot_items(params):
  try:
    return _build(params)
  except Exception:
    return [_row("torque_learner", "Torque learner", "Unavailable")]
