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


def _values(params):
  (section,) = tls.sections(params)
  return {item["label"]: item["value"] for item in section["items"]}


def test_section_shape(tmp_path):
  (section,) = tls.sections(_params(tmp_path, _cache(IONIQ_COUNTS, False, 50)))
  assert section["title"] == "Lateral (Steering) › Torque Learner"
  assert section["resettable"] is False and section["hasLearnedColumn"] is False
  assert all(item["readOnly"] and item["defaultValue"] is None and item["key"].startswith("TorqueLearner")
             for item in section["items"])
  assert len({item["key"] for item in section["items"]}) == len(section["items"])


def test_ioniq_cache_shape_with_exemption(tmp_path, with_exemption):
  values = _values(_params(tmp_path, _cache(IONIQ_COUNTS, False, 58)))
  assert values["State"] == "Learning 58%"
  assert values["Points"] == "3783 / 600"
  assert values["Next drive"] == "decimated" and values["Last drive"] == "decimated"
  assert values["Lat accel factor"] == "4.50 in use (learner 5.55)"
  assert (values["Offset"], values["Friction"], values["Cache updated"]) == ("+0.00", "0.090", "0 min ago")
  assert values["Left curves, torque 0.3–0.5"] == "0 / 10 · optional on this car"
  assert values["Left curves, torque 0.2–0.3"] == "5 / 30 (normal 300) · short 25"
  assert values["Right curves, torque 0.2–0.3"] == "20 / 30 (normal 300) · short 10"
  assert values["Right curves, torque 0–0.1"] == "1500 · done"


def test_exemption_falls_back_to_prev_route_fingerprint(tmp_path, with_exemption):
  values = _values(_params(tmp_path, _cache(IONIQ_COUNTS, False, 58), current=None))
  assert values["Last drive"] == "decimated"
  assert values["Left curves, torque 0.3–0.5"] == "0 / 10 · optional on this car"


@pytest.mark.parametrize("exempt", [True, False])
def test_no_exemption_for_other_cars(tmp_path, request, exempt):
  # Holds with wat-ioniq's rules (exempt) and Dom-wat's: the empty outer bucket caps calPerc at 50.
  if exempt:
    request.getfixturevalue("with_exemption")
  values = _values(_params(tmp_path, _cache(IONIQ_COUNTS, False, 50), current=_cp("CHEVROLET_BOLT_EUV")))
  assert values["Last drive"] == "decimated"
  assert values["Left curves, torque 0.3–0.5"] == "0 / 10 (normal 100) · short 10"


def test_valid_cache_next_drive_normal(tmp_path):
  # Valid after a decimated drive: the normal-mode minimums still apply next drive.
  values = _values(_params(tmp_path, _cache([150, 120, 600, 1500, 1500, 700, 400, 150], True, 100)))
  assert (values["State"], values["Points"], values["Next drive"]) == ("Valid (100%)", "5120 / 4000", "normal")
  assert values["Last drive"] == "decimated"
  assert values["Left curves, torque 0.2–0.3"] == "120 / 300 · short 180"


def test_last_drive_omitted_when_both_modes_match(tmp_path):
  values = _values(_params(tmp_path, _cache([150, 320, 600, 1500, 1500, 700, 400, 150], True, 100)))
  assert "Last drive" not in values
  assert all(v.endswith("· done") for k, v in values.items() if "curves" in k)


def test_bucket_counts_capped_like_torqued(tmp_path):
  values = _values(_params(tmp_path, _cache([0, 0, 0, 2000, 0, 0, 0, 0], False, 0)))
  assert values["Left curves, torque 0–0.1"] == "1500 · done"


@pytest.mark.parametrize("bools, expected", [
  ({}, "4.50 not applied: auto-tune off (learner 5.55)"),
  ({"ForceAutoTune": b"1"}, "4.50 not applied: auto-tune off (learner 5.55)"),
  ({"AdvancedLateralTune": b"1", "ForceAutoTune": b"1"}, "4.50 in use (learner 5.55)"),
])
def test_factor_applied_only_with_auto_tune(tmp_path, bools, expected):
  # e.g. GM is outside torqued.ALLOWED_CARS, so useParams is False unless ForceAutoTune is on.
  values = _values(_params(tmp_path, _cache(IONIQ_COUNTS, False, 50, use_params=False), **bools))
  assert values["Lat accel factor"] == expected


@pytest.mark.parametrize("raw", [0.0, float("nan")])
def test_missing_raw_factor_is_omitted(tmp_path, raw):
  assert _values(_params(tmp_path, _cache(IONIQ_COUNTS, False, 50, raw=raw)))["Lat accel factor"] == "4.50 in use"


def test_changed_tune_is_not_stale(tmp_path):
  # The next drive compares the cache's own CarParams (copied into PrevRoute at start) with its new ones.
  values = _values(_params(tmp_path, _cache(IONIQ_COUNTS, False, 50), prev=_cp(lat_accel_factor=3.0)))
  assert "Note" not in values and "Right curves, torque 0.3–0.5" in values


def test_old_cache_version_is_stale(tmp_path):
  values = _values(_params(tmp_path, _cache(IONIQ_COUNTS, False, 58, version=torqued.VERSION - 1)))
  assert values["Note"] == "Cache is from an older torqued: the next drive starts fresh"
  assert not any("curves" in k for k in values)


@pytest.mark.parametrize("cache", [None, b""])
def test_no_section_without_cache(tmp_path, cache):
  assert tls.sections(_params(tmp_path, cache)) == []


def test_garbage_cache_is_unavailable(tmp_path):
  assert _values(_params(tmp_path, b"not a capnp message")) == {"State": "Unavailable"}


def test_bucket_labels():
  labels = [tls.bucket_label(lo, hi) for lo, hi in torqued.STEER_BUCKET_BOUNDS]
  assert labels[0] == "Left curves, torque 0.3–0.5"
  assert labels[3] == "Left curves, torque 0–0.1"
  assert labels[4] == "Right curves, torque 0–0.1"
  assert labels[7] == "Right curves, torque 0.3–0.5"
