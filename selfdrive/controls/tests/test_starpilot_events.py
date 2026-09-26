from pathlib import Path
from types import SimpleNamespace

from openpilot.selfdrive.selfdrived.events import ET, StarPilotEventName
from openpilot.starpilot.controls.lib.starpilot_events import StarPilotEvents


class FakeSM(dict):
  frame = 0


class DummyThemeManager:
  def update_wheel_image(self, *args, **kwargs):
    pass


def make_toggles(**overrides):
  defaults = {
    "current_holiday_theme": "stock",
    "green_light_alert": True,
    "is_metric": False,
    "lead_departing_alert": True,
    "nnff": False,
    "random_events": False,
    "speed_limit_changed_alert": False,
    "startup_alert_top": "",
    "startup_alert_bottom": "",
    "wheel_image": "stock",
  }
  defaults.update(overrides)
  return SimpleNamespace(**defaults)


def make_planner(*, lead_status: bool, lead_dRel: float, lead_vLead: float, standstill: bool,
                  model_stopped: bool = False, stop_light_detected: bool = False, tracking_lead: bool = False):
  # tracking_lead is deliberately kept False by default in these tests: both bugs this branch
  # fixes only showed up because the *old* code path required tracking_lead to be True (or, for
  # greenLight, False) at exactly the moment standstill kicked in, which almost never held. Setting
  # it False here and asserting the alert still fires proves the gate no longer depends on it.
  return SimpleNamespace(
    lead_one=SimpleNamespace(status=lead_status, dRel=lead_dRel, vLead=lead_vLead),
    tracking_lead=tracking_lead,
    model_stopped=model_stopped,
    starpilot_vcruise=SimpleNamespace(forcing_stop=False, slc=SimpleNamespace(speed_limit_changed_timer=1.0)),
    starpilot_cem=SimpleNamespace(stop_light_detected=stop_light_detected),
    lateral_acceleration=0.0,
    params_memory=SimpleNamespace(get_bool=lambda *a, **k: False, put_bool=lambda *a, **k: None),
    params=SimpleNamespace(get=lambda *a, **k: None),
  )


def make_sm(*, standstill: bool, gear_shifter="drive"):
  sm = FakeSM({
    "selfdriveState": SimpleNamespace(enabled=True, alertType="", alertText1="", alertText2=""),
    "starpilotSelfdriveState": SimpleNamespace(alertText1="", alertText2="", alertSound=0),
    "deviceState": SimpleNamespace(started=True),
    "carControl": SimpleNamespace(actuators=SimpleNamespace(accel=0.0)),
    "carState": SimpleNamespace(standstill=standstill, gearShifter=gear_shifter, vEgo=0.0, vCruise=0.0, vCruiseCluster=0.0),
    "starpilotCarState": SimpleNamespace(alwaysOnLateralAllowed=False, trafficModeEnabled=False),
    "starpilotModelV2": SimpleNamespace(turnDirection=0),
  })
  return sm


def make_events(planner):
  return StarPilotEvents(planner, Path("/tmp/nonexistent-error-log"), DummyThemeManager())


def test_lead_departing_fires_without_active_tracking_lead():
  # tracking_lead is False here (the frozen, usually-False value at a real stop) -- the fix
  # gates on lead_one.status instead, so the alert must still fire.
  planner = make_planner(lead_status=True, lead_dRel=10.0, lead_vLead=0.0, standstill=True, tracking_lead=False)
  events = make_events(planner)
  toggles = make_toggles()

  events.update(True, 0.0, make_sm(standstill=True), toggles)
  assert StarPilotEventName.leadDeparting not in events.events.names

  planner.lead_one.dRel = 12.0
  planner.lead_one.vLead = 1.5
  events.update(True, 0.0, make_sm(standstill=True), toggles)

  assert StarPilotEventName.leadDeparting in events.events.names


def test_lead_departing_does_not_fire_without_a_lead():
  planner = make_planner(lead_status=False, lead_dRel=float("inf"), lead_vLead=0.0, standstill=True)
  events = make_events(planner)

  events.update(True, 0.0, make_sm(standstill=True), make_toggles())
  planner.lead_one.dRel = 5.0
  events.update(True, 0.0, make_sm(standstill=True), make_toggles())

  assert StarPilotEventName.leadDeparting not in events.events.names


def test_lead_departing_respects_toggle():
  planner = make_planner(lead_status=True, lead_dRel=10.0, lead_vLead=0.0, standstill=True)
  events = make_events(planner)
  toggles = make_toggles(lead_departing_alert=False)

  events.update(True, 0.0, make_sm(standstill=True), toggles)
  planner.lead_one.dRel = 12.0
  planner.lead_one.vLead = 1.5
  events.update(True, 0.0, make_sm(standstill=True), toggles)

  assert StarPilotEventName.leadDeparting not in events.events.names


def test_green_light_fires_without_active_tracking_lead():
  # No lead present (lead_one.status False) -- the old `not tracking_lead` gate happened to work
  # here by coincidence; the fix (`not lead_one.status`) is the direct, live check.
  planner = make_planner(lead_status=False, lead_dRel=float("inf"), lead_vLead=0.0, standstill=True,
                          model_stopped=True, stop_light_detected=True)
  events = make_events(planner)
  toggles = make_toggles()

  # Frame 1: still stopped for the light -- latches stopped_for_light from stop_light_detected.
  events.update(True, 0.0, make_sm(standstill=True), toggles)
  assert StarPilotEventName.greenLight not in events.events.names
  assert events.stopped_for_light is True

  # Frame 2: light turns green (model_stopped goes False) while still in standstill.
  planner.model_stopped = False
  events.update(True, 0.0, make_sm(standstill=True), toggles)

  assert StarPilotEventName.greenLight in events.events.names


def test_green_light_does_not_fire_behind_a_lead():
  planner = make_planner(lead_status=True, lead_dRel=10.0, lead_vLead=0.0, standstill=True,
                          model_stopped=True, stop_light_detected=True)
  events = make_events(planner)
  toggles = make_toggles()

  events.update(True, 0.0, make_sm(standstill=True), toggles)
  planner.model_stopped = False
  events.update(True, 0.0, make_sm(standstill=True), toggles)

  assert StarPilotEventName.greenLight not in events.events.names
