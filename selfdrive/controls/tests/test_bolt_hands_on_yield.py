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
from openpilot.selfdrive.controls.lib.bolt_hands_on_yield import BoltHandsOnYield
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque

SLOW = 12 * CV.MPH_TO_MS
FAST = 40 * CV.MPH_TO_MS


# setpoint 2.0 is a right turn (controller right +); the car's driver torque is left +, so steering_torque -1.0 is the
# driver steering into that turn (push) and +1.0 against it (hold-back). side=-1 mirrors both into a left turn.
def settled(push_percent=100, hold_back_percent=100, v_ego=SLOW, setpoint=2.0, measurement=2.0, steering_torque=-1.0,
            frames=200, side=1.0):
  yielder = BoltHandsOnYield(DT_CTRL)
  for _ in range(frames):
    yielder.update(push_percent, hold_back_percent, v_ego, side * setpoint, side * measurement, side * steering_torque)
  return yielder


SIDES = [1.0, -1.0]


class TestBoltHandsOnYieldWeights:
  @pytest.mark.parametrize("side", SIDES)
  @pytest.mark.parametrize("steering_torque", [0.0, 0.2, -0.3, 0.3])
  def test_none_at_resting_grip(self, steering_torque, side):
    yielder = settled(steering_torque=steering_torque, side=side)
    assert yielder.push == 0.0
    assert yielder.hold_back == 0.0

  @pytest.mark.parametrize("side", SIDES)
  @pytest.mark.parametrize("nm", [0.9, 1.0, 2.5])
  def test_push_only_into_the_turn(self, nm, side):
    yielder = settled(steering_torque=-nm, side=side)
    assert yielder.push == pytest.approx(1.0)
    assert yielder.hold_back == 0.0

  @pytest.mark.parametrize("side", SIDES)
  @pytest.mark.parametrize("nm", [0.9, 1.0, 2.5])
  def test_hold_back_only_against_the_turn(self, nm, side):
    yielder = settled(steering_torque=nm, side=side)
    assert yielder.push == 0.0
    assert yielder.hold_back == pytest.approx(1.0)

  def test_monotone_in_driver_torque(self):
    pushes = [settled(steering_torque=-nm).push for nm in np.linspace(0.0, 1.5, 31)]
    holds = [settled(steering_torque=nm).hold_back for nm in np.linspace(0.0, 1.5, 31)]
    for weights in (pushes, holds):
      assert all(a <= b for a, b in zip(weights, weights[1:], strict=False))
      assert weights[12] == pytest.approx(0.5)  # 0.6 Nm, halfway

  def test_settings_scale_each_half(self):
    assert settled(push_percent=40, hold_back_percent=70).push == pytest.approx(0.4)
    assert settled(push_percent=40, hold_back_percent=70, steering_torque=1.0).hold_back == pytest.approx(0.7)

  def test_off_by_default(self):
    for steering_torque in (-1.0, 1.0):
      yielder = settled(push_percent=0, hold_back_percent=0, steering_torque=steering_torque)
      assert yielder.push == 0.0
      assert yielder.hold_back == 0.0

  def test_off_above_30_mph(self):
    assert settled(v_ego=30 * CV.MPH_TO_MS).push == 0.0
    assert settled(v_ego=FAST, steering_torque=1.0).hold_back == 0.0
    assert settled(v_ego=25 * CV.MPH_TO_MS).push == pytest.approx(0.5)

  def test_off_near_straight(self):
    assert settled(setpoint=0.2, measurement=-0.2).push == 0.0
    assert settled(setpoint=0.2, measurement=0.9).push == pytest.approx(1.0)  # either one in a real turn

  def test_driver_torque_is_smoothed(self):
    # a one-frame ~0.3 Nm jump on a 0.5 Nm push moves the weight a little, not by half
    yielder = settled(steering_torque=-0.5)
    steady = yielder.push
    yielder.update(100, 100, SLOW, 2.0, 2.0, -0.8)
    assert 0 < yielder.push - steady < 0.1
    for _ in range(30):
      yielder.update(100, 100, SLOW, 2.0, 2.0, -0.5)
    assert yielder.push == pytest.approx(steady, abs=1e-3)

  @pytest.mark.parametrize("steering_torque, mode", [(-1.0, "push"), (1.0, "hold_back")])
  def test_release_fades_over_a_quarter_second(self, steering_torque, mode):
    yielder = settled(steering_torque=steering_torque)
    weights = []
    for _ in range(40):
      yielder.update(100, 100, SLOW, 2.0, 2.0, 0.0)
      weights.append(getattr(yielder, mode))
    assert np.abs(np.diff([1.0] + weights)).max() <= DT_CTRL / 0.25 + 1e-9
    assert weights[19] > 0.15
    assert weights[-1] == 0.0

  @pytest.mark.parametrize("first, then", [(-1.0, 1.0), (1.0, -1.0)])
  def test_steering_the_other_way_ends_the_other_mode_at_once(self, first, then):
    yielder = settled(steering_torque=first)
    for _ in range(20):  # through the torque filter
      yielder.update(100, 100, SLOW, 2.0, 2.0, then)
    assert min(yielder.push, yielder.hold_back) == 0.0
    assert max(yielder.push, yielder.hold_back) > 0.5

  @pytest.mark.parametrize("side", SIDES)
  def test_turn_direction_follows_the_wheel_when_the_plan_is_near_zero(self, side):
    # driver turning in ahead of a plan still wobbling around center: push throughout, no flip to hold-back
    yielder = BoltHandsOnYield(DT_CTRL)
    pushes = []
    for frame in range(200):
      yielder.update(100, 0, SLOW, side * 0.05 * (-1) ** frame, side * 1.5, -side * 0.8)
      pushes.append(yielder.push)
    assert min(pushes[100:]) == pytest.approx(max(pushes[100:]))
    assert pushes[-1] > 0.8

  def test_setting_change_ramps(self):
    yielder = settled(push_percent=0, frames=50)  # driver torque settled first
    pushes = []
    for percent in [100] * 150 + [40] * 150:
      yielder.update(percent, 0, SLOW, 2.0, 2.0, -1.0)
      pushes.append(yielder.push)
    assert np.abs(np.diff([0.0] + pushes)).max() <= DT_CTRL + 1e-9
    assert pushes[149] == pytest.approx(1.0)
    assert pushes[-1] == pytest.approx(0.4)

  def test_setting_out_of_range_is_clamped(self):
    assert settled(push_percent=150).push == pytest.approx(1.0)
    assert settled(hold_back_percent=-20, steering_torque=1.0).hold_back == 0.0

  def test_reset_fades_back_in(self):
    yielder = settled()
    yielder.reset()
    assert yielder.driver_filter.x == 0.0
    yielder.update(100, 100, SLOW, 2.0, 2.0, -1.0)
    assert yielder.push_share == pytest.approx(DT_CTRL)
    assert yielder.push < 0.01


