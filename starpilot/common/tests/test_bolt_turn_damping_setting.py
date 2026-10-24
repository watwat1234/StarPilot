import json
import re
from pathlib import Path

import pytest

from openpilot.starpilot.common import starpilot_variables as spv
from openpilot.starpilot.common.safe_mode import SAFE_MODE_MANAGED_KEYS

_ROOT = Path(__file__).resolve().parents[3]
_LAYOUT_PATH = _ROOT / "starpilot/common/assets/device_settings_layout.json"


def _setting():
  layout = json.loads(_LAYOUT_PATH.read_text(encoding="utf-8"))
  found = [(section["name"], param) for section in layout for param in section["params"]
           if param["key"] == "BoltTurnDamping"]
  assert len(found) == 1
  return found[0]


def test_layout_entry_is_a_percent_slider():
  section, setting = _setting()
  assert section == "Lateral (Steering)"
  assert (setting["data_type"], setting["ui_type"], setting["control"]) == ("int", "numeric", "slider")
  assert (setting["min"], setting["max"], setting["step"], setting["unit"]) == (0, 100, 10, "%")
  assert "options" not in setting
  assert setting["vehicle_makes"] == ["Chevrolet"]
  assert setting["settings_tier"] == "advanced"
  assert "parent_key" not in setting
  assert not setting.get("galaxy_only")


def test_safe_mode_and_galaxy_manage_the_key():
  assert "BoltTurnDamping" in SAFE_MODE_MANAGED_KEYS
  galaxy = (_ROOT / "starpilot/system/the_galaxy/the_galaxy.py").read_text(encoding="utf-8")
  lateral_keys = re.search(r"_TROUBLESHOOT_ADVANCED_LATERAL_KEYS = \[(.*?)\]", galaxy, re.S)
  assert lateral_keys is not None and '"BoltTurnDamping"' in lateral_keys[1]


def test_param_key_defaults_off():
  keys = (_ROOT / "common/params_keys.h").read_text(encoding="utf-8")
  assert '{"BoltTurnDamping", {PERSISTENT, INT, "0", "0", 2}},' in keys


def test_toggle_cars_match_the_controller_gate():
  from openpilot.selfdrive.controls.lib.latcontrol_vehicle_tunes import BOLT_2022_2023_CARS
  assert spv.BOLT_TURN_DAMPING_CARS == set(BOLT_2022_2023_CARS)


class _FakeParams:
  def __init__(self, ints):
    self.ints = ints

  def get(self, key):
    return self.ints.get(key)


def _read(stored, is_bolt):
  variables = object.__new__(spv.StarPilotVariables)
  variables.params = _FakeParams({} if stored is None else {"BoltTurnDamping": stored})
  variables.default_values = {"BoltTurnDamping": "0"}
  return variables.get_value("BoltTurnDamping", cast=int, condition=is_bolt, default=0, min=0, max=100)


@pytest.mark.parametrize("stored, is_bolt, expected", [
  (None, True, 0),   # default off
  (60, True, 60),
  (60, False, 0),    # other cars always read 0
  (150, True, 100),  # clamped
  (-10, True, 0),
])
def test_toggle_read(stored, is_bolt, expected):
  assert _read(stored, is_bolt) == expected


def test_toggle_read_matches_source():
  source = (_ROOT / "starpilot/common/starpilot_variables.py").read_text(encoding="utf-8")
  assert re.search(r'toggle\.bolt_turn_damping = self\.get_value\(\s*"BoltTurnDamping", cast=int, ' +
                   r'condition=toggle\.car_model in BOLT_TURN_DAMPING_CARS, default=0, min=0, max=100,', source)


def _exit_hold_setting():
  layout = json.loads(_LAYOUT_PATH.read_text(encoding="utf-8"))
  found = [(section["name"], param) for section in layout for param in section["params"]
           if param["key"] == "BoltTurnExitHold"]
  assert len(found) == 1
  return found[0]


def test_exit_hold_layout_entry_is_a_percent_slider_after_smooth_slow_turns():
  section, setting = _exit_hold_setting()
  assert section == "Lateral (Steering)"
  assert (setting["data_type"], setting["ui_type"], setting["control"]) == ("int", "numeric", "slider")
  assert (setting["min"], setting["max"], setting["step"], setting["unit"]) == (0, 100, 10, "%")
  assert setting["vehicle_makes"] == ["Chevrolet"]
  assert setting["settings_tier"] == "advanced"
  assert "parent_key" not in setting
  layout = json.loads(_LAYOUT_PATH.read_text(encoding="utf-8"))
  keys = [param["key"] for section in layout for param in section["params"]]
  assert keys.index("BoltTurnExitHold") == keys.index("BoltTurnDamping") + 1


def test_exit_hold_key_defaults_off_and_is_managed():
  keys = (_ROOT / "common/params_keys.h").read_text(encoding="utf-8")
  assert '{"BoltTurnExitHold", {PERSISTENT, INT, "0", "0", 2}},' in keys
  assert "BoltTurnExitHold" in SAFE_MODE_MANAGED_KEYS
  galaxy = (_ROOT / "starpilot/system/the_galaxy/the_galaxy.py").read_text(encoding="utf-8")
  lateral_keys = re.search(r"_TROUBLESHOOT_ADVANCED_LATERAL_KEYS = \[(.*?)\]", galaxy, re.S)
  assert lateral_keys is not None and '"BoltTurnExitHold"' in lateral_keys[1]


@pytest.mark.parametrize("stored, is_bolt, expected", [
  (None, True, 0),   # default off
  (60, True, 60),
  (60, False, 0),    # other cars always read 0
  (150, True, 100),  # clamped
  (-10, True, 0),
])
def test_exit_hold_toggle_read(stored, is_bolt, expected):
  variables = object.__new__(spv.StarPilotVariables)
  variables.params = _FakeParams({} if stored is None else {"BoltTurnExitHold": stored})
  variables.default_values = {"BoltTurnExitHold": "0"}
  assert variables.get_value("BoltTurnExitHold", cast=int, condition=is_bolt, default=0, min=0, max=100) == expected


def test_exit_hold_toggle_read_matches_source():
  source = (_ROOT / "starpilot/common/starpilot_variables.py").read_text(encoding="utf-8")
  assert re.search(r'toggle\.bolt_turn_exit_hold = self\.get_value\(\s*"BoltTurnExitHold", cast=int, ' +
                   r'condition=toggle\.car_model in BOLT_TURN_DAMPING_CARS, default=0, min=0, max=100,', source)
