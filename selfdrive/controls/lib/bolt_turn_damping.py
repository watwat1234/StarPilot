import numpy as np

from openpilot.common.constants import CV

# Bolt 2022/23 slow tight turns swing the wheel back and forth (~1.6-2 s period, route 00000180--e85874451d):
# the low-speed P is ~3.3 at 15 mph, the torque map is ~5x steeper near zero output, and the PID has no D.
# This adds a D term on the deviation from the planned motion, only at low speed and in real turns.
BOLT_TURN_DAMPING_KD = [0.0, 0.3, 0.6, 1.0]  # by level: Off / Low / Medium / High
BOLT_TURN_DAMPING_RAMP_TIME = 1.0  # s, from 0 to the highest level's gain
BOLT_TURN_DAMPING_SPEED_BP = [20 * CV.MPH_TO_MS, 30 * CV.MPH_TO_MS]
BOLT_TURN_DAMPING_SPEED_V = [1.0, 0.0]
BOLT_TURN_DAMPING_LAT_ACCEL_BP = [0.3, 0.8]  # m/s^2
BOLT_TURN_DAMPING_LAT_ACCEL_V = [0.0, 1.0]
BOLT_TURN_DAMPING_MAX_RATE = 6.0  # m/s^3, wider than MAX_LAT_JERK_UP so the swing peaks aren't cut


class BoltTurnDamping:
  def __init__(self, dt):
    self.ramp_step = dt * BOLT_TURN_DAMPING_KD[-1] / BOLT_TURN_DAMPING_RAMP_TIME
    self.kd = 0.0

  def update(self, level, v_ego, setpoint, measurement, steering_pressed):
    """Return the D gain; ramps toward the level's gain so switching mid-turn fades in or out."""
    target = BOLT_TURN_DAMPING_KD[int(np.clip(level, 0, len(BOLT_TURN_DAMPING_KD) - 1))]
    if steering_pressed:
      # D on wheel motion would resist the driver; drop it at once and fade back in after release
      self.kd = 0.0
      return 0.0
    self.kd = float(np.clip(target, self.kd - self.ramp_step, self.kd + self.ramp_step))
    speed_weight = np.interp(v_ego, BOLT_TURN_DAMPING_SPEED_BP, BOLT_TURN_DAMPING_SPEED_V)
    turn_weight = np.interp(max(abs(setpoint), abs(measurement)), BOLT_TURN_DAMPING_LAT_ACCEL_BP,
                            BOLT_TURN_DAMPING_LAT_ACCEL_V)
    return float(self.kd * speed_weight * turn_weight)


def get_bolt_turn_error_rate(desired_lateral_jerk, raw_measurement_rate):
  """Rate of the tracking error: zero while the wheel follows the planned motion."""
  return float(desired_lateral_jerk - np.clip(raw_measurement_rate, -BOLT_TURN_DAMPING_MAX_RATE,
                                              BOLT_TURN_DAMPING_MAX_RATE))
