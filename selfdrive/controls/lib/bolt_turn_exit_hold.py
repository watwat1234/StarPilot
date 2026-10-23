import numpy as np

from opendbc.car import rate_limit
from openpilot.common.constants import CV

# Bolt 2022/23 slow turn exits unwind early (route 00000181--e3ab2a9962): with the wheel a little past the easing
# plan, the low-speed P (and D) cancel the feedforward hold, the rack snaps the wheel toward center, the car drifts
# out of the lane and torque slams back in. This keeps the output from dropping below a share of the planned lateral
# accel in the planned direction, only at low speed and never while the driver holds the wheel.
BOLT_TURN_EXIT_HOLD_SPEED_BP = [20 * CV.MPH_TO_MS, 30 * CV.MPH_TO_MS]
BOLT_TURN_EXIT_HOLD_SPEED_V = [1.0, 0.0]
BOLT_TURN_EXIT_HOLD_SHARE_RAMP_TIME = 1.0  # s, from 0 to 100% when the setting changes
BOLT_TURN_EXIT_HOLD_RELEASE_RAMP_TIME = 0.25  # s, back to full after the driver lets go


class BoltTurnExitHold:
  def __init__(self, dt):
    self.share_step = dt / BOLT_TURN_EXIT_HOLD_SHARE_RAMP_TIME
    self.release_step = dt / BOLT_TURN_EXIT_HOLD_RELEASE_RAMP_TIME
    self.reset()

  def reset(self):
    """Lateral inactive: fade back in on re-engage."""
    self.share = 0.0
    self.release_weight = 0.0

  def update(self, hold_percent, v_ego, planned_lat_accel, output_lataccel, steering_pressed, neg_limit, pos_limit):
    """Return the output, raised to the floor when it asks for less turn than the floor in the planned direction."""
    self.share = rate_limit(float(np.clip(hold_percent, 0, 100)) / 100, self.share, -self.share_step, self.share_step)
    if steering_pressed:
      self.release_weight = 0.0
      return output_lataccel
    self.release_weight = min(self.release_weight + self.release_step, 1.0)
    speed_weight = np.interp(v_ego, BOLT_TURN_EXIT_HOLD_SPEED_BP, BOLT_TURN_EXIT_HOLD_SPEED_V)
    floor = float(np.clip(self.share * speed_weight * self.release_weight * planned_lat_accel, neg_limit, pos_limit))
    if floor > 0:
      return max(output_lataccel, floor)
    if floor < 0:
      return min(output_lataccel, floor)
    return output_lataccel
