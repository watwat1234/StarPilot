from types import SimpleNamespace
from unittest.mock import MagicMock

from openpilot.selfdrive.ui.mici.onroad.hud_renderer import HudRenderer
from openpilot.selfdrive.ui.onroad.starpilot import blind_spot_indicators
from openpilot.selfdrive.ui.onroad.starpilot.blind_spot_indicators import BlindSpotIndicators
from openpilot.selfdrive.ui.ui_state import ui_state


def _car_state(left_blindspot=False, right_blindspot=False):
  return SimpleNamespace(leftBlindspot=left_blindspot, rightBlindspot=right_blindspot)


def _make_indicators(monkeypatch):
  class FakeSM(dict):
    valid = {"carState": True}

  monkeypatch.setattr(blind_spot_indicators.gui_app, "texture", lambda *args, **kwargs: object())
  monkeypatch.setattr(blind_spot_indicators.gui_app, "_target_fps", 60)
  indicators = BlindSpotIndicators()
  monkeypatch.setattr(ui_state, "sm", FakeSM(carState=_car_state()))
  return indicators


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
