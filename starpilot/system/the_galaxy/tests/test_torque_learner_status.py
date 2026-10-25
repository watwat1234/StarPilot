import random

import numpy as np
import pytest

from cereal import car, log
from openpilot.selfdrive.locationd import torqued
from openpilot.starpilot.system.the_galaxy import torque_learner_status as tls

# Ioniq 6 cache after f6 (2026-10-24): 58% with the outer buckets excused, decimated next.
IONIQ_COUNTS = [0, 5, 201, 1500, 1500, 553, 20, 4]


class FakeParams:
  def __init__(self, tmp_path, values):
    self.tmp_path, self.values = tmp_path, values
    for key, value in values.items():
      if value is not None:
        (tmp_path / key).write_bytes(value)

  def get(self, key):
    return self.values.get(key)

  def get_param_path(self, key):
    return str(self.tmp_path / key)


def _points(counts):
  points = []
  for (lo, hi), n in zip(torqued.STEER_BUCKET_BOUNDS, counts, strict=True):
    points += [[lo + (hi - lo) * (i + 0.5) / max(n, 1), 0.0] for i in range(n)]
  return points


def _cache(counts, live_valid, cal_perc, version=torqued.VERSION):
  return log.Event.new_message(liveTorqueParameters={
    "version": version, "liveValid": live_valid, "calPerc": cal_perc, "points": _points(counts),
    "latAccelFactorFiltered": 4.5, "latAccelFactorRaw": 5.55, "latAccelOffsetFiltered": 0.0,
    "frictionCoefficientFiltered": 0.09,
  }).to_bytes()


def _cp(fingerprint="HYUNDAI_IONIQ_6", lat_accel_factor=4.5):
  return car.CarParams.new_message(carFingerprint=fingerprint,
                                   lateralTuning={"torque": {"friction": 0.09, "latAccelFactor": lat_accel_factor}}).to_bytes()


def _params(tmp_path, cache, prev=None, current=None):
  return FakeParams(tmp_path, {"LiveTorqueParameters": cache, "CarParamsPrevRoute": prev or _cp(),
                               "CarParamsPersistent": current or _cp()})


@pytest.fixture
def with_exemption(monkeypatch):
  class ExemptEstimator(torqued.TorqueEstimator):
    def __init__(self, CP, decimated=False, track_all_points=False, allow_empty_buckets=False):
      pass
  monkeypatch.setattr(torqued, "TorqueEstimator", ExemptEstimator)


def test_ioniq_cache_shape_with_exemption(tmp_path, with_exemption):
  rows = tls.snapshot_items(_params(tmp_path, _cache(IONIQ_COUNTS, False, 58)))
  summary = rows[0]["value"]
  assert summary.startswith("Learning 58% · points 3783 / 600 · next drive: decimated · last drive: decimated")
  assert "factor 4.50 (raw 5.55) · offset +0.00 · friction 0.090 · 0 min ago" in summary
  values = {r["label"]: r["value"] for r in rows[1:]}
  assert values["Torque learner: Left curves, torque 0.3–0.5"] == "0 / 10 · optional on this car"
  assert values["Torque learner: Left curves, torque 0.2–0.3"] == "5 / 30 (normal 300) · short 25"
  assert values["Torque learner: Right curves, torque 0.2–0.3"] == "20 / 30 (normal 300) · short 10"
  assert values["Torque learner: Right curves, torque 0–0.1"] == "1500 · done"
  assert all(r["resettable"] is False for r in rows)


def test_without_exemption_outer_buckets_count(tmp_path):
  # Dom-wat's torqued has no allow_empty_buckets: the empty outer bucket caps calPerc at 50.
  rows = tls.snapshot_items(_params(tmp_path, _cache(IONIQ_COUNTS, False, 50)))
  assert "last drive: decimated" in rows[0]["value"]
  assert rows[1]["value"] == "0 / 10 (normal 100) · short 10"


