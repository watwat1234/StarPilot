from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.controls.lib.lane_centering import LaneCenteringController, get_lane_centering_visual_direction


_V_EGO = 20.0
_XS = np.linspace(0.0, 50.0, 52)


def _path(y, y_std=0.1):
  return SimpleNamespace(
    x=_XS.copy(),
    y=np.full_like(_XS, float(y)),
    yStd=np.full_like(_XS, float(y_std)),
  )


def _model(left=-1.8, right=1.8, model_y=0.0, lane_prob=0.9, lane_std=0.1, path_std=0.1, lane_change=0):
  return SimpleNamespace(
    laneLines=[_path(0.0), _path(left), _path(right), _path(0.0)],
    laneLineProbs=[0.0, lane_prob, lane_prob, 0.0],
    laneLineStds=[0.0, lane_std, lane_std, 0.0],
    position=_path(model_y, path_std),
    meta=SimpleNamespace(laneChangeState=lane_change),
  )


def _update(controller, model, *, offset=0.0, authority=1.0, enabled=True, active=True, valid=True, speed=_V_EGO,
            pause_on_signal=False, turn_signal_active=False, driver_override=False, **kwargs):
  return controller.update(0.0, model, speed, enabled, offset, authority, active, valid,
                           pause_on_signal, turn_signal_active, driver_override, **kwargs)


def _converge(model, *, offset=0.0, authority=1.0, **kwargs):
  controller = LaneCenteringController()
  output = 0.0
  for _ in range(300):
    output = _update(controller, model, offset=offset, authority=authority, **kwargs)
  return controller, output


@pytest.mark.parametrize(
  "kwargs",
  [
    {"enabled": False},
    {"active": False},
    {"valid": False},
    {"speed": 4.9},
  ],
)
def test_hard_gates_are_noop(kwargs):
  assert _update(LaneCenteringController(), _model(left=-1.5, right=2.1), **kwargs) == 0.0


def test_lane_change_is_noop():
  assert _update(LaneCenteringController(), _model(left=-1.5, right=2.1, lane_change=1)) == 0.0


def test_turn_signal_fades_lane_centering_correction():
  model = _model(left=-1.5, right=2.1)
  controller, centered = _converge(model, authority=0.0)
  fading = _update(controller, model, authority=0.0, pause_on_signal=True, turn_signal_active=True)
  assert 0.0 < fading < centered

  for _ in range(300):
    fading = _update(controller, model, authority=0.0, pause_on_signal=True, turn_signal_active=True)
  assert abs(fading) < 1e-6


def test_turn_signal_pause_can_be_disabled():
  model = _model(left=-1.5, right=2.1)
  _, output = _converge(model, authority=0.0)
  controller, _ = _converge(model, authority=0.0)
  signaled = _update(controller, model, authority=0.0, turn_signal_active=True)
  assert signaled == pytest.approx(output, abs=1e-7)


def test_driver_override_clears_filtered_correction():
  model = _model(left=-1.5, right=2.1)
  controller, centered = _converge(model, authority=0.0)
  assert centered > 0.0

  overridden = _update(controller, model, authority=0.0, driver_override=True)
  assert overridden == 0.0

  reacquired = _update(controller, model, authority=0.0)
  assert 0.0 < reacquired < centered


@pytest.mark.parametrize(
  "field,value",
  [
    ("prob", np.nan),
    ("prob", 1.1),
    ("std", np.nan),
    ("std", -0.1),
  ],
)
def test_invalid_lane_confidence_is_rejected(field, value):
  model = _model(left=-1.5, right=2.1)
  values = model.laneLineProbs if field == "prob" else model.laneLineStds
  values[1] = value
  assert _update(LaneCenteringController(), model) == 0.0


def test_input_must_cover_lookahead():
  model = _model(left=-1.5, right=2.1)
  model.laneLines[1].x = model.laneLines[1].x[:10]
  model.laneLines[1].y = model.laneLines[1].y[:10]
  assert _update(LaneCenteringController(), model) == 0.0


