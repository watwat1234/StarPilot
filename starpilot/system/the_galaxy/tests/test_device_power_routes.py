import pytest

from test_personality_profiles_api import _client, the_galaxy

ROUTES = [
  ("/api/system/reboot", "DoReboot"),
  ("/api/system/power_off", "DoShutdown"),
]


def _record_hardware(monkeypatch):
  calls = []
  monkeypatch.setattr(the_galaxy.HARDWARE, "reboot", lambda *args, **kwargs: calls.append("reboot"), raising=False)
  monkeypatch.setattr(the_galaxy.HARDWARE, "shutdown", lambda *args, **kwargs: calls.append("shutdown"), raising=False)
  return calls


@pytest.mark.parametrize("route,_param", ROUTES)
@pytest.mark.parametrize("device_state", [
  {"IsOnroad": True, "IsOffroad": False},
  {"IsOnroad": False, "IsOffroad": False},
  {"IsOnroad": True, "IsOffroad": True},
])
def test_device_power_requires_confirmed_offroad(monkeypatch, route, _param, device_state):
  client, params = _client(monkeypatch, device_state)
  hardware_calls = _record_hardware(monkeypatch)

  response = client.post(route)

  assert response.status_code == 403
  assert response.get_json()["success"] is False
  assert params.writes == []
  assert hardware_calls == []


@pytest.mark.parametrize("route,param", ROUTES)
def test_device_power_parked_requests_manager_action(monkeypatch, route, param):
  client, params = _client(monkeypatch, {"IsOnroad": False, "IsOffroad": True})
  hardware_calls = _record_hardware(monkeypatch)

  response = client.post(route)

  assert response.status_code == 200
  assert response.get_json()["success"] is True
  assert params.writes == [(param, True)]
  # manager performs the reboot/shutdown after a clean process stop.
  assert hardware_calls == []


@pytest.mark.parametrize("route,_param", ROUTES)
def test_device_power_rejects_get(monkeypatch, route, _param):
  client, params = _client(monkeypatch, {"IsOnroad": False, "IsOffroad": True})

  assert client.get(route).status_code == 405
  assert params.writes == []
