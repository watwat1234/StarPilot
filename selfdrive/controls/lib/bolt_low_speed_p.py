"""Bolt 2022/23: scale the torque PID error down at low speed.

On tight, slow turn exits the low-speed P gain (up to ~23 per m/s^2 at 10 mph) drives the command into a full
reverse before the feedforward unwinds, then back into the turn: a correction swing. Scaling the error the PID sees
softens P and the integrator together. Friction and feedforward are not affected.

Experiment: wat-dev-notes/experiment/wat-bolt-experiment-low-speed-p. Set ENABLED = False to turn it off.
"""
import numpy as np

ENABLED = True

# vEgo (m/s) -> scale. 1.0 at creep speed and from ~29 mph up; 0.4 over ~7-20 mph. The 9-13 m/s ramp keeps the
# scaled P from jumping back up 2.3x over 2 mph at ~28 mph (with 12-13 it rose 1.4 -> 3.2 per m/s^2).
SCALE_BP = [2.0, 3.0, 9.0, 13.0]
SCALE_V = [1.0, 0.4, 0.4, 1.0]
MIN_SCALE = 0.3


def get_bolt_low_speed_p_scale(v_ego: float) -> float:
  if not ENABLED:
    return 1.0
  return float(np.clip(np.interp(v_ego, SCALE_BP, SCALE_V), MIN_SCALE, 1.0))
