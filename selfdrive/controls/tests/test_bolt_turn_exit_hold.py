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
from openpilot.selfdrive.controls.lib.bolt_turn_exit_hold import BoltTurnExitHold
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque

SLOW = 15 * CV.MPH_TO_MS
FAST = 40 * CV.MPH_TO_MS
LIMITS = (-3.0, 3.0)


def settled(hold_percent, output, v_ego=SLOW, plan=2.0, limits=LIMITS, frames=200):
  hold = BoltTurnExitHold(DT_CTRL)
  result = output
  for _ in range(frames):
    result = hold.update(hold_percent, v_ego, plan if isinstance(plan, tuple) else (plan,) * 3, output, False, *limits)
  return result


class TestBoltTurnExitHoldFloor:
  @pytest.mark.parametrize("plan, output", [(2.0, 1.5), (2.0, 3.0), (-2.0, -1.5), (-2.0, -3.0)])
  def test_unchanged_at_or_above_floor(self, plan, output):
    # turn-in: the PID already asks for more turn than the floor
    assert settled(60, output, plan=plan) == output

  @pytest.mark.parametrize("plan, output", [(2.0, 0.5), (2.0, -1.0), (-2.0, -0.5), (-2.0, 1.0)])
  def test_floor_is_share_of_plan(self, plan, output):
    assert settled(60, output, plan=plan) == pytest.approx(0.6 * plan)

  def test_no_plan_leaves_output_alone(self):
    assert settled(100, 0.7, plan=0.0) == 0.7
    assert settled(100, -0.7, plan=0.0) == -0.7

  @pytest.mark.parametrize("targets", [(1.0, -0.1, 0.5), (1.0, 1.0, -0.2), (-1.0, 0.1, -1.0), (0.0, 0.3, 0.0)])
  def test_no_floor_unless_targets_agree(self, targets):
    # road crown (roll-adjusted plan flips) or the plan flipping sides ahead of the setpoint
    assert settled(100, -0.4, plan=targets) == -0.4
    assert settled(100, 0.4, plan=targets) == 0.4

  def test_floor_follows_smallest_target(self):
    assert settled(60, 0.0, plan=(2.0, 1.8, 1.0)) == pytest.approx(0.6)
    assert settled(60, 0.0, plan=(-1.0, -2.0, -1.5)) == pytest.approx(-0.6)

  def test_off_by_default(self):
    assert settled(0, -1.0) == -1.0

  def test_floor_within_pid_limits(self):
    assert settled(100, 0.0, plan=3.5, limits=(-2.4, 3.4)) == pytest.approx(3.4)
    assert settled(100, 0.0, plan=-3.5, limits=(-2.4, 3.4)) == pytest.approx(-2.4)

  def test_off_above_30_mph(self):
    assert settled(100, -1.0, v_ego=30 * CV.MPH_TO_MS) == -1.0
    assert settled(100, -1.0, v_ego=FAST) == -1.0

  def test_speed_fade_is_monotone(self):
    floors = [settled(100, 0.0, v_ego=v * CV.MPH_TO_MS) for v in range(35)]
    assert all(f == pytest.approx(2.0) for f in floors[:21])
    assert all(a >= b for a, b in zip(floors, floors[1:], strict=False))
    assert floors[25] == pytest.approx(1.0)

  def test_off_at_once_while_pressed_then_ramps_back(self):
    hold = BoltTurnExitHold(DT_CTRL)
    for _ in range(200):
      hold.update(100, SLOW, (2.0,) * 3, 0.0, False, *LIMITS)
    for _ in range(30):
      assert hold.update(100, SLOW, (2.0,) * 3, -0.5, True, *LIMITS) == -0.5
    after_release = [hold.update(100, SLOW, (2.0,) * 3, 0.0, False, *LIMITS) for _ in range(50)]
    assert after_release[0] == pytest.approx(2.0 * DT_CTRL / 0.25)
    assert after_release[24] == pytest.approx(2.0)
    assert np.abs(np.diff([0.0] + after_release)).max() <= 2.0 * DT_CTRL / 0.25 + 1e-9

  def test_setting_change_ramps(self):
    hold = BoltTurnExitHold(DT_CTRL)
    floors = [hold.update(100, SLOW, (1.0,) * 3, 0.0, False, *LIMITS) for _ in range(150)]
    floors += [hold.update(40, SLOW, (1.0,) * 3, 0.0, False, *LIMITS) for _ in range(150)]
    assert np.abs(np.diff([0.0] + floors)).max() <= DT_CTRL / 0.25 + 1e-9
    assert floors[149] == pytest.approx(1.0)
    assert floors[-1] == pytest.approx(0.4)

  def test_setting_out_of_range_is_clamped(self):
    assert settled(150, 0.0) == pytest.approx(2.0)
    assert settled(-20, -1.0) == -1.0

  def test_reset_fades_back_in(self):
    hold = BoltTurnExitHold(DT_CTRL)
    for _ in range(200):
      hold.update(100, SLOW, (2.0,) * 3, 0.0, False, *LIMITS)
    hold.reset()
    assert hold.update(100, SLOW, (2.0,) * 3, 0.0, False, *LIMITS) == pytest.approx(2.0 * DT_CTRL * DT_CTRL / 0.25)


