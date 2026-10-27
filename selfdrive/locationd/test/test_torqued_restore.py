from types import SimpleNamespace

import numpy as np
import pytest

import cereal.messaging as messaging
from cereal import car
from openpilot.common.params import Params
from openpilot.selfdrive.locationd import torqued
from openpilot.selfdrive.locationd.torqued import TorqueEstimator, MIN_FILTER_DECAY, MIN_POINTS_TOTAL_QLOG, STEER_BUCKET_BOUNDS, VERSION

OFFLINE_FACTOR = 4.5
OFFLINE_FRICTION = 0.09
LEARNED = {'latAccelFactor': 5.3, 'latAccelOffset': -0.05, 'frictionCoefficient': 0.08}


def make_cp(lat_accel_factor=OFFLINE_FACTOR):
  CP = car.CarParams.new_message()
  CP.carFingerprint = "HYUNDAI_IONIQ_6"
  CP.brand = "hyundai"
  CP.lateralTuning.init('torque')
  CP.lateralTuning.torque.latAccelFactor = lat_accel_factor
  CP.lateralTuning.torque.friction = OFFLINE_FRICTION
  return CP


def make_estimator(CP, decimated):
  est = TorqueEstimator(CP, decimated=decimated)
  est.starpilot_toggles = SimpleNamespace(use_custom_latAccelFactor=False, use_custom_friction=False)
  return est


def filtered(est):
  return {k: est.filtered_params[k].x for k in LEARNED}


def bucket_points(counts):
  return [[low + (high - low) * (i + 0.5) / n, 0.0] for (low, high), n in zip(STEER_BUCKET_BOUNDS, counts, strict=True)
          for i in range(n)]


def write_cache(CP, live_valid, decay, values=LEARNED, n_points=40, points=None):
  msg = messaging.new_message('liveTorqueParameters')
  ltp = msg.liveTorqueParameters
  ltp.version = VERSION
  ltp.liveValid = live_valid
  ltp.decay = decay
  ltp.latAccelFactorFiltered = values['latAccelFactor']
  ltp.latAccelOffsetFiltered = values['latAccelOffset']
  ltp.frictionCoefficientFiltered = values['frictionCoefficient']
  ltp.points = [[0.15, 0.75]] * n_points if points is None else points
  params = Params()
  params.put("CarParamsPrevRoute", CP.to_bytes())
  params.put("LiveTorqueParameters", msg.to_bytes())


def save_cache(est):
  Params().put("LiveTorqueParameters", est.get_msg(with_points=True).to_bytes())


def fill_buckets(est, per_bucket, slope=5.3):
  # lat accel = slope * torque with a +-0.1 hysteresis band, so the fit is finite
  for low, high in STEER_BUCKET_BOUNDS:
    for i, x in enumerate(np.linspace(low, high, per_bucket, endpoint=False)):
      est.filtered_points.add_point(x, slope * x + (0.1 if i % 2 else -0.1))


def assert_values(est, expected):
  for k, v in expected.items():
    assert abs(est.filtered_params[k].x - v) < 1e-4, (k, est.filtered_params[k].x, v)


def test_invalid_cache_with_learned_decay_restores():
  CP = make_cp()
  write_cache(CP, live_valid=False, decay=179.0)
  est = make_estimator(CP, decimated=True)
  assert_values(est, LEARNED)
  assert est.decay == 179.0
  assert len(est.filtered_points) == 40


def test_invalid_cache_with_min_decay_starts_from_offline():
  CP = make_cp()
  write_cache(CP, live_valid=False, decay=MIN_FILTER_DECAY)
  est = make_estimator(CP, decimated=True)
  assert_values(est, {'latAccelFactor': OFFLINE_FACTOR, 'latAccelOffset': 0.0, 'frictionCoefficient': OFFLINE_FRICTION})
  assert len(est.filtered_points) == 40


def test_valid_cache_restores():
  CP = make_cp()
  write_cache(CP, live_valid=True, decay=250.0)
  est = make_estimator(CP, decimated=False)
  assert_values(est, LEARNED)
  assert est.decay == 250.0


