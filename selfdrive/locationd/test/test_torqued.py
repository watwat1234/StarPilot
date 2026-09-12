import itertools

from cereal import car
from types import SimpleNamespace
from openpilot.selfdrive.locationd.torqued import TorqueEstimator


def test_cal_percent():
  est = TorqueEstimator(car.CarParams())
  est.starpilot_toggles = SimpleNamespace(use_custom_latAccelFactor=False, use_custom_friction=False)
  msg = est.get_msg()
  assert msg.liveTorqueParameters.calPerc == 0

  for (low, high), min_pts in zip(est.filtered_points.buckets.keys(),
                                  est.filtered_points.buckets_min_points.values(), strict=True):
    for _ in range(int(min_pts)):
      est.filtered_points.add_point((low + high) / 2.0, 0.0)

  # enough bucket points, but not enough total points
  msg = est.get_msg()
  assert msg.liveTorqueParameters.calPerc == (len(est.filtered_points) / est.min_points_total * 100 + 100) / 2

  # add enough points to bucket with most capacity
  key = list(est.filtered_points.buckets)[0]
  for _ in range(est.min_points_total - len(est.filtered_points)):
    est.filtered_points.add_point((key[0] + key[1]) / 2.0, 0.0)

  msg = est.get_msg()
  assert msg.liveTorqueParameters.calPerc == 100


def test_allow_empty_buckets_no_crash_when_all_empty():
  # regression test: is_calculable()'s "len() >= 3" floor must stay in place, or estimate_params()'s
  # SVD raises an uncaught IndexError on the very first get_msg() call with allow_empty_buckets=True
  est = TorqueEstimator(car.CarParams(), allow_empty_buckets=True)
  est.starpilot_toggles = SimpleNamespace(use_custom_latAccelFactor=False, use_custom_friction=False)
  msg = est.get_msg()
  assert msg.liveTorqueParameters.calPerc == 0


def test_allow_empty_buckets_valid_percent_and_validity():
  est = TorqueEstimator(car.CarParams(), allow_empty_buckets=True)
  est.starpilot_toggles = SimpleNamespace(use_custom_latAccelFactor=False, use_custom_friction=False)

  keys = list(est.filtered_points.buckets.keys())
  min_pts = est.filtered_points.buckets_min_points
  excused_bounds = (keys[0], keys[-1])

  excused_key = keys[0]     # left empty throughout, excused via allow_empty_buckets (Hard Left)
  partial_key = keys[1]     # a genuine middle bucket -- must still clear its own threshold to be valid
  full_keys = [k for k in keys if k not in (excused_key, partial_key)]

  for key in full_keys:
    for _ in range(int(min_pts[key])):
      est.filtered_points.add_point((key[0] + key[1]) / 2.0, 0.0)

  half_partial = int(min_pts[partial_key]) // 2
  for _ in range(half_partial):
    est.filtered_points.add_point((partial_key[0] + partial_key[1]) / 2.0, 0.0)

  # Stage 1: enough total/bucket points to be calculable, but partial bucket hasn't cleared its own
  # threshold yet and total points haven't reached min_points_total -> not valid yet.
  assert est.filtered_points.is_calculable()
  assert not est.filtered_points.is_valid()

  total_points_perc = min(len(est.filtered_points) / est.min_points_total * 100, 100)
  bucket_percs = [len(v) / min_pts[k] * 100 for k, v in est.filtered_points.buckets.items() if k not in excused_bounds]
  expected_cal_perc = int((total_points_perc + min(min(bucket_percs), 100)) / 2)

  msg = est.get_msg()
  assert msg.liveTorqueParameters.calPerc == expected_cal_perc
  assert msg.liveTorqueParameters.liveValid is False

  # clear the partial bucket's own threshold
  for _ in range(int(min_pts[partial_key]) - half_partial):
    est.filtered_points.add_point((partial_key[0] + partial_key[1]) / 2.0, 0.0)

  # Top up total points by cycling across the full (already-at-minimum) buckets -- any single bucket's
  # headroom below POINTS_PER_BUCKET (NPQueue's maxlen) isn't enough on its own to absorb the whole
  # remainder, and NPQueue silently caps at maxlen instead of growing past it, which would leave total
  # points short of min_points_total and liveValid permanently False.
  remaining = int(est.min_points_total - len(est.filtered_points))
  topup_keys = itertools.cycle(full_keys)
  for _ in range(remaining):
    topup_key = next(topup_keys)
    est.filtered_points.add_point((topup_key[0] + topup_key[1]) / 2.0, 0.0)

  # Stage 2: all non-excused buckets at/above their minimum, total points met, excused bucket still empty.
  assert len(est.filtered_points.buckets[excused_key]) == 0
  msg = est.get_msg()
  assert msg.liveTorqueParameters.calPerc == 100
  assert msg.liveTorqueParameters.liveValid is True


def test_allow_empty_buckets_partial_fill_stays_excused():
  # regression test for the routes-34/35 transition: an excusable extreme bucket (Hard Left/Hard
  # Right) that goes from empty to a few points *below* its own min_pts must stay excused, not
  # become a new blocker relative to being empty.
  est = TorqueEstimator(car.CarParams(), allow_empty_buckets=True)
  est.starpilot_toggles = SimpleNamespace(use_custom_latAccelFactor=False, use_custom_friction=False)

  keys = list(est.filtered_points.buckets.keys())
  min_pts = est.filtered_points.buckets_min_points
  excused_key = keys[-1]  # Hard Right
  other_keys = keys[:-1]

  for key in other_keys:
    for _ in range(int(min_pts[key])):
      est.filtered_points.add_point((key[0] + key[1]) / 2.0, 0.0)

  # top up total points (cycling across the already-at-minimum buckets) so min_points_total is met too
  remaining = int(est.min_points_total - len(est.filtered_points))
  topup_keys = itertools.cycle(other_keys)
  for _ in range(max(remaining, 0)):
    topup_key = next(topup_keys)
    est.filtered_points.add_point((topup_key[0] + topup_key[1]) / 2.0, 0.0)

  assert est.filtered_points.is_valid()
  empty_msg = est.get_msg()
  assert empty_msg.liveTorqueParameters.liveValid is True
  empty_cal_perc = empty_msg.liveTorqueParameters.calPerc

  # give the excused bucket a few points, well below its own min_pts
  partial_count = max(1, int(min_pts[excused_key]) // 10)
  for _ in range(partial_count):
    est.filtered_points.add_point((excused_key[0] + excused_key[1]) / 2.0, 0.0)

  assert 0 < len(est.filtered_points.buckets[excused_key]) < min_pts[excused_key]
  assert est.filtered_points.is_valid()
  partial_msg = est.get_msg()
  assert partial_msg.liveTorqueParameters.liveValid is True
  assert partial_msg.liveTorqueParameters.calPerc >= empty_cal_perc


def test_allow_empty_buckets_default_path_unchanged():
  est = TorqueEstimator(car.CarParams())
  assert est.filtered_points.allow_empty_buckets is False
  assert not est.filtered_points.is_calculable()

  # even with several points in one bucket, every other bucket still being empty must keep
  # is_calculable() False when allow_empty_buckets is off (the default for every other car)
  key = list(est.filtered_points.buckets.keys())[1]
  for _ in range(3):
    est.filtered_points.add_point((key[0] + key[1]) / 2.0, 0.0)
  assert not est.filtered_points.is_calculable()
