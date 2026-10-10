import numpy as np
import pytest

import openpilot.selfdrive.controls.lib.bolt_low_speed_p as bolt_low_speed_p
import openpilot.selfdrive.controls.lib.latcontrol_torque as latcontrol_torque
from opendbc.car.gm.values import CAR as GM
from opendbc.car.hyundai.values import CAR as HYUNDAI
from openpilot.selfdrive.controls.lib.bolt_low_speed_p import get_bolt_low_speed_p_scale
import openpilot.selfdrive.controls.tests.test_latcontrol as test_latcontrol

GET_FRICTION = latcontrol_torque.get_friction


def _run(car_name, v_ego, monkeypatch, enabled):
  monkeypatch.setattr(bolt_low_speed_p, "ENABLED", enabled)
  friction_inputs = []

  def record_friction(error, *args):
    friction_inputs.append(error)
    return GET_FRICTION(error, *args)

  monkeypatch.setattr(latcontrol_torque, "get_friction", record_friction)
  controller, VM, CS, params, starpilot_toggles = test_latcontrol.TestLatControl._build_torque_controller(car_name)
  CS.vEgo = v_ego
  for _ in range(5):
    output_torque, _, lac_log = controller.update(True, CS, VM, params, False, 0.02, False, 0.2, None, None, starpilot_toggles)
  return output_torque, lac_log, friction_inputs


class TestBoltLowSpeedP:
  @pytest.mark.parametrize("v_ego", [0.0, 1.0, 2.0, 13.0, 20.0, 35.0])
  def test_scale_is_one_outside_band(self, v_ego):
    assert get_bolt_low_speed_p_scale(v_ego) == 1.0

  def test_scale_bounds(self):
    assert min(bolt_low_speed_p.SCALE_V) >= bolt_low_speed_p.MIN_SCALE
    for v_ego in np.linspace(0.0, 40.0, 401):
      assert bolt_low_speed_p.MIN_SCALE <= get_bolt_low_speed_p_scale(v_ego) <= 1.0
    assert get_bolt_low_speed_p_scale(5.0) == pytest.approx(0.4)
    assert get_bolt_low_speed_p_scale(11.0) == pytest.approx(0.7)

  def test_scale_is_one_when_disabled(self, monkeypatch):
    monkeypatch.setattr(bolt_low_speed_p, "ENABLED", False)
    for v_ego in np.linspace(0.0, 40.0, 41):
      assert get_bolt_low_speed_p_scale(v_ego) == 1.0

  def test_bolt_low_speed_scales_p_not_friction(self, monkeypatch):
    _, base_log, base_friction = _run(GM.CHEVROLET_BOLT_ACC_2022_2023, 5.0, monkeypatch, False)
    _, scaled_log, scaled_friction = _run(GM.CHEVROLET_BOLT_ACC_2022_2023, 5.0, monkeypatch, True)

    scale = get_bolt_low_speed_p_scale(5.0)
    assert scale < 1.0
    assert abs(base_log.error) > 0.1
    assert scaled_log.error == pytest.approx(scale * base_log.error)
    assert scaled_log.p == pytest.approx(scale * base_log.p)
    assert scaled_friction == pytest.approx(base_friction)

  @pytest.mark.parametrize(("car_name", "v_ego"), [
    (GM.CHEVROLET_BOLT_ACC_2022_2023, 20.0),
    (HYUNDAI.HYUNDAI_SONATA_HYBRID, 5.0),
  ])
  def test_unchanged_outside_band_or_other_cars(self, monkeypatch, car_name, v_ego):
    base_torque, base_log, _ = _run(car_name, v_ego, monkeypatch, False)
    torque, lac_log, _ = _run(car_name, v_ego, monkeypatch, True)

    assert abs(base_log.error) > 0.01
    assert torque == base_torque
    assert lac_log.error == base_log.error
    assert lac_log.p == base_log.p