def test_lane_center_error_steers_toward_center():
  _, right = _converge(_model(left=-1.5, right=2.1), authority=0.0)
  _, left = _converge(_model(left=-2.1, right=1.5), authority=0.0)
  assert right > 0.0
  assert left < 0.0


def test_small_center_error_does_not_chatter():
  _, output = _converge(_model(left=-1.75, right=1.85), authority=0.0)
  assert output == 0.0


def test_offset_direction():
  _, right = _converge(_model(), offset=0.2, authority=0.0)
  _, left = _converge(_model(), offset=-0.2, authority=0.0)
  assert right > 0.0
  assert left < 0.0


def test_offset_is_reduced_in_narrow_lane():
  narrow = _model(left=-1.3, right=1.3)
  _, at_safe_limit = _converge(narrow, offset=0.2, authority=0.0)
  _, above_safe_limit = _converge(narrow, offset=0.3, authority=0.0)
  assert np.isclose(at_safe_limit, above_safe_limit)


@pytest.mark.parametrize("direction", [-1.0, 1.0])
def test_confident_e2e_path_retains_bounded_lane_correction(direction):
  model = _model(left=-2.4, right=2.4, model_y=direction * 0.6, path_std=0.1)
  _, lane_authority = _converge(model, authority=0.0)
  _, e2e_authority = _converge(model, authority=1.0)
  assert lane_authority * direction < 0.0
  assert e2e_authority == pytest.approx(0.2 * lane_authority)


@pytest.mark.parametrize("direction", [-1.0, 1.0])
def test_e2e_retains_more_lane_correction_near_boundary(direction):
  model = _model(model_y=direction * 0.8, path_std=0.1)
  _, lane_authority = _converge(model, authority=0.0)
  _, e2e_authority = _converge(model, authority=1.0)
  assert e2e_authority == pytest.approx(0.5 * lane_authority)


def test_e2e_boundary_authority_blends_continuously():
  fractions = []
  for clearance in np.linspace(1.55, 1.05, 101):
    model = _model(left=-2.4, right=2.4, model_y=-2.4 + clearance)
    lane_valid, lane_authority = LaneCenteringController._raw_correction(model, _V_EGO, 0.0, 0.0)
    e2e_valid, e2e_authority = LaneCenteringController._raw_correction(model, _V_EGO, 0.0, 1.0)
    assert lane_valid and e2e_valid
    fractions.append(e2e_authority / lane_authority)
  assert fractions[0] == pytest.approx(0.2)
  assert fractions[-1] == pytest.approx(0.5)
  assert np.all(np.diff(fractions) >= -1e-9)
  assert np.max(np.diff(fractions)) < 0.004


@pytest.mark.parametrize("line", [1, 2])
def test_e2e_boundary_correction_requires_both_lane_lines(line):
  model = _model(model_y=-0.8)
  assert _update(LaneCenteringController(), model) > 0.0
  model.laneLineProbs[line] = 0.59
  assert _update(LaneCenteringController(), model) == 0.0


@pytest.mark.parametrize("direction", [-1.0, 1.0])
def test_e2e_boundary_correction_remains_capped_and_yields_to_driver(direction):
  model = _model(model_y=direction * 2.0)
  controller, output = _converge(model)
  assert output * direction < 0.0
  assert abs(output) <= 0.004 * 0.30
  assert _update(controller, model, driver_override=True) == 0.0


def test_uncertain_e2e_path_does_not_break_in():
  model = _model(left=-1.0, right=2.6, model_y=0.0, path_std=0.6)
  _, lane_only = _converge(model, authority=0.0)
  _, output = _converge(model, authority=1.0)
  assert output == pytest.approx(lane_only)


def test_e2e_authority_blends_lane_correction():
  model = _model(left=-1.2, right=2.4, model_y=0.0, path_std=0.1)
  _, lane_only = _converge(model, authority=0.0)
  _, blended = _converge(model, authority=0.5)
  _, e2e = _converge(model, authority=1.0)
  assert lane_only > blended > e2e >= 0.0