def test_valid_cache_next_drive_normal(tmp_path):
  # Valid after a decimated drive: the normal-mode minimums still apply next drive.
  counts = [150, 120, 600, 1500, 1500, 700, 400, 150]
  rows = tls.snapshot_items(_params(tmp_path, _cache(counts, True, 100)))
  assert rows[0]["value"].startswith("Valid (100%) · points 5120 / 4000 · next drive: normal · last drive: decimated ·")
  assert rows[2]["value"] == "120 / 300 · short 180"


def test_last_drive_omitted_when_both_modes_match(tmp_path):
  counts = [150, 320, 600, 1500, 1500, 700, 400, 150]
  rows = tls.snapshot_items(_params(tmp_path, _cache(counts, True, 100)))
  assert "last drive" not in rows[0]["value"]
  assert all(r["value"].endswith("· done") for r in rows[1:])


def test_stale_restore_key_shows_summary_only(tmp_path):
  rows = tls.snapshot_items(_params(tmp_path, _cache(IONIQ_COUNTS, False, 58), prev=_cp(lat_accel_factor=3.0)))
  assert len(rows) == 1
  assert rows[0]["value"].endswith("torqued will start fresh")


def test_old_cache_version_is_stale(tmp_path):
  rows = tls.snapshot_items(_params(tmp_path, _cache(IONIQ_COUNTS, False, 58, version=torqued.VERSION - 1)))
  assert rows[0]["value"].endswith("torqued will start fresh")


@pytest.mark.parametrize("cache", [None, b"", b"not a capnp message"])
def test_missing_or_garbage_cache_is_unavailable(tmp_path, cache):
  assert tls.snapshot_items(_params(tmp_path, cache)) == [
    {"id": "torque_learner", "label": "Torque learner", "value": "Unavailable", "resettable": False}]


def test_bucket_labels():
  labels = [tls.bucket_label(lo, hi) for lo, hi in torqued.STEER_BUCKET_BOUNDS]
  assert labels[0] == "Torque learner: Left curves, torque 0.3–0.5"
  assert labels[3] == "Torque learner: Left curves, torque 0–0.1"
  assert labels[4] == "Torque learner: Right curves, torque 0–0.1"
  assert labels[7] == "Torque learner: Right curves, torque 0.3–0.5"


def test_exempt_buckets_detection(with_exemption):
  assert tls.exempt_buckets(torqued.TorqueEstimator, "HYUNDAI_IONIQ_6", 8) == [True] + [False] * 6 + [True]
  assert tls.exempt_buckets(torqued.TorqueEstimator, "CHEVROLET_BOLT_EUV", 8) == [False] * 8


def test_exempt_buckets_absent_without_parameter():
  assert tls.exempt_buckets(torqued.TorqueEstimator, "HYUNDAI_IONIQ_6", 8) == [False] * 8


@pytest.mark.parametrize("decimated", [True, False])
def test_valid_percent_matches_torqued(decimated):
  rng = random.Random(0)
  mins = torqued.MIN_BUCKET_POINTS / 10 if decimated else torqued.MIN_BUCKET_POINTS
  total = torqued.MIN_POINTS_TOTAL_QLOG if decimated else torqued.MIN_POINTS_TOTAL
  for _ in range(50):
    points = [[rng.uniform(-0.6, 0.6), 0.0] for _ in range(rng.randint(1, 5000))]
    buckets = torqued.TorqueBuckets(x_bounds=torqued.STEER_BUCKET_BOUNDS, min_points=mins, min_points_total=total,
                                    points_per_bucket=torqued.POINTS_PER_BUCKET, rowsize=3)
    buckets.load_points(points)
    counts = [len(v) for v in buckets.buckets.values()]
    assert counts == [min(c, torqued.POINTS_PER_BUCKET) for c in tls.bucket_counts(points, torqued.STEER_BUCKET_BOUNDS)]
    assert tls.valid_percent(counts, mins, total, [False] * len(counts)) == buckets.get_valid_percent()
    assert np.isclose(sum(counts), len(buckets))
