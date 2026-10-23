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
  BOLT_TURN_DAMPING_KD,
  BOLT_TURN_DAMPING_MAX_RATE,
  BoltTurnDamping,
)
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque

SLOW = 15 * CV.MPH_TO_MS
FAST = 40 * CV.MPH_TO_MS


def settled(level, v_ego=SLOW, setpoint=2.0, measurement=1.8, frames=200):
  damping = BoltTurnDamping(DT_CTRL)
  kd = 0.0
  for _ in range(frames):
    kd, _ = damping.update(level, v_ego, setpoint, measurement, 0.0, False)
  return kd


class TestBoltTurnDampingGain:
  @pytest.mark.parametrize("level", range(4))
  def test_levels_map_to_gains_in_a_slow_turn(self, level):
    assert settled(level) == pytest.approx(BOLT_TURN_DAMPING_KD[level])
    assert BOLT_TURN_DAMPING_KD == [0.0, 0.3, 0.6, 1.0]

  def test_off_above_30_mph(self):
    assert settled(3, v_ego=30 * CV.MPH_TO_MS) == 0.0
    assert settled(3, v_ego=FAST) == 0.0

  def test_off_near_straight(self):
    assert settled(3, setpoint=0.3, measurement=-0.2) == 0.0
    assert settled(3, setpoint=0.0, measurement=0.0) == 0.0

  def test_turn_weight_uses_larger_of_setpoint_and_measurement(self):
    assert settled(3, setpoint=0.1, measurement=-0.8) == pytest.approx(1.0)
    assert settled(3, setpoint=-0.55, measurement=0.0) == pytest.approx(0.5)

  def test_speed_fade_is_monotone(self):
    gains = [settled(3, v_ego=v * CV.MPH_TO_MS) for v in range(35)]
    assert all(g == pytest.approx(1.0) for g in gains[:21])
    assert all(a >= b for a, b in zip(gains, gains[1:], strict=False))
    assert gains[25] == pytest.approx(0.5)

  def test_fades_out_while_pressed_then_back(self):
    damping = BoltTurnDamping(DT_CTRL)
    for _ in range(200):
      damping.update(3, SLOW, 2.0, 1.8, 0.0, False)
    pressed = [damping.update(3, SLOW, 2.0, 1.8, 0.0, True)[0] for _ in range(30)]
    assert pressed[0] == pytest.approx(0.95)  # no output step on the first pressed frame
    assert pressed[19] == 0.0 and pressed[-1] == 0.0
    after_release = [damping.update(3, SLOW, 2.0, 1.8, 0.0, False)[0] for _ in range(150)]
    assert after_release[0] == pytest.approx(0.01)
    assert after_release[-1] == pytest.approx(1.0)

  def test_one_frame_press_flicker_barely_moves_gain(self):
    damping = BoltTurnDamping(DT_CTRL)
    for _ in range(200):
      damping.update(3, SLOW, 2.0, 1.8, 0.0, False)
    damping.update(3, SLOW, 2.0, 1.8, 0.0, True)
    assert damping.update(3, SLOW, 2.0, 1.8, 0.0, False)[0] == pytest.approx(0.96)

  def test_reset_starts_from_zero_gain_and_no_setpoint_history(self):
    damping = BoltTurnDamping(DT_CTRL)
    for _ in range(200):
      damping.update(3, SLOW, 2.0, 1.8, 0.0, False)
    damping.reset()
    kd, error_rate = damping.update(3, SLOW, -2.0, -1.8, 0.0, False)
    assert kd == pytest.approx(0.01)
    assert error_rate == 0.0  # the jump from +2 to -2 across the reset is not a rate

  def test_switching_level_ramps(self):
    damping = BoltTurnDamping(DT_CTRL)
    gains = [damping.update(3, SLOW, 2.0, 1.8, 0.0, False)[0] for _ in range(150)]
    gains += [damping.update(1, SLOW, 2.0, 1.8, 0.0, False)[0] for _ in range(150)]
    gains += [damping.update(0, SLOW, 2.0, 1.8, 0.0, False)[0] for _ in range(150)]
    steps = np.abs(np.diff([0.0] + gains))
    assert steps.max() <= 0.01 + 1e-9
    assert gains[149] == pytest.approx(1.0)
    assert gains[299] == pytest.approx(0.3)
    assert gains[-1] == 0.0

  def test_level_out_of_range_is_clamped(self):
    assert settled(7) == pytest.approx(1.0)
    assert settled(-1) == 0.0


def error_rates(setpoints, measurement_rate):
  damping = BoltTurnDamping(DT_CTRL)
  return [damping.update(3, SLOW, sp, 1.0, measurement_rate, False)[1] for sp in setpoints]


