from types import SimpleNamespace

import pytest

from test_personality_profiles_api import _client, the_galaxy

ROUTES = [
  ("/api/system/reboot", "DoReboot"),
  ("/api/system/power_off", "DoShutdown"),
]
PARKED = {"IsOnroad": False, "IsOffroad": True}


def _power_client(monkeypatch, values, *, ignition=False, update_running=False):
  client, params = _client(monkeypatch, values)
  monkeypatch.setattr(the_galaxy, "_device_ignition_on", lambda: ignition)
  monkeypatch.setattr(the_galaxy, "_get_fast_update_state", lambda: {"running": update_running})
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
def test_device_power_refused_while_update_running(monkeypatch, route, _param):
  client, params, hardware_calls = _power_client(monkeypatch, PARKED, update_running=True)

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
  def __init__(self, pandas):
    self.pandas = pandas
    self.updated = {"pandaStates": False}

  def update(self, timeout):
    del timeout
    if self.pandas is not None:
      self.updated["pandaStates"] = True

  def __getitem__(self, key):
    assert key == "pandaStates"
    return self.pandas


@pytest.mark.parametrize("pandas,expected", [
  ([_panda()], False),
  ([_panda(ignition_line=True)], True),
  ([_panda(ignition_can=True)], True),
  ([_panda(ignition_line=True, panda_type="unknown")], False),
  (None, None),
])
def test_device_ignition_on_reads_panda_states(monkeypatch, pandas, expected):
  # The Galaxy test import stubs cereal.log; only the PandaType enum is needed here.
  monkeypatch.setattr(the_galaxy, "log", SimpleNamespace(PandaState=SimpleNamespace(PandaType=PANDA_TYPES)))
  monkeypatch.setattr(the_galaxy.messaging, "SubMaster", lambda services: _FakeSubMaster(pandas))

  assert the_galaxy._device_ignition_on(timeout_s=0.05) is expected
