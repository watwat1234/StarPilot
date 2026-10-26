from types import SimpleNamespace

import numpy as np

import cereal.messaging as messaging
from cereal import car
from openpilot.common.params import Params
from openpilot.selfdrive.locationd.torqued import TorqueEstimator, MIN_FILTER_DECAY, STEER_BUCKET_BOUNDS, VERSION

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


def write_cache(CP, live_valid, decay, values=LEARNED, n_points=40):
  msg = messaging.new_message('liveTorqueParameters')
  ltp = msg.liveTorqueParameters
  ltp.version = VERSION
  ltp.liveValid = live_valid
  ltp.decay = decay
  ltp.latAccelFactorFiltered = values['latAccelFactor']
  ltp.latAccelOffsetFiltered = values['latAccelOffset']
  ltp.frictionCoefficientFiltered = values['frictionCoefficient']
  ltp.points = [[0.15, 0.75]] * n_points
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
