import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.starpilot.common.vision_bsm import get_fresh_vasm_state

BLIND_SPOT_MARGIN_X = 20  # Distance from edge of screen
BLIND_SPOT_Y_OFFSET = 100  # Distance from top of screen


class BlindSpotIndicators:
  def __init__(self):
    self._txt_blind_spot_left: rl.Texture = gui_app.texture('icons_mici/onroad/blind_spot_left.png', 108, 128)
    self._txt_blind_spot_right: rl.Texture = gui_app.texture('icons_mici/onroad/blind_spot_left.png', 108, 128, flip_x=True)
    # Cyan copies for detections only V-ASM (vision) sees; the car's own BSM stays orange
    self._txt_vasm_left: rl.Texture = gui_app.texture('icons_mici/onroad/blind_spot_left_vasm.png', 108, 128)
    self._txt_vasm_right: rl.Texture = gui_app.texture('icons_mici/onroad/blind_spot_left_vasm.png', 108, 128, flip_x=True)

    self._blind_spot_left_alpha_filter = FirstOrderFilter(0, 0.15, 1 / gui_app.target_fps)
    self._blind_spot_right_alpha_filter = FirstOrderFilter(0, 0.15, 1 / gui_app.target_fps)
    self._vasm_left_alpha_filter = FirstOrderFilter(0, 0.15, 1 / gui_app.target_fps)
    self._vasm_right_alpha_filter = FirstOrderFilter(0, 0.15, 1 / gui_app.target_fps)

  def update(self) -> None:
    sm = ui_state.sm
    CS = sm['carState']

    vasm_left, vasm_right = False, False
    if ui_state.starpilot_toggles.get("v_asm_enabled", False):
      vasm_left, vasm_right = get_fresh_vasm_state(ui_state.params_memory)

    self._blind_spot_left_alpha_filter.update(1.0 if CS.leftBlindspot else 0.0)
    self._blind_spot_right_alpha_filter.update(1.0 if CS.rightBlindspot else 0.0)
    # Stock BSM wins when both fire on the same side
    self._vasm_left_alpha_filter.update(1.0 if vasm_left and not CS.leftBlindspot else 0.0)
    self._vasm_right_alpha_filter.update(1.0 if vasm_right and not CS.rightBlindspot else 0.0)

  @property
  def detected(self) -> bool:
    filters = (self._blind_spot_left_alpha_filter, self._blind_spot_right_alpha_filter,
               self._vasm_left_alpha_filter, self._vasm_right_alpha_filter)
    return any(f.x > 0.01 for f in filters)

  def render(self, rect: rl.Rectangle) -> None:
    self._draw_side(rect, self._txt_blind_spot_left, self._blind_spot_left_alpha_filter.x,
                    self._txt_vasm_left, self._vasm_left_alpha_filter.x, right_side=False)
    self._draw_side(rect, self._txt_blind_spot_right, self._blind_spot_right_alpha_filter.x,
                    self._txt_vasm_right, self._vasm_right_alpha_filter.x, right_side=True)

  @staticmethod
  def _draw_side(rect: rl.Rectangle, orange_texture: rl.Texture, orange_alpha: float,
                 cyan_texture: rl.Texture, cyan_alpha: float, right_side: bool) -> None:
    # Orange first, then cyan on top. The orange layer is boosted by what the cyan layer lets through, so the
    # combined opacity stays at the stronger of the two while the color crossfades instead of the icon dimming.
    orange_alpha = min(1.0, orange_alpha / max(1.0 - cyan_alpha, 1e-3))

    for texture, alpha in ((orange_texture, orange_alpha), (cyan_texture, cyan_alpha)):
      if alpha > 0.01:
        if right_side:
          pos_x = rect.x + rect.width - BLIND_SPOT_MARGIN_X - texture.width
        else:
          pos_x = rect.x + BLIND_SPOT_MARGIN_X
        pos_y = rect.y + BLIND_SPOT_Y_OFFSET
        color = rl.Color(255, 255, 255, int(255 * alpha))
        rl.draw_texture_ex(texture, rl.Vector2(int(pos_x), int(pos_y)), 0.0, 1.0, color)
