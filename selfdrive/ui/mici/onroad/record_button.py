import time
import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.starpilot.common.screen_recorder import ScreenRecording
from openpilot.system.ui.lib.application import FontWeight, gui_app, MousePos
from openpilot.system.ui.widgets import Widget


class RecordButton(Widget):
  """Tap to start/stop an onroad screen recording. Shown when the ScreenRecorder toggle is on."""
  MARGIN = 14
  RADIUS = 16
  TOUCH_PAD = 12
  MAX_TAP_TRAVEL = 24

  def __init__(self):
    super().__init__()
    self._font = gui_app.font(FontWeight.SEMI_BOLD)
    self._recording = ScreenRecording(gui_app)
    self._button_rect = rl.Rectangle()
    self._pressed = False
    self._press_pos: MousePos | None = None
    self._interacting = False

  def interacting(self):
    interacting, self._interacting = self._interacting, False
    return interacting

  def available(self) -> bool:
    if not (ui_state.started and gui_app.can_record):
      return False
    toggles = getattr(ui_state, "starpilot_toggles", None)
    if toggles is not None and "screen_recorder" in toggles:
      return bool(toggles.get("screen_recorder"))
    return ui_state.ui_params.get_bool("ScreenRecorder")

  def stop_if_recording(self):
    if self._recording.is_recording:
      self._recording.stop()

  def _hit(self, pos: MousePos) -> bool:
    pad = self.TOUCH_PAD
    r = self._button_rect
    return rl.check_collision_point_rec(pos, rl.Rectangle(r.x - pad, r.y - pad, r.width + 2 * pad, r.height + 2 * pad))

  def _render(self, rect: rl.Rectangle):
    if not self.available():
      self._button_rect = rl.Rectangle()
      # Never leave a recording running once the button is gone (offroad / toggle off)
      self.stop_if_recording()
      return

    d = self.RADIUS * 2
    self._button_rect = rl.Rectangle(rect.x + self.MARGIN, rect.y + self.MARGIN, d, d)
    cx, cy = int(self._button_rect.x + self.RADIUS), int(self._button_rect.y + self.RADIUS)

    rl.draw_circle(cx, cy, self.RADIUS, rl.Color(0, 0, 0, 140))
    if self._recording.is_recording:
      pulse = 0.6 + 0.4 * (1 if int(time.monotonic() * 2) % 2 == 0 else 0)
      rl.draw_circle(cx, cy, self.RADIUS - 6, rl.Color(230, 40, 40, int(255 * pulse)))
      elapsed = int(time.monotonic() - self._recording.started_at)
      text = f"{elapsed // 60}:{elapsed % 60:02d}"
      rl.draw_text_ex(self._font, text, rl.Vector2(self._button_rect.x + d + 8, cy - 10), 20, 0, rl.Color(255, 255, 255, 230))
    else:
      rl.draw_circle_lines(cx, cy, self.RADIUS - 6, rl.Color(255, 255, 255, 220))

  def _handle_mouse_press(self, mouse_pos: MousePos):
    self._pressed = self.available() and self._hit(mouse_pos)
    if self._pressed:
      self._press_pos = mouse_pos
      self._interacting = True

  def _handle_mouse_release(self, mouse_pos: MousePos):
    if self._pressed and self._press_pos is not None and self._hit(mouse_pos):
      travel = ((mouse_pos.x - self._press_pos.x) ** 2 + (mouse_pos.y - self._press_pos.y) ** 2) ** 0.5
      if travel <= self.MAX_TAP_TRAVEL:
        self._recording.toggle(time.monotonic())
    self._pressed = False
    self._press_pos = None
