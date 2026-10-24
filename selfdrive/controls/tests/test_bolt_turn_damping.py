import math
from collections import deque
from types import SimpleNamespace

import numpy as np
import pytest

from cereal import car, custom, log
from opendbc.car.car_helpers import interfaces
from opendbc.car.gm.values import CAR as GM
from opendbc.car.hyundai.values import CAR as HYUNDAI
from opendbc.car.vehicle_model import VehicleModel
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.lib.bolt_turn_damping import (
  BOLT_TURN_DAMPING_MAX_KD,
  BOLT_TURN_DAMPING_MAX_ERROR_RATE,
  BoltTurnDamping,
)
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque

SLOW = 15 * CV.MPH_TO_MS
FAST = 40 * CV.MPH_TO_MS


def settled(percent, v_ego=SLOW, setpoint=2.0, measurement=1.8, frames=200):
  damping = BoltTurnDamping(DT_CTRL)
  kd = 0.0
  for _ in range(frames):
    kd, _ = damping.update(percent, v_ego, setpoint, measurement, False)
  return kd


class TestBoltTurnDampingGain:
  @pytest.mark.parametrize("percent, kd", [(0, 0.0), (10, 0.1), (30, 0.3), (60, 0.6), (100, 1.0)])
  def test_percent_maps_linearly_to_gain_in_a_slow_turn(self, percent, kd):
    # 30 / 60 / 100% match the old Low / Medium / High levels
    assert settled(percent) == pytest.approx(kd)
    assert BOLT_TURN_DAMPING_MAX_KD == 1.0

  def test_off_above_30_mph(self):
    assert settled(100, v_ego=30 * CV.MPH_TO_MS) == 0.0
    assert settled(100, v_ego=FAST) == 0.0

  def test_off_near_straight(self):
    assert settled(100, setpoint=0.3, measurement=-0.2) == 0.0
    assert settled(100, setpoint=0.0, measurement=0.0) == 0.0

  def test_turn_weight_uses_larger_of_setpoint_and_measurement(self):
    assert settled(100, setpoint=0.1, measurement=-0.8) == pytest.approx(1.0)
    assert settled(100, setpoint=-0.55, measurement=0.0) == pytest.approx(0.5)

  def test_speed_fade_is_monotone(self):
    gains = [settled(100, v_ego=v * CV.MPH_TO_MS) for v in range(35)]
    assert all(g == pytest.approx(1.0) for g in gains[:21])
    assert all(a >= b for a, b in zip(gains, gains[1:], strict=False))
    assert gains[25] == pytest.approx(0.5)

  def test_off_at_once_while_pressed_then_fades_back(self):
    damping = BoltTurnDamping(DT_CTRL)
    for _ in range(200):
      damping.update(100, SLOW, 2.0, 1.8, False)
    # the driver turns the wheel: no D against them on any pressed frame
    measurement = 1.8
    for _ in range(30):
      measurement += 10.0 * DT_CTRL
      assert damping.update(100, SLOW, 2.0, measurement, True)[0] == 0.0
    after_release = [damping.update(100, SLOW, 2.0, measurement, False)[0] for _ in range(150)]
    assert after_release[0] == pytest.approx(0.01)
    assert after_release[-1] == pytest.approx(1.0)

  def test_changing_the_setting_ramps(self):
    damping = BoltTurnDamping(DT_CTRL)
    gains = [damping.update(100, SLOW, 2.0, 1.8, False)[0] for _ in range(150)]
    gains += [damping.update(30, SLOW, 2.0, 1.8, False)[0] for _ in range(150)]
    gains += [damping.update(0, SLOW, 2.0, 1.8, False)[0] for _ in range(150)]
    steps = np.abs(np.diff([0.0] + gains))
    assert steps.max() <= 0.01 + 1e-9
    assert gains[149] == pytest.approx(1.0)
    assert gains[299] == pytest.approx(0.3)
    assert gains[-1] == 0.0

  def test_percent_out_of_range_is_clamped(self):
    assert settled(150) == pytest.approx(1.0)
    assert settled(-10) == 0.0

  def test_reset_starts_from_zero_gain_and_no_history(self):
    damping = BoltTurnDamping(DT_CTRL)
    for _ in range(200):
      damping.update(100, SLOW, 2.0, 1.8, False)
    damping.reset()
    kd, error_rate = damping.update(100, SLOW, -2.0, -1.8, False)
    assert kd == pytest.approx(0.01)
    assert error_rate == 0.0  # the jump from +2 to -2 across the reset is not a rate