def test_confident_e2e_authority_starts_before_large_offset():
  model = _model(left=-1.7, right=2.1, model_y=0.0, path_std=0.1)
  _, lane_only = _converge(model, authority=0.0)
  _, e2e = _converge(model, authority=1.0)
  assert lane_only > e2e > 0.0


def test_confidence_loss_drops_filtered_correction():
  controller, output = _converge(_model(left=-1.5, right=2.1), authority=0.0)
  assert output > 0.0
  fading = _update(controller, _model(left=-1.5, right=2.1, lane_prob=0.2), authority=0.0)
  assert 0.0 < fading < output

  for _ in range(300):
    fading = _update(controller, _model(left=-1.5, right=2.1, lane_prob=0.2), authority=0.0)
  assert abs(fading) < 1e-6


def test_correction_is_smoothed_and_capped():
  controller = LaneCenteringController()
  model = _model(left=0.0, right=3.0, path_std=0.6)
  first = _update(controller, model, authority=0.0)
  _, steady = _converge(model, authority=0.0)
  assert 0.0 < first < steady
  assert np.isclose(steady, 0.004 * 0.30, atol=1e-6)


def test_default_strength_matches_unity_strength():
  model = _model(left=-1.5, right=2.1)
  _, default = _converge(model, authority=0.0)
  _, unity = _converge(model, authority=0.0, strength=1.0)
  assert default > 0.0
  assert unity == default


@pytest.mark.parametrize("strength", [0.5, 2.0, 3.0])
def test_strength_scales_unsaturated_correction(strength):
  model = _model(left=-1.5, right=2.1)
  _, base = _converge(model, authority=0.0)
  _, scaled = _converge(model, authority=0.0, strength=strength)
  assert base * 3.0 < 0.004 * 0.30
  assert scaled == pytest.approx(base * strength, rel=1e-6)


@pytest.mark.parametrize("strength", [2.0, 3.0])
def test_strength_does_not_raise_correction_cap(strength):
  _, steady = _converge(_model(left=0.0, right=3.0, path_std=0.6), authority=0.0, strength=strength)
  assert np.isclose(steady, 0.004 * 0.30, atol=1e-6)


@pytest.mark.parametrize("strength,clamped", [(0.1, 0.5), (-2.0, 0.5), (10.0, 3.0)])
def test_out_of_range_strength_is_clamped(strength, clamped):
  model = _model(left=-1.5, right=2.1)
  _, expected = _converge(model, authority=0.0, strength=clamped)
  _, output = _converge(model, authority=0.0, strength=strength)
  assert output == pytest.approx(expected, rel=1e-9)


@pytest.mark.parametrize("strength", [float("nan"), float("inf"), None, "x"])
def test_invalid_strength_resets_to_model_curvature(strength):
  model = _model(left=-1.5, right=2.1)
  controller, output = _converge(model, authority=0.0)
  assert output > 0.0
  assert _update(controller, model, authority=0.0, strength=strength) == 0.0
  assert _update(controller, model, authority=0.0) < output


def test_visual_direction_matches_curvature_sign():
  # Positive curvature is right in this tree's convention.
  assert get_lane_centering_visual_direction(_model(left=-1.5, right=2.1), _V_EGO, 0.0, 0.0, True, True) == 1
  assert get_lane_centering_visual_direction(_model(left=-2.1, right=1.5), _V_EGO, 0.0, 0.0, True, True) == -1


def test_visual_direction_requires_both_primary_lane_lines():
  model = _model(left=-1.5, right=2.1)
  model.laneLineProbs[2] = 0.2
  assert get_lane_centering_visual_direction(model, _V_EGO, 0.0, 0.0, True, True) == 0


def test_visual_direction_uses_filtered_correction_in_deadband():
  model = _model()
  assert get_lane_centering_visual_direction(model, _V_EGO, 0.0, 0.0, True, True, applied_correction=0.001) == 1


def test_visual_direction_follows_applied_correction():
  model = _model(left=-1.5, right=2.1)
  assert get_lane_centering_visual_direction(model, _V_EGO, 0.0, 0.0, True, True, applied_correction=-0.001) == -1


