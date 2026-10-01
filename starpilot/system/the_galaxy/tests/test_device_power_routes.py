from types import SimpleNamespace

import pytest

from test_personality_profiles_api import _client, the_galaxy

ROUTES = [
  ("/api/system/reboot", "DoReboot"),
  ("/api/system/power_off", "DoShutdown"),
]
PARKED = {"IsOnroad": False, "IsOffroad": True}


def _power_client(monkeypatch, values, *, ignition=False, update_state=None):
  client, params = _client(monkeypatch, values)
  probes = []

  def ignition_probe():
    probes.append(True)
    return ignition
  monkeypatch.setattr(the_galaxy, "_device_ignition_on", ignition_probe)
  state = {"running": False, "stage": "idle", **(update_state or {})}
  monkeypatch.setattr(the_galaxy, "_fast_update_state", state)
  params.ignition_probes = probes
  params.update_state = state
  calls = []
  monkeypatch.setattr(the_galaxy.HARDWARE, "reboot", lambda *args, **kwargs: calls.append("reboot"), raising=False)
  monkeypatch.setattr(the_galaxy.HARDWARE, "shutdown", lambda *args, **kwargs: calls.append("shutdown"), raising=False)
  return client, params, calls


@pytest.mark.parametrize("route,_param", ROUTES)
@pytest.mark.parametrize("device_state", [
  {"IsOnroad": True, "IsOffroad": False},
  {"IsOnroad": False, "IsOffroad": False},
  {"IsOnroad": True, "IsOffroad": True},
])
def test_device_power_requires_confirmed_offroad(monkeypatch, route, _param, device_state):
  client, params, hardware_calls = _power_client(monkeypatch, device_state)

  response = client.post(route)

  assert response.status_code == 403
  assert response.get_json()["success"] is False
  assert params.writes == []
  assert params.ignition_probes == []
  assert hardware_calls == []


@pytest.mark.parametrize("route,_param", ROUTES)
@pytest.mark.parametrize("ignition", [True, None])
def test_device_power_requires_ignition_confirmed_off(monkeypatch, route, _param, ignition):
  # IsOffroad stays set with the car running under ForceOffroad or a blocked startup.
  client, params, hardware_calls = _power_client(monkeypatch, PARKED, ignition=ignition)

  response = client.post(route)

  assert response.status_code == 403
  assert params.writes == []
  assert hardware_calls == []


@pytest.mark.parametrize("route,_param", ROUTES)
@pytest.mark.parametrize("update_state", [
  {"running": True, "stage": "downloading"},
  # _finish_update_and_reboot: running is already False while it waits to call HARDWARE.reboot().
  {"running": False, "stage": "rebooting"},
])
def test_device_power_refused_while_update_active(monkeypatch, route, _param, update_state):
  client, params, hardware_calls = _power_client(monkeypatch, PARKED, update_state=update_state)

  assert client.post(route).status_code == 409
  assert params.writes == []
  assert params.ignition_probes == []
  assert hardware_calls == []


@pytest.mark.parametrize("route,_param", ROUTES)
def test_device_power_rechecks_update_state_before_writing(monkeypatch, route, _param):
  # An update that starts while the ignition probe runs must still block the write.
  client, params, hardware_calls = _power_client(monkeypatch, PARKED)

  def probe_while_update_starts():
    params.update_state.update(running=True, stage="starting")
    return False
  monkeypatch.setattr(the_galaxy, "_device_ignition_on", probe_while_update_starts)

  assert client.post(route).status_code == 409
  assert params.writes == []
  assert hardware_calls == []


def test_reboot_parked_requests_manager_reboot(monkeypatch):
  client, params, hardware_calls = _power_client(monkeypatch, PARKED)

  response = client.post("/api/system/reboot")

  assert response.status_code == 200
  assert response.get_json()["success"] is True
  assert params.writes == [("DoReboot", True)]
  # manager performs the reboot after a clean process stop.
  assert hardware_calls == []


