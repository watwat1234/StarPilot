import numpy as np

from opendbc.car import rate_limit
from openpilot.common.constants import CV
from openpilot.common.filter_simple import FirstOrderFilter

# Bolt 2022/23 slow tight turns swing the wheel back and forth (~1.6-2 s period, route 00000180--e85874451d):
# the low-speed P is ~3.3 at 15 mph, the torque map is ~5x steeper near zero output, and the PID has no D.
# This adds a D term on the rate of the tracking error, only at low speed and in real turns.
BOLT_TURN_DAMPING_KD = [0.0, 0.3, 0.6, 1.0]  # by level: Off / Low / Medium / High
BOLT_TURN_DAMPING_RAMP_TIME = 1.0  # s, from 0 to the highest level's gain
BOLT_TURN_DAMPING_PRESSED_RAMP_TIME = 0.2  # s, fade-out while the driver presses (no output step, rides out flicker)
BOLT_TURN_DAMPING_SPEED_BP = [20 * CV.MPH_TO_MS, 30 * CV.MPH_TO_MS]
BOLT_TURN_DAMPING_SPEED_V = [1.0, 0.0]
BOLT_TURN_DAMPING_LAT_ACCEL_BP = [0.3, 0.8]  # m/s^2
BOLT_TURN_DAMPING_LAT_ACCEL_V = [0.0, 1.0]
BOLT_TURN_DAMPING_MAX_RATE = 6.0  # m/s^3, wider than MAX_LAT_JERK_UP so the swing peaks aren't cut
BOLT_TURN_DAMPING_RATE_RC = 1 / (2 * np.pi * 2.0)  # same filter as the controller's measurement_rate_filter


class BoltTurnDamping:
  def __init__(self, dt):
    self.dt = dt
    self.ramp_step = dt * BOLT_TURN_DAMPING_KD[-1] / BOLT_TURN_DAMPING_RAMP_TIME
    self.pressed_step = dt * BOLT_TURN_DAMPING_KD[-1] / BOLT_TURN_DAMPING_PRESSED_RAMP_TIME
    self.setpoint_rate_filter = FirstOrderFilter(0.0, BOLT_TURN_DAMPING_RATE_RC, dt)
    self.reset()

  def reset(self):
    """Lateral inactive: start from zero gain and no setpoint history on re-engage."""
    self.kd = 0.0
    self.prev_setpoint = None
    self.setpoint_rate_filter.x = 0.0

  def update(self, level, v_ego, setpoint, measurement, raw_measurement_rate, steering_pressed):
    """Return (D gain, tracking-error rate). The gain ramps toward the level's, so switching mid-turn fades."""
    if self.prev_setpoint is None:
      self.prev_setpoint = setpoint
    setpoint_rate = self.setpoint_rate_filter.update((setpoint - self.prev_setpoint) / self.dt)
    self.prev_setpoint = setpoint
    # both rates carry the same speed-change term (lat accel = curvature * v^2) and the same filter lag,
    # so a steady wheel through a speed change, or a wheel tracking the setpoint, gives zero
    error_rate = (np.clip(setpoint_rate, -BOLT_TURN_DAMPING_MAX_RATE, BOLT_TURN_DAMPING_MAX_RATE) -
                  np.clip(raw_measurement_rate, -BOLT_TURN_DAMPING_MAX_RATE, BOLT_TURN_DAMPING_MAX_RATE))

    target = BOLT_TURN_DAMPING_KD[int(np.clip(level, 0, len(BOLT_TURN_DAMPING_KD) - 1))]
    if steering_pressed:
      # D on wheel motion would resist the driver; fade it out quickly, then back in after release
      self.kd = rate_limit(0.0, self.kd, -self.pressed_step, self.pressed_step)
    else:
      self.kd = rate_limit(target, self.kd, -self.ramp_step, self.ramp_step)
    speed_weight = np.interp(v_ego, BOLT_TURN_DAMPING_SPEED_BP, BOLT_TURN_DAMPING_SPEED_V)
    turn_weight = np.interp(max(abs(setpoint), abs(measurement)), BOLT_TURN_DAMPING_LAT_ACCEL_BP,
                            BOLT_TURN_DAMPING_LAT_ACCEL_V)
    return float(self.kd * speed_weight * turn_weight), float(error_rate)