class TestBoltTurnErrorRate:
  def test_zero_when_wheel_tracks_setpoint(self):
    # setpoint ramps at 2 m/s^3 and the (already filtered) wheel rate matches once the filter settles
    rates = error_rates([1.0 + 2.0 * DT_CTRL * i for i in range(200)], 2.0)
    assert rates[-1] == pytest.approx(0.0, abs=1e-3)

  def test_sign_opposes_wheel_motion_away_from_setpoint(self):
    assert error_rates([1.0] * 10, 3.0)[-1] == -3.0
    assert error_rates([1.0] * 10, -3.0)[-1] == 3.0

  def test_wide_clip(self):
    assert error_rates([1.0] * 10, 20.0)[-1] == -BOLT_TURN_DAMPING_MAX_RATE
    assert error_rates([1.0] * 10, -20.0)[-1] == BOLT_TURN_DAMPING_MAX_RATE
    assert error_rates([1.0] * 10, 5.0)[-1] == -5.0  # beyond the 2.5 PID clip


def build(car_name, v_ego, level):
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
  return controller, VM, CS, params, SimpleNamespace(bolt_turn_damping_level=level)


def run_turn(car_name, v_ego, level, angle_rate_deg, frames=150, target_lat_accel=1.5, switch=None, accel=0.0):
  """Hold a steady planned turn while the wheel turns further at angle_rate_deg per second.

  switch = (frame, level) changes the level mid-run, like flipping the onroad favorite.
  accel changes the speed each frame while the wheel and the planned curvature stay put."""
  controller, VM, CS, params, toggles = build(car_name, v_ego, level)
  # measurement = -curvature(angle) * v^2, linear in angle with no roll
  angle = target_lat_accel / (-VM.calc_curvature(math.radians(1.0), v_ego, 0.0) * v_ego ** 2)
  desired_curvature = target_lat_accel / v_ego ** 2
  controller.curvature_request_buffer = deque([desired_curvature] * controller.request_buffer_len,
                                              maxlen=controller.request_buffer_len)
  outputs, logs = [], []
  direction = math.copysign(1.0, angle)
  for frame in range(frames):
    if switch is not None and frame == switch[0]:
      toggles.bolt_turn_damping_level = switch[1]
    CS.steeringAngleDeg = angle
    CS.vEgo = v_ego + accel * DT_CTRL * frame
    output, _, lac_log = controller.update(True, CS, VM, params, False, desired_curvature, False, 0.2, None, None,
                                           toggles)
    outputs.append(output)
    logs.append(lac_log)
    angle += direction * angle_rate_deg * DT_CTRL
  return controller, outputs, logs


class TestBoltTurnDampingInController:
  def test_damping_opposes_wheel_moving_away_from_target(self):
    _, _, logs = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, 3, angle_rate_deg=40.0)
    # the wheel turns further into the turn than planned: d pulls back (opposite sign to the turn)
    assert logs[-1].actualLateralAccel > 1.5
    assert logs[-1].d < -0.5

  def test_no_damping_when_wheel_follows_plan(self):
    _, _, logs = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, 3, angle_rate_deg=0.0)
    assert abs(logs[-1].d) < 1e-3

  def test_no_damping_from_speed_change_with_steady_wheel(self):
    # pulling away through a tight turn: lat accel = curvature * v^2 rises with speed on both sides of the error
    _, _, logs = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 4.0, 3, angle_rate_deg=0.0, frames=200, accel=1.5)
    assert max(abs(lac_log.d) for lac_log in logs[50:]) < 0.1

  def test_level_off_has_no_d(self):
    _, _, logs = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, 0, angle_rate_deg=40.0)
    assert all(lac_log.d == 0.0 for lac_log in logs)

  @pytest.mark.parametrize("start, switch_to", [(0, 3), (3, 0)])
  def test_level_switch_moves_d_gradually(self, start, switch_to):
    _, _, logs = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, start, angle_rate_deg=40.0, frames=300,
                          switch=(150, switch_to))
    d = np.array([lac_log.d for lac_log in logs])
    assert abs(d[-1] - d[149]) > 0.5  # the switch did take effect
    # one frame's gain step (0.01) times the clipped error rate; the rate itself settles before the switch
    assert np.abs(np.diff(d[140:])).max() < 0.01 * (BOLT_TURN_DAMPING_MAX_RATE + 2.5)

  @pytest.mark.parametrize("car_name, v_ego", [
    (HYUNDAI.HYUNDAI_SONATA, SLOW),
    (GM.CHEVROLET_BOLT_ACC_2022_2023, FAST),
  ])
  def test_unchanged_where_not_gated_in(self, car_name, v_ego):
    _, off_outputs, off_logs = run_turn(car_name, v_ego, 0, angle_rate_deg=40.0)
    _, on_outputs, on_logs = run_turn(car_name, v_ego, 3, angle_rate_deg=40.0)
    assert on_outputs == off_outputs
    assert [x.d for x in on_logs] == [x.d for x in off_logs]