class TestBoltHandsOnYieldScaling:
  @pytest.mark.parametrize("side", SIDES)
  def test_push_scales_error_against_the_driver(self, side):
    # wheel ahead of the plan: the error points out of the turn, against the push
    yielder = settled(push_percent=60, side=side)
    assert yielder.yield_error(-side * 0.5) == pytest.approx(-side * 0.2)

  @pytest.mark.parametrize("side", SIDES)
  def test_push_keeps_error_with_the_driver(self, side):
    assert settled(side=side).yield_error(side * 0.5) == side * 0.5

  @pytest.mark.parametrize("side", SIDES)
  def test_hold_back_leaves_error_alone(self, side):
    # holding back with the wheel short of the plan: the error is against the driver, but push is off here
    assert settled(steering_torque=1.0, side=side).yield_error(side * 0.5) == side * 0.5

  def test_push_scales_damping(self):
    assert settled(push_percent=60).yield_kd(1.0) == pytest.approx(0.4)
    assert settled(push_percent=60, steering_torque=0.0).yield_kd(1.0) == 1.0
    assert settled(push_percent=60, steering_torque=1.0).yield_kd(1.0) == 1.0  # not while holding back

  @pytest.mark.parametrize("side", SIDES)
  def test_hold_back_scales_output_against_the_driver(self, side):
    yielder = settled(hold_back_percent=60, steering_torque=1.0, side=side)
    assert yielder.yield_output(side * 0.8) == pytest.approx(side * 0.32)

  @pytest.mark.parametrize("side", SIDES)
  def test_hold_back_keeps_output_with_the_driver(self, side):
    assert settled(steering_torque=1.0, side=side).yield_output(-side * 0.8) == -side * 0.8

  @pytest.mark.parametrize("side", SIDES)
  def test_push_leaves_output_alone(self, side):
    # pushing into the turn while the car's output opposes it: that's push's job (P), not hold-back's
    assert settled(side=side).yield_output(-side * 0.8) == -side * 0.8


def build(car_name, v_ego, push_percent, hold_back_percent):
  CarInterface = interfaces[car_name]
  CP = CarInterface.get_non_essential_params(car_name)
  CI = CarInterface(CP, custom.StarPilotCarParams.new_message())
  controller = LatControlTorque(CP.as_reader(), CI, DT_CTRL)
  VM = VehicleModel(CP)
  CS = car.CarState.new_message()
  CS.vEgo = v_ego
  params = log.LiveParametersData.new_message()
  params.steerRatio = CP.steerRatio
  params.stiffnessFactor = 1.0
  params.roll = 0.0
  params.angleOffsetDeg = 0.0
  toggles = SimpleNamespace(bolt_turn_damping=100, bolt_turn_exit_hold=70, bolt_turn_push_yield=push_percent,
                            bolt_turn_hold_back_yield=hold_back_percent)
  return controller, VM, CS, params, toggles


