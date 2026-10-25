"""Torque learner (torqued) progress for the Galaxy Troubleshoot page, read from the LiveTorqueParameters cache.

torqued writes the cache once a minute onroad, so these rows are as fresh as that. Negative torque is a left curve
(torqued negates carOutput torque; checked against the GPS track on both cars, 2026-10-25). Counts and percentages
come from torqued's own TorqueBuckets, loaded the way torqued restores the cache.
"""
import inspect
import math
import os
from datetime import UTC, datetime

from cereal import car, log


def _row(row_id, label, value):
  return {"id": row_id, "label": label, "value": value, "resettable": False}


def allow_empty_buckets(buckets_cls, fingerprint):
  # wat-ioniq only (None elsewhere): mirrors torqued.main, which excuses the outer buckets for the Ioniq 6.
  if "allow_empty_buckets" not in inspect.signature(buckets_cls.__init__).parameters:
    return None
  from opendbc.car.hyundai.values import CAR
  return fingerprint == CAR.HYUNDAI_IONIQ_6


def load_buckets(torqued, points, allow_empty):
  # One load (np.append per point is the slow part); the normal-mode buckets share the decimated ones' queues.
  kwargs = {} if allow_empty is None else {"allow_empty_buckets": allow_empty}
  modes = {mode: torqued.TorqueBuckets(x_bounds=torqued.STEER_BUCKET_BOUNDS,
                                       min_points=torqued.MIN_BUCKET_POINTS / 10 if mode == "decimated" else torqued.MIN_BUCKET_POINTS,
                                       min_points_total=torqued.MIN_POINTS_TOTAL_QLOG if mode == "decimated" else torqued.MIN_POINTS_TOTAL,
                                       points_per_bucket=torqued.POINTS_PER_BUCKET, rowsize=3, **kwargs)
           for mode in ("decimated", "normal")}
  modes["decimated"].load_points(points)
  modes["normal"].buckets = modes["decimated"].buckets
  return modes


def applied(params, use_params):
  # controlsd applies the live values only with useParams or ForceAutoTune (gated by AdvancedLateralTune).
  try:
    return use_params or (params.get_bool("AdvancedLateralTune") and params.get_bool("ForceAutoTune"))
  except Exception:
    return use_params


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


def _fingerprint(params):
  # The cache was written under CarParamsPersistent; PrevRoute is the fallback if Persistent was cleared.
  for key in ("CarParamsPersistent", "CarParamsPrevRoute"):
    cp_bytes = params.get(key)
    if cp_bytes:
      with car.CarParams.from_bytes(cp_bytes) as cp:
        return str(cp.carFingerprint)
  return ""


def _build(params):
  from openpilot.selfdrive.locationd import torqued

  cache = params.get("LiveTorqueParameters")
  if not cache:
    return [_row("torque_learner", "Torque learner", "Unavailable")]
  with log.Event.from_bytes(cache) as evt:
    ltp = evt.liveTorqueParameters
    points = [list(p) for p in ltp.points]
    cached = {k: getattr(ltp, k) for k in ("liveValid", "useParams", "calPerc", "version", "latAccelFactorFiltered",
                                           "latAccelFactorRaw", "latAccelOffsetFiltered", "frictionCoefficientFiltered")}

  # The next drive compares this cache's CarParams with its own; only a torqued VERSION bump is knowable now.
  stale = cached["version"] != torqued.VERSION
  allow_empty = allow_empty_buckets(torqued.TorqueBuckets, _fingerprint(params))
  modes = load_buckets(torqued, points, allow_empty)
  last = [mode for mode, buckets in modes.items() if buckets.get_valid_percent() == cached["calPerc"]]
  next_mode = "normal" if cached["liveValid"] else "decimated"
  buckets = modes[next_mode]

  parts = [f"Valid ({cached['calPerc']}%)" if cached["liveValid"] else f"Learning {cached['calPerc']}%",
           f"points {len(buckets)} / {buckets.min_points_total}", f"next drive: {next_mode}"]
  if len(last) == 1:
    parts.append(f"last drive: {last[0]}")
  raw = f" (learner {cached['latAccelFactorRaw']:.2f})" if math.isfinite(cached["latAccelFactorRaw"]) and cached["latAccelFactorRaw"] else ""
  in_use = "in use" if applied(params, cached["useParams"]) else "not applied: auto-tune off"
  parts += [f"factor {cached['latAccelFactorFiltered']:.2f} {in_use}{raw}",f"offset {cached['latAccelOffsetFiltered']:+.2f}",
            f"friction {cached['frictionCoefficientFiltered']:.3f}"]
  age = _age_text(params)
  if age:
    parts.append(age)
  if stale:
    parts.append("cache is from an older torqued: it will start fresh")
  rows = [_row("torque_learner", "Torque learner", " · ".join(parts))]
  if stale:
    return rows

  excusable = getattr(buckets, "_is_excusable", lambda bound: False)
  for i, (bound, queue) in enumerate(buckets.buckets.items()):
    text = bucket_text(len(queue), buckets.buckets_min_points[bound], torqued.MIN_BUCKET_POINTS[i], excusable(bound),
                       next_mode == "decimated")
    rows.append(_row(f"torque_learner_bucket_{i}", bucket_label(*bound), text))
  return rows


def snapshot_items(params):
  try:
    return _build(params)
  except Exception:
    return [_row("torque_learner", "Torque learner", "Unavailable")]
