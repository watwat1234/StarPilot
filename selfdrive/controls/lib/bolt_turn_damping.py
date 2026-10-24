from collections import deque

import numpy as np

from opendbc.car import rate_limit
from openpilot.common.constants import CV
from openpilot.common.filter_simple import FirstOrderFilter

# Bolt 2022/23 slow tight turns swing the wheel back and forth (~1.6-2 s period, route 00000180--e85874451d):
# the low-speed P is ~3.3 at 15 mph, the torque map is ~5x steeper near zero output, and the PID has no D.
# This adds a D term on the rate of the tracking error, only at low speed and in real turns.
BOLT_TURN_DAMPING_MAX_KD = 1.0  # at 100% on the setting slider
BOLT_TURN_DAMPING_RAMP_TIME = 1.0  # s, from 0 to the 100% gain
BOLT_TURN_DAMPING_SPEED_BP = [20 * CV.MPH_TO_MS, 30 * CV.MPH_TO_MS]
BOLT_TURN_DAMPING_SPEED_V = [1.0, 0.0]
BOLT_TURN_DAMPING_LAT_ACCEL_BP = [0.3, 0.8]  # m/s^2
BOLT_TURN_DAMPING_LAT_ACCEL_V = [0.0, 1.0]
BOLT_TURN_DAMPING_MAX_ERROR_RATE = 3.0  # m/s^3, caps the push on fast planned moves the wheel lags behind
BOLT_TURN_DAMPING_RATE_FRAMES = 5  # the model runs at 20 Hz, so the setpoint steps every 5 control frames
BOLT_TURN_DAMPING_RATE_RC = 1 / (2 * np.pi * 2.0)  # s, passes the ~0.5 Hz swing


class BoltTurnDamping:
  def __init__(self, dt):
    self.rate_span = BOLT_TURN_DAMPING_RATE_FRAMES * dt
    self.ramp_step = dt * BOLT_TURN_DAMPING_MAX_KD / BOLT_TURN_DAMPING_RAMP_TIME
    self.setpoint_rate_filter = FirstOrderFilter(0.0, BOLT_TURN_DAMPING_RATE_RC, dt)
    self.measurement_rate_filter = FirstOrderFilter(0.0, BOLT_TURN_DAMPING_RATE_RC, dt)
    self.history = deque(maxlen=BOLT_TURN_DAMPING_RATE_FRAMES + 1)
    self.reset()

  def reset(self):
    """Lateral inactive: start from zero gain and no history on re-engage."""
    self.kd = 0.0
    self.history.clear()
    self.setpoint_rate_filter.x = 0.0
    self.measurement_rate_filter.x = 0.0

  def update(self, percent, v_ego, setpoint, measurement, steering_pressed):
    """Return (D gain, tracking-error rate). The gain ramps toward the setting's, so changing it mid-turn fades."""
    if not self.history:
      self.history.extend([(setpoint, measurement)] * BOLT_TURN_DAMPING_RATE_FRAMES)
    self.history.append((setpoint, measurement))
    old_setpoint, old_measurement = self.history[0]
    # both rates take the same span and filter, so they share the speed-change term (lat accel = curvature * v^2)
    # and the lag: a steady wheel through a speed change, or a wheel tracking the setpoint, gives zero.
    # The 5-frame span averages out the model's 20 Hz setpoint steps.
    setpoint_rate = self.setpoint_rate_filter.update((setpoint - old_setpoint) / self.rate_span)
    measurement_rate = self.measurement_rate_filter.update((measurement - old_measurement) / self.rate_span)
    error_rate = float(np.clip(setpoint_rate - measurement_rate, -BOLT_TURN_DAMPING_MAX_ERROR_RATE,
                               BOLT_TURN_DAMPING_MAX_ERROR_RATE))

    if steering_pressed:
      # D on wheel motion would resist the driver; drop it at once and fade back in after release
      self.kd = 0.0
      return 0.0, error_rate
    target = BOLT_TURN_DAMPING_MAX_KD * float(np.clip(percent, 0, 100)) / 100
    self.kd = rate_limit(target, self.kd, -self.ramp_step, self.ramp_step)
    speed_weight = np.interp(v_ego, BOLT_TURN_DAMPING_SPEED_BP, BOLT_TURN_DAMPING_SPEED_V)
    turn_weight = np.interp(max(abs(setpoint), abs(measurement)), BOLT_TURN_DAMPING_LAT_ACCEL_BP,
                            BOLT_TURN_DAMPING_LAT_ACCEL_V)
    return float(self.kd * speed_weight * turn_weight), error_rate