def run_turn(car_name, push_percent, hold_back_percent, driver_nm, v_ego=SLOW, plan=2.0, offset=0.5, side=1.0,
             logs=None, controllers=None):
  """A steady turn planned at `plan` m/s^2 with the wheel `offset` past it (negative: short of it) and the driver's
  torque per frame from `driver_nm` (+ = into the turn). side=-1 mirrors it into a left turn.

  Returns the torque in the turn's direction per frame (the controller returns it negated). `logs`, if given, gets
  (error, f) per frame from torqueState, also in the turn's direction; `controllers` gets the controller."""
  controller, VM, CS, params, toggles = build(car_name, v_ego, push_percent, hold_back_percent)
  if controllers is not None:
    controllers.append(controller)
  per_degree = -VM.calc_curvature(math.radians(1.0), v_ego, 0.0) * v_ego ** 2
  controller.curvature_request_buffer = deque([side * plan / v_ego ** 2] * controller.request_buffer_len,
                                              maxlen=controller.request_buffer_len)
  CS.steeringAngleDeg = side * (plan + offset) / per_degree
  torque = []
  for nm in driver_nm:
    CS.steeringTorque = -side * nm  # left +, so a right turn (side 1) is pushed with negative torque
    CS.steeringPressed = abs(nm) > 1.0
    output, _, pid_log = controller.update(True, CS, VM, params, False, side * plan / v_ego ** 2, False, 0.2, None,
                                           None, toggles)
    torque.append(-side * output)
    if logs is not None:
      logs.append((side * pid_log.error, side * pid_log.f))
  return torque


PUSH_RAMP = [0.0] * 150 + np.linspace(0.0, 1.2, 120).tolist() + [1.2] * 30  # push into the turn, through the press
HOLD_BACK = [0.0] * 150 + [-0.7] * 100  # hold the wheel back below the press


