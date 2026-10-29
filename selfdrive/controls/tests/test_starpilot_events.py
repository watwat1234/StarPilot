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
                  model_stopped: bool = False, stop_light_detected: bool = False, tracking_lead: bool = False,
                  lead_modelProb: float = 1.0):
  # tracking_lead is deliberately kept False by default in these tests: both bugs this branch
  # fixes only showed up because the *old* code path required tracking_lead to be True (or, for
  # greenLight, False) at exactly the moment standstill kicked in, which almost never held. Setting
  # it False here and asserting the alert still fires proves the gate no longer depends on it.
  return SimpleNamespace(
    lead_one=SimpleNamespace(status=lead_status, dRel=lead_dRel, vLead=lead_vLead, modelProb=lead_modelProb),
    tracking_lead=tracking_lead,
    model_stopped=model_stopped,
    starpilot_vcruise=SimpleNamespace(forcing_stop=False, slc=SimpleNamespace(speed_limit_changed_timer=1.0, limit_change_started=False)),
    starpilot_cem=SimpleNamespace(stop_light_detected=stop_light_detected),
    lateral_acceleration=0.0,
    params_memory=SimpleNamespace(get_bool=lambda *a, **k: False, put_bool=lambda *a, **k: None),
    params=SimpleNamespace(get=lambda *a, **k: None),
  )


def make_sm(*, standstill: bool, gear_shifter="drive", gas_pressed: bool = False):
  sm = FakeSM({
    "selfdriveState": SimpleNamespace(enabled=True, alertType="", alertText1="", alertText2=""),
    "starpilotSelfdriveState": SimpleNamespace(alertText1="", alertText2="", alertSound=0),
    "deviceState": SimpleNamespace(started=True),
    "carControl": SimpleNamespace(actuators=SimpleNamespace(accel=0.0)),
    "carParams": SimpleNamespace(carFingerprint="MOCK"),
    "carState": SimpleNamespace(standstill=standstill, gearShifter=gear_shifter, gasPressed=gas_pressed, vEgo=0.0, vCruise=0.0,
                                vCruiseCluster=0.0),
    "starpilotCarState": SimpleNamespace(alwaysOnLateralAllowed=False, trafficModeEnabled=False),
    "starpilotModelV2": SimpleNamespace(turnDirection=0),
  })
  return sm


def make_events(planner):
  return StarPilotEvents(planner, Path("/tmp/nonexistent-error-log"), DummyThemeManager())


def pull_away(events, planner, toggles, distances, v_lead=1.5, **sm_kwargs):
  # Steps the lead through distances one update at a time (small steps, like a real car pulling away)
  # and returns whether leadDeparting fired on any of them.
  fired = False
  for d_rel in distances:
    planner.lead_one.dRel = d_rel
    planner.lead_one.vLead = v_lead
    events.update(True, 0.0, make_sm(standstill=True, **sm_kwargs), toggles)
    fired |= StarPilotEventName.leadDeparting in events.events.names
  return fired


def test_lead_departing_fires_without_active_tracking_lead():
  # tracking_lead is False here (the frozen, usually-False value at a real stop) -- the fix
  # gates on lead_one.status instead, so the alert must still fire.
  planner = make_planner(lead_status=True, lead_dRel=10.0, lead_vLead=0.0, standstill=True, tracking_lead=False)
  events = make_events(planner)
  toggles = make_toggles()

  events.update(True, 0.0, make_sm(standstill=True), toggles)
  assert StarPilotEventName.leadDeparting not in events.events.names

  assert pull_away(events, planner, toggles, [10.5, 11.0, 11.5])


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

  assert not pull_away(events, planner, toggles, [10.5, 11.0, 11.5])


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


def test_lead_departing_does_not_fire_while_gas_pressed():
  planner = make_planner(lead_status=True, lead_dRel=10.0, lead_vLead=0.0, standstill=True)
  events = make_events(planner)
  toggles = make_toggles()

  events.update(True, 0.0, make_sm(standstill=True), toggles)

  assert not pull_away(events, planner, toggles, [10.5, 11.0, 11.5], gas_pressed=True)


def test_green_light_does_not_fire_while_gas_pressed():
  planner = make_planner(lead_status=False, lead_dRel=float("inf"), lead_vLead=0.0, standstill=True,
                          model_stopped=True, stop_light_detected=True)
  events = make_events(planner)
  toggles = make_toggles()

  events.update(True, 0.0, make_sm(standstill=True), toggles)
  planner.model_stopped = False
  events.update(True, 0.0, make_sm(standstill=True, gas_pressed=True), toggles)

  assert StarPilotEventName.greenLight not in events.events.names


def test_lead_departing_ignores_lead_the_camera_does_not_see():
  # Cross traffic at an intersection: a radar track with almost no camera support.
  planner = make_planner(lead_status=True, lead_dRel=5.0, lead_vLead=0.0, standstill=True, lead_modelProb=0.3)
  events = make_events(planner)
  toggles = make_toggles()

  events.update(True, 0.0, make_sm(standstill=True), toggles)

  assert not pull_away(events, planner, toggles, [5.5, 6.0, 6.5, 7.0])


def test_lead_departing_ignores_jump_to_another_object():
  # The driveway gate swinging open: the "lead" jumps from the gate to whatever is beyond it.
  planner = make_planner(lead_status=True, lead_dRel=5.0, lead_vLead=0.0, standstill=True)
  events = make_events(planner)
  toggles = make_toggles()

  events.update(True, 0.0, make_sm(standstill=True), toggles)

  assert not pull_away(events, planner, toggles, [17.0, 17.5, 18.0, 18.5])


def test_lead_departing_needs_a_close_stop():
  planner = make_planner(lead_status=True, lead_dRel=12.0, lead_vLead=0.0, standstill=True)
  events = make_events(planner)
  toggles = make_toggles()

  events.update(True, 0.0, make_sm(standstill=True), toggles)

  assert not pull_away(events, planner, toggles, [12.5, 13.0, 13.5, 14.0])


def test_lead_departing_uses_closest_distance_in_the_stop():
  # A stray 8 m reading on the first frame, then the real lead at 3.8 m pulls away (Ioniq event #77):
  # the baseline must follow the closer reading, or the departure is missed.
  planner = make_planner(lead_status=True, lead_dRel=8.0, lead_vLead=0.0, standstill=True)
  events = make_events(planner)
  toggles = make_toggles()

  events.update(True, 0.0, make_sm(standstill=True), toggles)
  assert not pull_away(events, planner, toggles, [3.8, 3.8], v_lead=0.0)

  assert pull_away(events, planner, toggles, [4.3, 4.8, 5.3])


def test_lead_departing_baseline_resets_when_camera_drops():
  planner = make_planner(lead_status=True, lead_dRel=5.0, lead_vLead=0.0, standstill=True)
  events = make_events(planner)
  toggles = make_toggles()

  events.update(True, 0.0, make_sm(standstill=True), toggles)
  assert events.tracked_lead_distance == 5.0

  planner.lead_one.modelProb = 0.3
  assert not pull_away(events, planner, toggles, [5.5])
  assert events.tracked_lead_distance == 0

  # Camera back: the baseline restarts from the current distance, so the earlier 5 m doesn't count.
  planner.lead_one.modelProb = 1.0
  assert not pull_away(events, planner, toggles, [6.0, 6.5])
  assert events.tracked_lead_distance == 6.0
