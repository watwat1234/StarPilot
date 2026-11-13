import numpy as np

from opendbc.car import rate_limit
# same slow turns as the damping and the exit hold
from openpilot.selfdrive.controls.lib.bolt_turn_damping import (BOLT_TURN_DAMPING_LAT_ACCEL_BP, BOLT_TURN_DAMPING_LAT_ACCEL_V,
                                                                BOLT_TURN_DAMPING_SPEED_BP, BOLT_TURN_DAMPING_SPEED_V)

# Bolt 2022/23 slow turns with hands on the wheel (routes 0000019a-000001af): the controller treats anything under
# the 1.0 Nm press threshold as hands-off. Pushing tighter than the plan, the low-speed P cancels the push until the
# car's torque runs out at ~1.8 Nm, then the wheel breaks loose (push); holding the wheel back keeps full torque
# against the driver, so letting go whips it in (hold-back). Both yield in proportion to the driver's torque.
BOLT_HANDS_ON_YIELD_GRIP_BP = [0.3, 1.0]  # Nm of driver torque: none below a resting grip, full at the press
BOLT_HANDS_ON_YIELD_GRIP_V = [0.0, 1.0]
BOLT_HANDS_ON_YIELD_RAMP_TIME = 1.0  # s, from 0 to 100% when a setting changes


class BoltHandsOnYield:
  def __init__(self, dt):
    self.share_step = dt / BOLT_HANDS_ON_YIELD_RAMP_TIME
    self.reset()

  def reset(self):
    """Lateral inactive: fade back in on re-engage."""
    self.push_share = 0.0
    self.hold_back_share = 0.0
    self.push = 0.0
    self.hold_back = 0.0
    self.driver = 0.0

  def update(self, push_percent, hold_back_percent, v_ego, setpoint, measurement, steering_torque):
    """Set this frame's yield weights. steering_torque is the car's driver torque (left +); the controller is right +."""
    self.push_share = rate_limit(float(np.clip(push_percent, 0, 100)) / 100, self.push_share,
                                 -self.share_step, self.share_step)
    self.hold_back_share = rate_limit(float(np.clip(hold_back_percent, 0, 100)) / 100, self.hold_back_share,
                                      -self.share_step, self.share_step)
    self.driver = -float(steering_torque)
    grip = np.interp(abs(self.driver), BOLT_HANDS_ON_YIELD_GRIP_BP, BOLT_HANDS_ON_YIELD_GRIP_V)
    gate = (np.interp(v_ego, BOLT_TURN_DAMPING_SPEED_BP, BOLT_TURN_DAMPING_SPEED_V) *
            np.interp(max(abs(setpoint), abs(measurement)), BOLT_TURN_DAMPING_LAT_ACCEL_BP, BOLT_TURN_DAMPING_LAT_ACCEL_V))
    self.push = float(self.push_share * grip * gate)
    self.hold_back = float(self.hold_back_share * grip * gate)

  def yield_error(self, error):
    """Push: scale down a tracking error (P and I) that would pull against the driver."""
    return error * (1 - self.push) if error * self.driver < 0 else error

  def yield_kd(self, kd):
    """Push: the wheel moving with the driver must not become a new damping wall."""
    return kd * (1 - self.push)

  def yield_output(self, output_torque):
    """Hold-back: scale down an output that pushes against the driver; GM's up-rate limit brings it back on release."""
    return output_torque * (1 - self.hold_back) if output_torque * self.driver < 0 else output_torque