def build(car_name, v_ego, hold_percent):
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
  return controller, VM, CS, params, SimpleNamespace(bolt_turn_damping_level=0, bolt_turn_exit_hold=hold_percent)


def run_exit(car_name, hold_percent, v_ego=7.0, past=0.3, initial_i=0.0):
  """Route 181 16:00:32 shape: a right turn planned at 2.9 m/s^2 eases to 1.2 while the wheel stays 0.3 past it.

  Returns the torque in the turn's direction (the controller returns it negated) and the controller."""
  controller, VM, CS, params, toggles = build(car_name, v_ego, hold_percent)
  per_degree = -VM.calc_curvature(math.radians(1.0), v_ego, 0.0) * v_ego ** 2
  plans = [2.9] * 150 + [2.9 - 1.7 * i / 100 for i in range(100)] + [1.2] * 50
  controller.curvature_request_buffer = deque([plans[0] / v_ego ** 2] * controller.request_buffer_len,
                                              maxlen=controller.request_buffer_len)
  controller.pid.i = initial_i
  torque = []
  for plan in plans:
    CS.steeringAngleDeg = (plan + past) / per_degree
    output, _, _ = controller.update(True, CS, VM, params, False, plan / v_ego ** 2, False, 0.2, None, None, toggles)
    torque.append(-output)
  return controller, torque


