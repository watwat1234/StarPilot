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

  def get_bool(self, key):
    return bool(self.values.get(key))

  def get_param_path(self, key):
    return str(self.tmp_path / key)


def _points(counts):
  points = []
  for (lo, hi), n in zip(torqued.STEER_BUCKET_BOUNDS, counts, strict=True):
    points += [[lo + (hi - lo) * (i + 0.5) / max(n, 1), 0.0] for i in range(n)]
  return points


def _cache(counts, live_valid, cal_perc, version=torqued.VERSION, use_params=True, raw=5.55):
  return log.Event.new_message(liveTorqueParameters={
    "version": version, "liveValid": live_valid, "useParams": use_params, "calPerc": cal_perc, "points": _points(counts),
    "latAccelFactorFiltered": 4.5, "latAccelFactorRaw": raw, "latAccelOffsetFiltered": 0.0,
    "frictionCoefficientFiltered": 0.09,
  }).to_bytes()


def _cp(fingerprint="HYUNDAI_IONIQ_6", lat_accel_factor=4.5):
  return car.CarParams.new_message(carFingerprint=fingerprint,
                                   lateralTuning={"torque": {"friction": 0.09, "latAccelFactor": lat_accel_factor}}).to_bytes()


def _params(tmp_path, cache, prev=b"default", current=b"default", **bools):
  return FakeParams(tmp_path, {"LiveTorqueParameters": cache,
                               "CarParamsPrevRoute": _cp() if prev == b"default" else prev,
                               "CarParamsPersistent": _cp() if current == b"default" else current, **bools})


@pytest.fixture
def with_exemption(monkeypatch):
  # Test double of wat-ioniq's PointBuckets (helpers.py): the outer buckets are excusable when allowed.
  class ExemptBuckets(torqued.TorqueBuckets):
    def __init__(self, *args, allow_empty_buckets=False, **kwargs):
      super().__init__(*args, **kwargs)
      self.allow_empty_buckets = allow_empty_buckets

    def _is_excusable(self, bound):
      return self.allow_empty_buckets and bound in (self.x_bounds[0], self.x_bounds[-1])

    def get_valid_percent(self):
      total_perc = min(len(self) / self.min_points_total * 100, 100)
      percs = [len(v) / self.buckets_min_points[b] * 100 for b, v in self.buckets.items() if not self._is_excusable(b)]
      return int((total_perc + (min(min(percs), 100) if percs else 0)) / 2)
  monkeypatch.setattr(torqued, "TorqueBuckets", ExemptBuckets)


def test_ioniq_cache_shape_with_exemption(tmp_path, with_exemption):
  rows = tls.snapshot_items(_params(tmp_path, _cache(IONIQ_COUNTS, False, 58)))
  summary = rows[0]["value"]
  assert summary.startswith("Learning 58% · points 3783 / 600 · next drive: decimated · last drive: decimated")
  assert "factor 4.50 in use (learner 5.55) · offset +0.00 · friction 0.090 · 0 min ago" in summary
  values = {r["label"]: r["value"] for r in rows[1:]}
  assert values["Torque learner: Left curves, torque 0.3–0.5"] == "0 / 10 · optional on this car"
  assert values["Torque learner: Left curves, torque 0.2–0.3"] == "5 / 30 (normal 300) · short 25"
  assert values["Torque learner: Right curves, torque 0.2–0.3"] == "20 / 30 (normal 300) · short 10"
  assert values["Torque learner: Right curves, torque 0–0.1"] == "1500 · done"
  assert all(r["resettable"] is False for r in rows)


def test_exemption_falls_back_to_prev_route_fingerprint(tmp_path, with_exemption):
  rows = tls.snapshot_items(_params(tmp_path, _cache(IONIQ_COUNTS, False, 58), current=None))
  assert "last drive: decimated" in rows[0]["value"]
  assert rows[1]["value"] == "0 / 10 · optional on this car"


def test_exemption_only_for_ioniq_6(tmp_path, with_exemption):
  rows = tls.snapshot_items(_params(tmp_path, _cache(IONIQ_COUNTS, False, 50), current=_cp("CHEVROLET_BOLT_EUV")))
  assert "last drive: decimated" in rows[0]["value"]
  assert rows[1]["value"] == "0 / 10 (normal 100) · short 10"


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


def test_bucket_counts_capped_like_torqued(tmp_path):
  rows = tls.snapshot_items(_params(tmp_path, _cache([0, 0, 0, 2000, 0, 0, 0, 0], False, 0)))
  assert rows[4]["value"] == "1500 · done"


@pytest.mark.parametrize("bools, expected", [
  ({}, "factor 4.50 not applied: auto-tune off (learner 5.55)"),
  ({"ForceAutoTune": b"1"}, "factor 4.50 not applied: auto-tune off (learner 5.55)"),
  ({"AdvancedLateralTune": b"1", "ForceAutoTune": b"1"}, "factor 4.50 in use (learner 5.55)"),
])
def test_factor_applied_only_with_auto_tune(tmp_path, bools, expected):
  # e.g. GM is outside torqued.ALLOWED_CARS, so useParams is False unless ForceAutoTune is on.
  rows = tls.snapshot_items(_params(tmp_path, _cache(IONIQ_COUNTS, False, 50, use_params=False), **bools))
  assert expected in rows[0]["value"]


@pytest.mark.parametrize("raw", [0.0, float("nan")])
def test_missing_raw_factor_is_omitted(tmp_path, raw):
  rows = tls.snapshot_items(_params(tmp_path, _cache(IONIQ_COUNTS, False, 50, raw=raw)))
  assert "factor 4.50 in use · offset" in rows[0]["value"]


def test_changed_tune_is_not_stale(tmp_path):
  # The next drive compares the cache's own CarParams (copied into PrevRoute at start) with its new ones.
  rows = tls.snapshot_items(_params(tmp_path, _cache(IONIQ_COUNTS, False, 50), prev=_cp(lat_accel_factor=3.0)))
  assert len(rows) == 9


def test_old_cache_version_is_stale(tmp_path):
  rows = tls.snapshot_items(_params(tmp_path, _cache(IONIQ_COUNTS, False, 58, version=torqued.VERSION - 1)))
  assert len(rows) == 1
  assert rows[0]["value"].endswith("cache is from an older torqued: it will start fresh")


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
