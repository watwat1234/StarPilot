from __future__ import annotations

import time
import pyray as rl
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets import Widget
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.starpilot.common.experimental_state import (
  CEStatus,
  next_manual_ce_status,
  sync_manual_ce_state,
)


BRAKE_WHEEL_COLOR = rl.Color(255, 0, 0, 255)
ACCEL_WHEEL_COLOR = rl.Color(22, 127, 64, 255)
COMMAND_ACCEL_THRESHOLD = 0.05
FULL_TINT_ACCEL = 1.0  # m/s^2 at which the wheel reaches full red/green
PEDAL_MIN_INTENSITY = 0.15  # faint tint for any pedal press, even at steady speed
WHEEL_TINT_TAU = 0.3  # seconds


def _accel_magnitude(accel: float) -> float:
  return min(1.0, max(0.0, (accel - COMMAND_ACCEL_THRESHOLD) / (FULL_TINT_ACCEL - COMMAND_ACCEL_THRESHOLD)))


def get_wheel_pedal_intensity(pedal_feedback_enabled: bool, long_active: bool,
                              driver_braking: bool = False, gas_pressed: bool = False,
                              acceleration: float = 0.0, commanded_accel: float = 0.0,
                              commanded_gas: float = 0.0) -> float:
  """Signed pedal intensity in [-1, 1]: negative is braking, positive is accelerating.

  A driver pedal press always wins and scales with measured accel. Otherwise, only the
  longitudinal controller's command tints the wheel, so coasting stays neutral.
  """
  if not pedal_feedback_enabled:
    return 0.0
  if driver_braking:
    return -max(PEDAL_MIN_INTENSITY, _accel_magnitude(-acceleration))
  if gas_pressed:
    return max(PEDAL_MIN_INTENSITY, _accel_magnitude(acceleration))
  if not long_active:
    return 0.0

  signal = commanded_accel if abs(commanded_accel) > COMMAND_ACCEL_THRESHOLD else 0.0
  if commanded_gas > COMMAND_ACCEL_THRESHOLD:
    signal = max(signal, commanded_gas * FULL_TINT_ACCEL)

  magnitude = _accel_magnitude(abs(signal))
  return -magnitude if signal < 0 else magnitude


class WheelTintFader:
  """Eases the wheel tint between the mode color and red/green instead of snapping."""

  def __init__(self, fps: float):
    self._level = FirstOrderFilter(0.0, WHEEL_TINT_TAU, 1 / fps)

  def update(self, intensity: float, mode_tint: rl.Color | None) -> rl.Color | None:
    level = self._level.update(intensity)
    if abs(level) < 0.01:
      return mode_tint

    target = BRAKE_WHEEL_COLOR if level < 0 else ACCEL_WHEEL_COLOR
    base = mode_tint if mode_tint is not None else rl.Color(255, 255, 255, 255)
    mix = abs(level)
    return rl.Color(*(round(b + (t - b) * mix) for b, t in ((base.r, target.r), (base.g, target.g), (base.b, target.b))), 255)