def error_rates(setpoints, measurements):
  damping = BoltTurnDamping(DT_CTRL)
  return np.array([damping.update(100, SLOW, sp, meas, False)[1] for sp, meas in zip(setpoints, measurements, strict=True)])


def ramp(start, rate, frames):
  return [start + rate * DT_CTRL * i for i in range(frames)]


class TestBoltTurnErrorRate:
  def test_zero_every_frame_when_wheel_tracks_setpoint(self):
    # turn in, hold, turn out: the same motion on both sides gives no error rate at any frame (matched filters)
    path = ramp(0.0, 2.0, 100) + [2.0] * 50 + ramp(2.0, -2.0, 100)
    rates = error_rates(path, [p - 0.3 for p in path])
    assert np.abs(rates).max() < 1e-9

  def test_no_ripple_from_20hz_setpoint_steps(self):
    # the model updates the setpoint every 5 frames; the wheel moves smoothly at the same average rate
    smooth = ramp(1.0, 1.0, 300)
    stepped = [smooth[i - i % 5] for i in range(300)]
    rates = error_rates(stepped, smooth)
    assert np.abs(rates[100:]).max() < 0.02

  def test_sign_opposes_wheel_motion_away_from_setpoint(self):
    assert error_rates([1.0] * 200, ramp(1.0, 2.0, 200))[-1] == pytest.approx(-2.0, abs=1e-3)
    assert error_rates([1.0] * 200, ramp(1.0, -2.0, 200))[-1] == pytest.approx(2.0, abs=1e-3)

  def test_capped(self):
    # wheel swinging fast, and a fast planned move the wheel lags behind: both capped
    assert error_rates([1.0] * 200, ramp(1.0, 20.0, 200))[-1] == -BOLT_TURN_DAMPING_MAX_ERROR_RATE
    assert error_rates(ramp(1.0, 5.0, 200), [1.0] * 200)[-1] == BOLT_TURN_DAMPING_MAX_ERROR_RATE


def build(car_name, v_ego, percent):
  CarInterface = interfaces[car_name]
  CP = CarInterface.get_non_essential_params(car_name)
  CI = CarInterface(CP, custom.StarPilotCarParams.new_message())
  controller = LatControlTorque(CP.as_reader(), CI, DT_CTRL)
  VM = VehicleModel(CP)
  CS = car.CarState.new_message()
  CS.vEgo = v_ego
  CS.steeringPressed = False
  params = log.LiveParametersData.new_message()
  params.steerRatio = CP.steerRatio
  params.stiffnessFactor = 1.0
  params.roll = 0.0
  params.angleOffsetDeg = 0.0
  return controller, VM, CS, params, SimpleNamespace(bolt_turn_damping=percent)


def run_turn(car_name, v_ego, percent, angle_rate_deg, frames=150, target_lat_accel=1.5, switch=None):
  """Hold a steady planned turn while the wheel turns further at angle_rate_deg per second.

  switch = (frame, percent) changes the setting mid-run."""
  controller, VM, CS, params, toggles = build(car_name, v_ego, percent)
  # measurement = -curvature(angle) * v^2, linear in angle with no roll
  angle = target_lat_accel / (-VM.calc_curvature(math.radians(1.0), v_ego, 0.0) * v_ego ** 2)
  desired_curvature = target_lat_accel / v_ego ** 2
  controller.curvature_request_buffer = deque([desired_curvature] * controller.request_buffer_len,
                                              maxlen=controller.request_buffer_len)
  outputs, logs = [], []
  direction = math.copysign(1.0, angle)
  for frame in range(frames):
    if switch is not None and frame == switch[0]:
      toggles.bolt_turn_damping = switch[1]
    CS.steeringAngleDeg = angle
    output, _, lac_log = controller.update(True, CS, VM, params, False, desired_curvature, False, 0.2, None, None,
                                           toggles)
    outputs.append(output)
    logs.append(lac_log)
    angle += direction * angle_rate_deg * DT_CTRL
  return controller, outputs, logs