class TestBoltHandsOnYieldInController:
  @pytest.mark.parametrize("side", [1.0, -1.0])
  def test_push_ahead_of_plan_stops_pulling_back(self, side):
    # the wheel is ahead of the plan with the driver pushing: without the yield P pulls against the push
    off = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, 0, PUSH_RAMP, side=side)
    on = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 100, 0, PUSH_RAMP, side=side)
    assert on[:150] == off[:150]
    assert on[-1] > off[-1] + 0.2
    assert on[-1] > 0.3  # still holding the turn, not fighting the driver

  @pytest.mark.parametrize("side", [1.0, -1.0])
  def test_push_has_no_step_at_the_press(self, side):
    # without it the exit-hold floor lets go at the press and the output steps ~1.8 against the driver
    off = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, 0, PUSH_RAMP, side=side)
    on = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 100, 100, PUSH_RAMP, side=side)
    press = 150 + int(np.argmax(np.linspace(0.0, 1.2, 120) > 1.0))
    assert off[press - 1] - off[press] > 1.0
    assert abs(on[press] - on[press - 1]) < 0.01
    # P fades in ~1 s of push here; where the output passes the floor it moves a little faster than GM's
    # up-rate limit (~0.011 per frame), which smooths it
    assert np.abs(np.diff(on[150:])).max() < 0.05

  @pytest.mark.parametrize("side", [1.0, -1.0])
  def test_hold_back_scales_down_the_output(self, side):
    off = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, 0, HOLD_BACK, offset=-0.5, side=side)
    on = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, 100, HOLD_BACK, offset=-0.5, side=side)
    assert on[:150] == off[:150]
    assert off[-1] > 0.5
    assert on[-1] == pytest.approx(off[-1] / 3, rel=1e-6)  # 0.7 Nm is 2/3 of the way from 0.3 to 0.9

  @pytest.mark.parametrize("side", [1.0, -1.0])
  def test_hold_back_leaves_output_with_the_driver(self, side):
    # driver pushing into the turn with the wheel short of the plan: the car's torque helps, nothing to yield
    driver = [0.0] * 150 + [0.7] * 100
    off = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, 0, driver, offset=-0.5, side=side)
    on = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 100, 100, driver, offset=-0.5, side=side)
    assert on == off

  def test_resting_grip_unchanged(self):
    driver = [0.0] * 150 + [0.25, -0.25] * 50
    off = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, 0, driver)
    on = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 100, 100, driver)
    assert on == off

  @pytest.mark.parametrize("car_name, v_ego", [
    (HYUNDAI.HYUNDAI_SONATA, SLOW),
    (GM.CHEVROLET_BOLT_ACC_2022_2023, FAST),
  ])
  def test_unchanged_where_not_gated_in(self, car_name, v_ego):
    for driver, offset in ((PUSH_RAMP, 0.5), (HOLD_BACK, -0.5)):
      off = run_turn(car_name, 0, 0, driver, v_ego=v_ego, offset=offset)
      on = run_turn(car_name, 100, 100, driver, v_ego=v_ego, offset=offset)
      assert on == off

  @pytest.mark.parametrize("nm, kd", [(0.0, 1.0), (0.6, 0.5), (0.8, 1 / 6)])
  def test_push_fades_the_damping_gain(self, nm, kd):
    # the wheel moving with the driver must not meet D instead of P; damping at 100% is kd 1.0 here
    controller, VM, CS, params, toggles = build(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, 100, 0)
    per_degree = -VM.calc_curvature(math.radians(1.0), SLOW, 0.0) * SLOW ** 2
    CS.steeringAngleDeg = 2.5 / per_degree
    CS.steeringTorque = -nm
    for _ in range(150):
      controller.update(True, CS, VM, params, False, 2.0 / SLOW ** 2, False, 0.2, None, None, toggles)
    assert controller.pid._k_d[1][0] == pytest.approx(kd)

  @pytest.mark.parametrize("side", [1.0, -1.0])
  def test_push_also_removes_the_friction_pull(self, side):
    # the friction feedforward is built from the same error: with the wheel ahead it pulls out of the turn
    off_logs, on_logs = [], []
    run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, 0, PUSH_RAMP, side=side, logs=off_logs)
    run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 100, 0, PUSH_RAMP, side=side, logs=on_logs)
    assert on_logs[-1][1] > off_logs[-1][1] + 0.1

  @pytest.mark.parametrize("side", [1.0, -1.0])
  def test_log_keeps_the_real_error(self, side):
    off_logs, on_logs = [], []
    run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, 0, PUSH_RAMP, side=side, logs=off_logs)
    run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 100, 0, PUSH_RAMP, side=side, logs=on_logs)
    assert [e for e, _ in on_logs] == pytest.approx([e for e, _ in off_logs])
    assert on_logs[-1][0] < -0.3

  @pytest.mark.parametrize("side", [1.0, -1.0])
  def test_push_setting_leaves_a_hold_back_alone(self, side):
    off = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, 0, HOLD_BACK, offset=-0.5, side=side)
    push_only = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 100, 0, HOLD_BACK, offset=-0.5, side=side)
    assert push_only == off

  @pytest.mark.parametrize("side", [1.0, -1.0])
  def test_hold_back_setting_leaves_a_push_alone(self, side):
    off = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, 0, PUSH_RAMP, side=side)
    hold_only = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, 100, PUSH_RAMP, side=side)
    assert hold_only == off

  @pytest.mark.parametrize("side", [1.0, -1.0])
  def test_hold_back_stops_the_integrator_winding(self, side):
    # a gentle turn, unsaturated, wheel short of the plan: without the freeze I would keep growing into the turn
    held = []
    for hold_back_percent in (0, 100):
      controllers = []
      run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, hold_back_percent, [0.0] * 150 + [-0.7] * 200, plan=1.0,
               offset=-0.05, side=side, controllers=controllers)
      held.append(side * controllers[0].pid.i)
    off_i, on_i = held
    assert 0 < on_i < 0.5 * off_i  # on_i: what I reached before the hold-back began

  @pytest.mark.parametrize("side", [1.0, -1.0])
  def test_push_release_is_spread_out(self, side):
    # letting go with the wheel ahead of the plan: P comes back over the release ramp, not in a few frames
    driver = PUSH_RAMP + [0.0] * 60
    on = run_turn(GM.CHEVROLET_BOLT_ACC_2022_2023, 100, 0, driver, side=side)
    release = np.abs(np.diff(on[len(PUSH_RAMP) - 1:]))
    assert (release > 1e-6).sum() >= 20

  def test_disengage_resets_yield_state(self):
    controller, VM, CS, params, toggles = build(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, 100, 100)
    CS.steeringTorque = -1.0
    for _ in range(150):
      controller.update(True, CS, VM, params, False, 2.0 / SLOW ** 2, False, 0.2, None, None, toggles)
    assert controller.bolt_hands_on_yield.push_share == pytest.approx(1.0)
    controller.update(False, CS, VM, params, False, 0.0, False, 0.2, None, None, toggles)
    assert controller.bolt_hands_on_yield.push_share == 0.0
    assert controller.bolt_hands_on_yield.hold_back_share == 0.0