def _centered_at(error, **kwargs):
  # Lanes +-1.8 m, model path offset so the center error (target - path) equals `error`.
  return _model(model_y=-error, **kwargs)


def _run(controller, model, steps, *, speed=_V_EGO, curvature=0.0, smoothing=True, **kwargs):
  out = []
  for _ in range(steps):
    out.append(controller.update(curvature, model, speed, True, 0.0, 0.0, True, True,
                                 smoothing=smoothing, **kwargs) - curvature)
  return np.array(out)


@pytest.mark.parametrize("smoothing", [False, True])
def test_smoothing_steady_error_converges_to_unsmoothed_value(smoothing):
  steady = _run(LaneCenteringController(), _centered_at(0.12), 6000, smoothing=smoothing)[-1]
  unsmoothed = _run(LaneCenteringController(), _centered_at(0.12), 6000, smoothing=False)[-1]
  assert unsmoothed > 0.0
  assert steady == pytest.approx(unsmoothed, rel=1e-6)


def test_smoothing_damps_sway_at_deadband_edge():
  # ~30 mph, error swinging 0.08 +- 0.05 m with a 3.3 s period (route 000000dd seg 4-5).
  def sway(smoothing):
    controller = LaneCenteringController()
    out = []
    for i in range(3000):
      model = _centered_at(0.08 + 0.05 * np.sin(2 * np.pi * i * 0.01 / 3.3))
      out.append(controller.update(0.0, model, 13.4, True, 0.0, 0.0, True, True, smoothing=smoothing))
    return np.ptp(out[-660:])

  assert sway(False) > 0.0
  assert sway(True) < 0.4 * sway(False)


@pytest.mark.parametrize("error,curvature", [(0.35, 0.0), (0.12, 0.003)])
def test_smoothing_is_bypassed_for_large_errors_and_turns(error, curvature):
  # 0.35 m is past the 0.30 m backstop; 0.003 1/m at 20 m/s is 1.2 m/s^2 of lateral accel.
  model = _centered_at(error)
  smoothed = _run(LaneCenteringController(), model, 100, curvature=curvature)
  unsmoothed = _run(LaneCenteringController(), model, 100, curvature=curvature, smoothing=False)
  assert unsmoothed[-1] > 0.0
  np.testing.assert_allclose(smoothed, unsmoothed, rtol=1e-9, atol=1e-12)


def test_smoothing_rises_slower_below_backstop():
  model = _centered_at(0.12)
  smoothed = _run(LaneCenteringController(), model, 100)
  unsmoothed = _run(LaneCenteringController(), model, 100, smoothing=False)
  assert 0.0 <= smoothed[-1] < 0.5 * unsmoothed[-1]


def test_smoothed_error_is_not_rescaled_by_braking():
  # Hold a highway error, then brake to 8 m/s with the car centered: the shorter lookahead must not turn the held
  # error into a larger correction.
  controller = LaneCenteringController()
  highway = _run(controller, _centered_at(0.14), 3000, speed=30.0)[-1]
  braking = _run(controller, _centered_at(0.0), 300, speed=8.0)
  assert highway > 0.0
  assert braking.max() <= highway


@pytest.mark.parametrize("kwargs", [{"driver_override": True}, {"smoothing": False}])
def test_override_and_toggle_off_clear_smoothed_error(kwargs):
  controller = LaneCenteringController()
  _run(controller, _centered_at(0.12), 1000)
  assert controller._smoothed_error > 0.0
  _run(controller, _centered_at(0.12), 1, **kwargs)
  assert controller._smoothed_error == 0.0


def test_lane_change_clears_smoothed_error():
  controller = LaneCenteringController()
  _run(controller, _centered_at(0.12), 1000)
  _run(controller, _centered_at(0.12, lane_change=1), 1)
  assert controller._smoothed_error == 0.0


def test_smoothing_keeps_strength_scaling():
  model = _centered_at(0.12)
  base = _run(LaneCenteringController(), model, 6000)[-1]
  scaled = _run(LaneCenteringController(), model, 6000, strength=2.0)[-1]
  assert scaled == pytest.approx(base * 2.0, rel=1e-6)
