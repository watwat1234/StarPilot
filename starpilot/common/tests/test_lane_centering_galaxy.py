import json
import re
from pathlib import Path


_LAYOUT_PATH = Path(__file__).resolve().parents[1] / "assets/device_settings_layout.json"


def _sections():
  layout = json.loads(_LAYOUT_PATH.read_text(encoding="utf-8"))
  return {
    section["name"]: {param["key"]: param for param in section["params"]}
    for section in layout
  }


def test_lane_centering_is_only_in_galaxy_developer_section():
  sections = _sections()
  keys = {"LaneCentering", "LaneCenterOffset", "LaneCenteringPauseOnSignal", "LaneCenteringE2EAuthority",
          "LaneCenteringStrength"}

  assert keys <= sections["Developer"].keys()
  for name, params in sections.items():
    if name != "Developer":
      assert keys.isdisjoint(params)

  for key in keys:
    assert sections["Developer"][key]["settings_tier"] == "advanced"


def test_lane_centering_galaxy_controls():
  developer = _sections()["Developer"]
  centering = developer["LaneCentering"]
  offset = developer["LaneCenterOffset"]
  pause_on_signal = developer["LaneCenteringPauseOnSignal"]
  e2e_authority = developer["LaneCenteringE2EAuthority"]
  strength = developer["LaneCenteringStrength"]

  assert centering["ui_type"] == "toggle"
  assert centering["is_parent_toggle"] is True

  assert pause_on_signal["ui_type"] == "toggle"
  assert pause_on_signal["parent_key"] == "LaneCentering"

  assert offset["parent_key"] == "LaneCentering"
  assert offset["min"] == -0.3
  assert offset["max"] == 0.3
  assert offset["step"] == 0.01

  assert e2e_authority["parent_key"] == "LaneCentering"
  assert e2e_authority["min"] == 0.0
  assert e2e_authority["max"] == 1.0
  assert e2e_authority["step"] == 0.05
  assert e2e_authority["control"] == "slider"
  assert len(e2e_authority["description_steps"]) == 5

  assert strength["parent_key"] == "LaneCentering"
  assert strength["min"] == 0.5
  assert strength["max"] == 3.0
  assert strength["step"] == 0.05
  assert strength["unit"] == "x"
  assert strength["control"] == "slider"


def test_lane_centering_strength_range_matches_controller_and_toggle():
  from openpilot.selfdrive.controls.lib.lane_centering import _MAX_STRENGTH, _MIN_STRENGTH

  strength = _sections()["Developer"]["LaneCenteringStrength"]
  assert (strength["min"], strength["max"]) == (_MIN_STRENGTH, _MAX_STRENGTH)

  source = (Path(__file__).resolve().parents[1] / "starpilot_variables.py").read_text(encoding="utf-8")
  match = re.search(r'"LaneCenteringStrength", cast=float,[^)]*?min=([\d.]+), max=([\d.]+)', source)
  assert match is not None
  assert (float(match[1]), float(match[2])) == (_MIN_STRENGTH, _MAX_STRENGTH)