class TestBoltTurnDampingInController:
  def test_damping_opposes_wheel_moving_away_from_target(self):
    _, _, logs = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, 100, angle_rate_deg=40.0)
    # the wheel turns further into the turn than planned: d pulls back (opposite sign to the turn)
    assert logs[-1].actualLateralAccel > 1.5
    assert logs[-1].d < -0.5

  def test_no_damping_when_wheel_follows_plan(self):
    _, _, logs = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, 100, angle_rate_deg=0.0)
    assert abs(logs[-1].d) < 1e-3

  def test_no_damping_from_speed_change_with_steady_wheel(self):
    # pulling away through a tight turn on the planned curvature: lat accel = curvature * v^2 rises with speed on
    # both sides of the error. The wheel follows the model's understeer so the car stays exactly on plan.
    # Gain pre-ramped to full so every frame counts.
    controller, VM, CS, params, toggles = build(GM.CHEVROLET_BOLT_ACC_2022_2023, 4.0, 100)
    controller.bolt_turn_damping.kd = BOLT_TURN_DAMPING_MAX_KD
    desired_curvature = 0.08
    controller.curvature_request_buffer = deque([desired_curvature] * controller.request_buffer_len,
                                                maxlen=controller.request_buffer_len)
    d = []
    for frame in range(200):
      CS.vEgo = 4.0 + 1.5 * DT_CTRL * frame
      CS.steeringAngleDeg = math.degrees(desired_curvature / -VM.calc_curvature(1.0, CS.vEgo, 0.0))
      d.append(controller.update(True, CS, VM, params, False, desired_curvature, False, 0.2, None, None, toggles)[2].d)
    assert max(abs(x) for x in d) < 0.01

  def test_disengage_resets_damping_state(self):
    controller, _, _ = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, 100, angle_rate_deg=40.0)
    assert controller.bolt_turn_damping.kd > 0.0
    _, VM, CS, params, toggles = build(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, 100)
    controller.update(False, CS, VM, params, False, 0.0, False, 0.2, None, None, toggles)
    assert controller.bolt_turn_damping.kd == 0.0
    assert len(controller.bolt_turn_damping.history) == 0

  def test_setting_off_has_no_d(self):
    _, _, logs = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, 0, angle_rate_deg=40.0)
    assert all(lac_log.d == 0.0 for lac_log in logs)

  @pytest.mark.parametrize("start, switch_to", [(0, 100), (100, 0)])
  def test_setting_change_moves_d_gradually(self, start, switch_to):
    _, _, logs = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, start, angle_rate_deg=40.0, frames=300,
                          switch=(150, switch_to))
    d = np.array([lac_log.d for lac_log in logs])
    assert abs(d[-1] - d[149]) > 0.5  # the switch did take effect
    # one frame's gain step (0.01) times the capped error rate; the rate itself settles before the switch
    assert np.abs(np.diff(d[140:])).max() < 0.01 * BOLT_TURN_DAMPING_MAX_ERROR_RATE + 0.005

  @pytest.mark.parametrize("car_name, v_ego", [
    (HYUNDAI.HYUNDAI_SONATA, SLOW),
    (GM.CHEVROLET_BOLT_ACC_2022_2023, FAST),
  ])
  def test_unchanged_where_not_gated_in(self, car_name, v_ego):
    _, off_outputs, off_logs = run_turn(car_name, v_ego, 0, angle_rate_deg=40.0)
    _, on_outputs, on_logs = run_turn(car_name, v_ego, 100, angle_rate_deg=40.0)
    assert on_outputs == off_outputs
    assert [x.d for x in on_logs] == [x.d for x in off_logs]