def test_power_off_parked_clears_pending_reboot_before_shutdown(monkeypatch):
  # manager checks DoReboot/DoUserReboot before DoShutdown, so a leftover reboot request would win.
  client, params, hardware_calls = _power_client(monkeypatch, {**PARKED, "DoReboot": True, "DoUserReboot": True})

  response = client.post("/api/system/power_off")

  assert response.status_code == 200
  assert response.get_json()["success"] is True
  assert params.writes == [("DoReboot", False), ("DoUserReboot", False), ("DoShutdown", True)]
  assert hardware_calls == []


@pytest.mark.parametrize("route,_param", ROUTES)
def test_device_power_rejects_get(monkeypatch, route, _param):
  client, params, _ = _power_client(monkeypatch, PARKED)

  assert client.get(route).status_code == 405
  assert params.writes == []


PANDA_TYPES = SimpleNamespace(unknown="unknown", tres="tres")


def _panda(ignition_line=False, ignition_can=False, panda_type="tres"):
  return SimpleNamespace(ignitionLine=ignition_line, ignitionCan=ignition_can, pandaType=panda_type)


class _FakeSubMaster:
  """Conflated pandaStates socket: `buffered` is what update(0) returns first, then `live` (None = no publisher)."""

  def __init__(self, live, *, buffered=None, valid=True):
    self.queue = [buffered] if buffered is not None else []
    self.live = live
    self.updated = {"pandaStates": False}
    self.valid = {"pandaStates": valid}
    self.data = None
    self.created = 0

  def update(self, timeout):
    msg = self.queue.pop(0) if self.queue else (self.live if timeout > 0 else None)
    self.updated["pandaStates"] = msg is not None
    if msg is not None:
      self.data = msg

  def __getitem__(self, key):
    assert key == "pandaStates"
    return self.data


def _ignition(monkeypatch, fake):
  # The Galaxy test import stubs cereal.log; only the PandaType enum is needed here.
  monkeypatch.setattr(the_galaxy, "log", SimpleNamespace(PandaState=SimpleNamespace(PandaType=PANDA_TYPES)))
  monkeypatch.setattr(the_galaxy, "_panda_states_sm", None)

  def make(services):
    assert services == ["pandaStates"]
    fake.created += 1
    return fake
  monkeypatch.setattr(the_galaxy.messaging, "SubMaster", make)
  return the_galaxy._device_ignition_on(timeout_s=0.05)


@pytest.mark.parametrize("pandas,expected", [
  ([_panda()], False),
  ([_panda(ignition_line=True)], True),
  ([_panda(ignition_can=True)], True),
  ([_panda(), _panda(ignition_can=True)], True),
  # Nothing confirms the car is off: fail closed.
  ([], None),
  ([_panda(panda_type="unknown")], None),
  ([_panda(ignition_line=True, panda_type="unknown")], None),
  (None, None),
])
def test_device_ignition_on_reads_panda_states(monkeypatch, pandas, expected):
  assert _ignition(monkeypatch, _FakeSubMaster(pandas)) is expected


def test_device_ignition_on_rejects_invalid_message(monkeypatch):
  assert _ignition(monkeypatch, _FakeSubMaster([_panda()], valid=False)) is None


def test_device_ignition_on_ignores_buffered_message(monkeypatch):
  # A message buffered since the last request can be arbitrarily old; only a new one counts.
  assert _ignition(monkeypatch, _FakeSubMaster(None, buffered=[_panda()])) is None
  assert _ignition(monkeypatch, _FakeSubMaster([_panda(ignition_line=True)], buffered=[_panda()])) is True


def test_device_ignition_on_reuses_one_subscriber(monkeypatch):
  # msgq never frees reader slots; a subscriber per request would eventually evict every pandaStates reader.
  fake = _FakeSubMaster([_panda()])
  assert _ignition(monkeypatch, fake) is False
  assert the_galaxy._device_ignition_on(timeout_s=0.05) is False
  assert the_galaxy._device_ignition_on(timeout_s=0.05) is False
  assert fake.created == 1