class ExpButton(Widget):
  def __init__(self, button_size: int, icon_size: int):
    super().__init__()
    self._params = ui_state.ui_params
    self._experimental_mode: bool = False
    self._engageable: bool = False

    # State hold mechanism
    self._hold_duration = 2.0  # seconds
    self._held_mode: bool | None = None
    self._hold_end_time: float | None = None

    self._white_color: rl.Color = rl.Color(255, 255, 255, 255)
    self._black_bg: rl.Color = rl.Color(0, 0, 0, 166)
    self.wheel_tint: rl.Color | None = None
    self._tint_fader = WheelTintFader(gui_app.target_fps)
    self._txt_wheel: rl.Texture = gui_app.texture('icons/chffr_wheel.png', icon_size, icon_size)
    self._txt_exp: rl.Texture = gui_app.texture('icons/experimental.png', icon_size, icon_size)
    self._rect = rl.Rectangle(0, 0, button_size, button_size)

    self._steer_angle_filter = FirstOrderFilter(0.0, 0.1, 1 / gui_app.target_fps)
    self._bg_colors = {
      "disengaged": rl.Color(0, 0, 0, 166),
      "switchback": rl.Color(0x8b, 0x6c, 0xc5, 255),
      "aol": rl.Color(0x0a, 0xba, 0xb5, 255),
      "cem_disabled": rl.Color(0xff, 0xff, 0x00, 255),
      "experimental": rl.Color(0xda, 0x6f, 0x25, 255),
      "traffic": rl.Color(0xc9, 0x22, 0x31, 255),
    }
    self._bg_color = self._black_bg

    # Visibility controlled by HideSteeringWheel toggle
    self.set_visible(lambda: not (ui_state.starpilot_toggles.get("hide_steering_wheel", False) or
                                  self._params.get_bool("HideSteeringWheel")))

  def set_rect(self, rect: rl.Rectangle) -> None:
    self._rect.x, self._rect.y = rect.x, rect.y

  def _update_state(self) -> None:
    selfdrive_state = ui_state.sm["selfdriveState"]
    self._experimental_mode = selfdrive_state.experimentalMode
    self._engageable = selfdrive_state.engageable or selfdrive_state.enabled or ui_state.always_on_lateral_active

    # Smooth steering angle for rotating wheel
    car_state = ui_state.sm["carState"]
    self._steer_angle_filter.update(car_state.steeringAngleDeg)

    # Determine background color based on engagement state
    simple_mode = ui_state.starpilot_toggles.get("simple_mode", False)
    if simple_mode or self.is_pressed or not self._engageable:
      self._bg_color = self._bg_colors["disengaged"]
    elif ui_state.switchback_mode_enabled:
      self._bg_color = self._bg_colors["switchback"]
    elif ui_state.always_on_lateral_active:
      self._bg_color = self._bg_colors["aol"]
    elif ui_state.conditional_status == 1:
      self._bg_color = self._bg_colors["cem_disabled"]
    elif self._held_or_actual_mode():
      self._bg_color = self._bg_colors["experimental"]
    elif ui_state.traffic_mode_enabled:
      self._bg_color = self._bg_colors["traffic"]
    else:
      self._bg_color = self._bg_colors["disengaged"]

  def _handle_mouse_release(self, _):
    super()._handle_mouse_release(_)
    if self._is_toggle_allowed():
      if self._params.get_bool("ConditionalExperimental"):
        current_status = ui_state.params_memory.get_int("CEStatus", default=CEStatus["OFF"])
        override_value = next_manual_ce_status(current_status, self._experimental_mode)
        ui_state.params_memory.put_int("CEStatus", override_value)
        sync_manual_ce_state(self._params, override_value)
        self._held_mode = None
        self._hold_end_time = None
      else:
        new_mode = not self._experimental_mode
        self._params.put_bool("ExperimentalMode", new_mode)

        # Hold new state temporarily
        self._held_mode = new_mode
        self._hold_end_time = time.monotonic() + self._hold_duration

  def _render(self, rect: rl.Rectangle) -> None:
    center_x = int(self._rect.x + self._rect.width // 2)
    center_y = int(self._rect.y + self._rect.height // 2)

    self._white_color.a = 180 if self.is_pressed or not self._engageable else 255

    exp_mode = self._held_or_actual_mode()
    texture = self._txt_exp if exp_mode else self._txt_wheel
    color = self._white_color
    tint = None
    car_state = ui_state.sm["carState"]
    car_control = ui_state.sm["carControl"] if getattr(ui_state.sm, "valid", {}).get("carControl", False) else None
    actuators = getattr(car_control, "actuators", None)
    long_active = bool(getattr(car_control, "longActive", False))
    pedal_feedback_enabled = self._params.get_bool("PedalsOnUI")
    intensity = get_wheel_pedal_intensity(
      pedal_feedback_enabled,
      long_active,
      getattr(car_state, "brakePressed", False) or getattr(car_state, "regenBraking", False),
      getattr(car_state, "gasPressed", False),
      getattr(car_state, "aEgo", 0.0),
      getattr(actuators, "accel", 0.0),
      getattr(actuators, "gas", 0.0),
    )
    wheel_tint = self._tint_fader.update(intensity, self.wheel_tint)
    if wheel_tint is not None:
      tint = rl.Color(wheel_tint.r, wheel_tint.g, wheel_tint.b, self._white_color.a)

    rl.draw_circle(center_x, center_y, self._rect.width / 2, self._bg_color)
    if tint is not None:
      if exp_mode:
        # The experimental icon is already colored, so show the lateral mode
        # around it instead of obscuring the icon with a texture tint.
        radius = self._rect.width / 2
        rl.draw_ring(rl.Vector2(center_x, center_y), radius - 8, radius, 0, 360, 0, tint)
      else:
        color = tint

    rotating_wheel = ui_state.starpilot_toggles.get("rotating_wheel", False) or self._params.get_bool("RotatingWheel")
    if texture == self._txt_wheel and rotating_wheel:
      source_rect = rl.Rectangle(0, 0, texture.width, texture.height)
      dest_rect = rl.Rectangle(center_x, center_y, texture.width, texture.height)
      origin = rl.Vector2(texture.width / 2, texture.height / 2)
      rl.draw_texture_pro(texture, source_rect, dest_rect, origin, -self._steer_angle_filter.x, color)
    else:
      rl.draw_texture_ex(texture, rl.Vector2(center_x - texture.width / 2, center_y - texture.height / 2), 0.0, 1.0, color)

  def _held_or_actual_mode(self):
    now = time.monotonic()
    if self._hold_end_time and now < self._hold_end_time:
      return self._held_mode

    if self._hold_end_time and now >= self._hold_end_time:
      self._hold_end_time = self._held_mode = None

    return self._experimental_mode

  def _is_toggle_allowed(self):
    if self._params.get_bool("SafeMode"):
      return False

    if not self._params.get_bool("ExperimentalModeConfirmed"):
      return False

    # Mirror exp mode toggle using persistent car params
    return ui_state.experimental_mode_available