class TestBoltTurnExitHoldInController:
  def test_exit_keeps_turning_with_hold(self):
    _, torque = run_exit(GM.CHEVROLET_BOLT_ACC_2022_2023, 60)
    assert min(torque[150:]) > 0.3

  def test_integrator_does_not_wind_up_while_held(self):
    # the wheel stays past the plan: without the hold I builds toward center; while the floor holds it stays put
    off, _ = run_exit(GM.CHEVROLET_BOLT_ACC_2022_2023, 0)
    held, _ = run_exit(GM.CHEVROLET_BOLT_ACC_2022_2023, 60)
    assert off.pid.i < -0.3
    assert held.pid.i > off.pid.i + 0.2

  @pytest.mark.parametrize("roll", [0.03, -0.03])
  def test_road_crown_on_straight_gets_no_hold(self, roll):
    outputs = []
    for hold_percent in (0, 100):
      controller, VM, CS, params, toggles = build(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, hold_percent)
      params.roll = roll
      run = []
      for frame in range(200):
        CS.steeringAngleDeg = 2.0 * math.sin(frame * DT_CTRL * 3.0)
        run.append(controller.update(True, CS, VM, params, False, 0.0, False, 0.2, None, None, toggles)[0])
      outputs.append(run)
    assert outputs[0] == outputs[1]

  def test_integrator_recovers_toward_floor_while_held(self):
    # I wound toward center earlier and the wheel is now short of the plan: I must still grow back into the turn
    held, held_torque = run_exit(GM.CHEVROLET_BOLT_ACC_2022_2023, 100, past=-0.3, initial_i=-4.0)
    off, off_torque = run_exit(GM.CHEVROLET_BOLT_ACC_2022_2023, 0, past=-0.3, initial_i=-4.0)
    assert held_torque != off_torque  # the floor was in control
    assert held.pid.i > -3.5
    assert held.pid.i == pytest.approx(off.pid.i)  # and I moved just as without the hold

  def test_small_turn_on_crown_gets_no_hold(self):
    # a small planned right turn whose roll-adjusted plan points left: the floor stays off
    outputs = []
    for hold_percent in (0, 100):
      controller, VM, CS, params, toggles = build(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, hold_percent)
      params.roll = 0.05  # ~0.49 m/s^2 of roll compensation against a 0.2 plan
      per_degree = -VM.calc_curvature(math.radians(1.0), SLOW, 0.0) * SLOW ** 2
      CS.steeringAngleDeg = 1.0 / per_degree  # wheel past the plan, so the PID asks for less than 0.2
      outputs.append([controller.update(True, CS, VM, params, False, 0.2 / SLOW ** 2, False, 0.2, None, None,
                                        toggles)[0] for _ in range(200)])
    assert outputs[0] == outputs[1]

  def test_exit_unwinds_without_hold(self):
    _, torque = run_exit(GM.CHEVROLET_BOLT_ACC_2022_2023, 0)
    assert torque[-1] < -0.3

  def test_off_matches_controller_without_hold(self, monkeypatch):
    _, off = run_exit(GM.CHEVROLET_BOLT_ACC_2022_2023, 0)
    monkeypatch.setattr(BoltTurnExitHold, "update", lambda self, *args: args[3])
    _, bypassed = run_exit(GM.CHEVROLET_BOLT_ACC_2022_2023, 0)
    assert off == bypassed

  @pytest.mark.parametrize("car_name, v_ego", [
    (HYUNDAI.HYUNDAI_SONATA, SLOW),
    (GM.CHEVROLET_BOLT_ACC_2022_2023, FAST),
  ])
  def test_unchanged_where_not_gated_in(self, car_name, v_ego):
    _, off = run_exit(car_name, 0, v_ego=v_ego)
    _, on = run_exit(car_name, 100, v_ego=v_ego)
    assert on == off

  def test_driver_pressing_gets_no_hold(self, monkeypatch):
    # the driver takes the wheel at the end of a held exit: the hold passes the PID output through untouched
    pressed = []
    for hold_percent in (0, 100):
      controller, _ = run_exit(GM.CHEVROLET_BOLT_ACC_2022_2023, hold_percent)
      calls = []
      original = BoltTurnExitHold.update

      def spy(self, *args, original=original, calls=calls):
        result = original(self, *args)
        calls.append((args[3], result))
        return result
      monkeypatch.setattr(BoltTurnExitHold, "update", spy)
      _, VM, CS, params, toggles = build(GM.CHEVROLET_BOLT_ACC_2022_2023, 7.0, hold_percent)
      CS.steeringPressed = True
      CS.steeringAngleDeg = 1.5 / (-VM.calc_curvature(math.radians(1.0), 7.0, 0.0) * 7.0 ** 2)
      output, _, _ = controller.update(True, CS, VM, params, False, 1.2 / 7.0 ** 2, False, 0.2, None, None, toggles)
      monkeypatch.undo()
      assert calls[-1][0] == calls[-1][1]
      assert controller.bolt_turn_exit_hold.release_weight == 0.0
      pressed.append(-output)
    # less stored-up I after a held exit, so no bigger push toward center than with the hold off
    assert pressed[1] >= pressed[0]

  def test_disengage_resets_hold_state(self):
    controller, _ = run_exit(GM.CHEVROLET_BOLT_ACC_2022_2023, 60)
    assert controller.bolt_turn_exit_hold.share == pytest.approx(0.6)
    _, VM, CS, params, toggles = build(GM.CHEVROLET_BOLT_ACC_2022_2023, SLOW, 60)
    controller.update(False, CS, VM, params, False, 0.0, False, 0.2, None, None, toggles)
    assert controller.bolt_turn_exit_hold.share == 0.0
    assert controller.bolt_turn_exit_hold.release_weight == 0.0
