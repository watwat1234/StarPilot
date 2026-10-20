from types import SimpleNamespace
from unittest.mock import MagicMock

from openpilot.selfdrive.ui.mici.onroad.hud_renderer import HudRenderer
from openpilot.selfdrive.ui.onroad.starpilot import blind_spot_indicators
from openpilot.selfdrive.ui.onroad.starpilot.blind_spot_indicators import BlindSpotIndicators
from openpilot.selfdrive.ui.ui_state import ui_state


def _car_state(left_blindspot=False, right_blindspot=False):
  return SimpleNamespace(leftBlindspot=left_blindspot, rightBlindspot=right_blindspot)


def _make_indicators(monkeypatch, v_asm=(False, False), v_asm_enabled=True):
  class FakeSM(dict):
    valid = {"carState": True}

  monkeypatch.setattr(blind_spot_indicators.gui_app, "texture", lambda *args, **kwargs: SimpleNamespace(width=108))
  monkeypatch.setattr(blind_spot_indicators.gui_app, "_target_fps", 60)
  indicators = BlindSpotIndicators()
  monkeypatch.setattr(ui_state, "sm", FakeSM(carState=_car_state()))
  monkeypatch.setattr(ui_state, "starpilot_toggles", {"v_asm_enabled": v_asm_enabled})
  monkeypatch.setattr(blind_spot_indicators, "get_fresh_vasm_state", lambda _memory: v_asm)
  return indicators


def _settle(indicators):
  for _ in range(200):
    indicators.update()


def test_alpha_filter_targets_follow_blindspot_signal(monkeypatch):
  indicators = _make_indicators(monkeypatch)

  ui_state.sm["carState"] = _car_state(left_blindspot=True)
  for _ in range(200):
    indicators.update()
  assert indicators._blind_spot_left_alpha_filter.x > 0.99
  assert indicators._blind_spot_right_alpha_filter.x < 0.01

  ui_state.sm["carState"] = _car_state()
  for _ in range(200):
    indicators.update()
  assert indicators._blind_spot_left_alpha_filter.x < 0.01


def test_detected_reflects_alpha_filters(monkeypatch):
  indicators = _make_indicators(monkeypatch)

  assert not indicators.detected

  ui_state.sm["carState"] = _car_state(right_blindspot=True)
  for _ in range(200):
    indicators.update()
  assert indicators.detected


def test_render_no_ops_when_alpha_is_zero(monkeypatch):
  indicators = _make_indicators(monkeypatch)

  calls = []
  monkeypatch.setattr(blind_spot_indicators.rl, "draw_texture_ex", lambda *args: calls.append(args))

  indicators.render(blind_spot_indicators.rl.Rectangle(0, 0, 800, 600))

  assert calls == []


def test_vasm_only_lights_cyan_not_orange(monkeypatch):
  indicators = _make_indicators(monkeypatch, v_asm=(True, False))

  _settle(indicators)

  assert indicators._vasm_left_alpha_filter.x > 0.99
  assert indicators._blind_spot_left_alpha_filter.x < 0.01
  assert indicators._vasm_right_alpha_filter.x < 0.01
  assert indicators.detected


def test_stock_bsm_wins_over_vasm_on_same_side(monkeypatch):
  indicators = _make_indicators(monkeypatch, v_asm=(False, True))
  ui_state.sm["carState"] = _car_state(right_blindspot=True)

  _settle(indicators)

  assert indicators._blind_spot_right_alpha_filter.x > 0.99
  assert indicators._vasm_right_alpha_filter.x < 0.01


def test_vasm_ignored_when_disabled(monkeypatch):
  indicators = _make_indicators(monkeypatch, v_asm=(True, True), v_asm_enabled=False)

  _settle(indicators)

  assert not indicators.detected


def test_render_draws_cyan_texture_for_vasm_only(monkeypatch):
  indicators = _make_indicators(monkeypatch, v_asm=(True, False))
  _settle(indicators)

  calls = []
  monkeypatch.setattr(blind_spot_indicators.rl, "draw_texture_ex", lambda *args: calls.append(args))

  indicators.render(blind_spot_indicators.rl.Rectangle(0, 0, 800, 600))

  assert [call[0] for call in calls] == [indicators._txt_vasm_left]


def test_render_crossfade_keeps_right_icon_opaque_with_cyan_on_top(monkeypatch):
  indicators = _make_indicators(monkeypatch)
  indicators._blind_spot_right_alpha_filter.x = 0.5
  indicators._vasm_right_alpha_filter.x = 0.5

  calls = []
  monkeypatch.setattr(blind_spot_indicators.rl, "draw_texture_ex", lambda *args: calls.append(args))

  indicators.render(blind_spot_indicators.rl.Rectangle(0, 0, 800, 600))

  assert [call[0] for call in calls] == [indicators._txt_blind_spot_right, indicators._txt_vasm_right]
  orange_alpha = calls[0][4].a / 255
  cyan_alpha = calls[1][4].a / 255
  assert cyan_alpha + orange_alpha * (1 - cyan_alpha) > 0.98
  right_x = 800 - blind_spot_indicators.BLIND_SPOT_MARGIN_X - 108
  assert [call[1].x for call in calls] == [right_x, right_x]


def _make_hud_renderer():
  renderer = object.__new__(HudRenderer)
  renderer._rect = blind_spot_indicators.rl.Rectangle(0, 0, 800, 600)
  renderer._blind_spot_indicators = MagicMock()
  return renderer


def test_render_blind_spot_icons_respects_param_off(monkeypatch):
  renderer = _make_hud_renderer()
  monkeypatch.setattr(ui_state.ui_params, "get_bool", lambda key, default=False: False)

  renderer.render_blind_spot_icons()

  renderer._blind_spot_indicators.render.assert_not_called()


def test_render_blind_spot_icons_respects_param_on(monkeypatch):
  renderer = _make_hud_renderer()
  monkeypatch.setattr(ui_state.ui_params, "get_bool", lambda key, default=False: True)

  renderer.render_blind_spot_icons()

  renderer._blind_spot_indicators.render.assert_called_once_with(renderer._rect)