def test_restore_key_mismatch_starts_from_offline():
  write_cache(make_cp(lat_accel_factor=4.0), live_valid=True, decay=250.0)
  est = make_estimator(make_cp(), decimated=True)
  assert_values(est, {'latAccelFactor': OFFLINE_FACTOR, 'latAccelOffset': 0.0, 'frictionCoefficient': OFFLINE_FRICTION})
  assert est.decay == MIN_FILTER_DECAY
  assert len(est.filtered_points) == 0


def test_decimated_normal_decimated_keeps_learned_values():
  CP = make_cp()
  Params().put("CarParamsPrevRoute", CP.to_bytes())

  # drive 1, decimated: enough points for the decimated minimums, filters learn
  est = make_estimator(CP, decimated=True)
  fill_buckets(est, per_bucket=80)
  for _ in range(200):
    msg = est.get_msg()
  assert msg.liveTorqueParameters.liveValid
  save_cache(est)  # its get_msg takes one more filter step, so read the values after it
  learned = filtered(est)
  assert learned['latAccelFactor'] > OFFLINE_FACTOR + 0.01  # filters moved; the drive-3 check is the real test

  # drive 2, normal: below the normal minimums, holds the values and caches liveValid False
  est = make_estimator(CP, decimated=False)
  assert_values(est, learned)
  for _ in range(20):
    msg = est.get_msg()
  assert not msg.liveTorqueParameters.liveValid
  assert_values(est, learned)
  save_cache(est)

  # drive 3, decimated: starts at the held values, not the offline ones
  est = make_estimator(CP, decimated=True)
  assert_values(est, learned)


class StopLoop(Exception):
  pass


class FakeSubMaster:
  def __init__(self, *args, **kwargs):
    pass

  def extend(self, services):
    return self

  def update(self):
    raise StopLoop


def run_main_mode(monkeypatch, live_valid_toggle):
  # main up to its first sm.update(), on the cache's CarParams; returns the mode of the estimator it learns with
  built = []

  class RecordingEstimator(TorqueEstimator):
    def __init__(self, *args, **kwargs):
      super().__init__(*args, **kwargs)
      built.append(self)

  Params().put("CarParams", Params().get("CarParamsPrevRoute"))
  monkeypatch.setattr(torqued, "TorqueEstimator", RecordingEstimator)
  monkeypatch.setattr(torqued, "config_realtime_process", lambda *args: None)
  monkeypatch.setattr(torqued, "get_starpilot_toggles", lambda *args: SimpleNamespace(
    liveValid=live_valid_toggle, use_custom_latAccelFactor=False, use_custom_friction=False))
  monkeypatch.setattr(messaging, "PubMaster", lambda *args, **kwargs: None)
  monkeypatch.setattr(messaging, "SubMaster", FakeSubMaster)
  with pytest.raises(StopLoop):
    torqued.main()
  return "decimated" if built[-1].min_points_total == MIN_POINTS_TOTAL_QLOG else "normal"


# Normal-mode minimums are [100, 300, 500, 500, 500, 500, 300, 100]; decimated ones are a tenth.
SHORT_NORMAL = [100, 40, 1500, 1500, 1500, 1500, 300, 100]  # Ioniq-like: left 0.2-0.3 short
FULL_NORMAL = [100, 300, 1500, 1500, 1500, 1500, 300, 100]


def test_main_valid_cache_with_short_normal_buckets_stays_decimated(monkeypatch):
  # The last drive's liveValid (a valid decimated drive) no longer switches the next drive to normal.
  CP = make_cp()
  write_cache(CP, live_valid=True, decay=250.0, points=bucket_points(SHORT_NORMAL))
  assert run_main_mode(monkeypatch, live_valid_toggle=True) == "decimated"


def test_main_full_normal_buckets_runs_normal(monkeypatch):
  CP = make_cp()
  write_cache(CP, live_valid=False, decay=250.0, points=bucket_points(FULL_NORMAL))
  assert run_main_mode(monkeypatch, live_valid_toggle=False) == "normal"


def test_main_without_cache_runs_decimated(monkeypatch):
  CP = make_cp()
  Params().put("CarParamsPrevRoute", CP.to_bytes())
  Params().remove("LiveTorqueParameters")
  assert run_main_mode(monkeypatch, live_valid_toggle=False) == "decimated"
