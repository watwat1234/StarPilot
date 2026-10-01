import queue
import threading
import time
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
  state = {"running": False, "stage": "idle", "finishedAt": 0.0, **(update_state or {})}
  monkeypatch.setattr(the_galaxy, "_fast_update_state", state)
  monkeypatch.setattr(the_galaxy, "_PANDA_FLASH_REBOOT_LOCK", threading.Lock())
  memory = {"FlashPanda": False}

  def memory_get_bool(key):
    if isinstance(memory.get(key), Exception):
      raise memory[key]
    return bool(memory.get(key))
  monkeypatch.setattr(the_galaxy, "params_memory", SimpleNamespace(get_bool=memory_get_bool))
  params.memory = memory

  # Record whether the update lock was held for each param write.
  write_locked = []
  put_bool = params.put_bool

  def locked_put_bool(key, value):
    write_locked.append(the_galaxy._fast_update_lock.locked())
    put_bool(key, value)
  monkeypatch.setattr(params, "put_bool", locked_put_bool)

  params.ignition_probes = probes
  params.update_state = state
  params.write_locked = write_locked
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
@pytest.mark.parametrize("update_kind", ["running", "rebooting", "rebooting_after_clock_jump"])
def test_device_power_refused_while_update_active(monkeypatch, route, _param, update_kind):
  # Timestamps are taken here, not at collection, so a slow suite can't age them out.
  update_state = {
    "running": {"running": True, "stage": "downloading"},
    # _finish_update_and_reboot: running is already False while it waits to call HARDWARE.reboot().
    "rebooting": {"running": False, "stage": "rebooting", "finishedAt": time.time(), "rebootingSinceMono": time.monotonic()},
    # The wall clock jumped (time sync) during the reboot notice; only the monotonic stamp counts.
    "rebooting_after_clock_jump": {"running": False, "stage": "rebooting", "finishedAt": time.time() - 86400.0,
                                   "rebootingSinceMono": time.monotonic()},
  }[update_kind]
  client, params, hardware_calls = _power_client(monkeypatch, PARKED, update_state=update_state)

  assert client.post(route).status_code == 409
  assert params.writes == []
  assert params.ignition_probes == []
  assert hardware_calls == []


@pytest.mark.parametrize("route,param", ROUTES)
def test_device_power_allowed_after_stuck_update_reboot(monkeypatch, route, param):
  # The update's own reboot never happened; the stale "rebooting" stage must not block recovery forever.
  stale = {"running": False, "stage": "rebooting", "finishedAt": time.time(),
           "rebootingSinceMono": time.monotonic() - the_galaxy._UPDATE_REBOOT_BUSY_SECONDS - 1}
  client, params, _ = _power_client(monkeypatch, PARKED, update_state=stale)

  assert client.post(route).status_code == 200
  assert params.writes[-1] == (param, True)


@pytest.mark.parametrize("route,_param", ROUTES)
@pytest.mark.parametrize("flash", ["lock", "param", "param_unreadable"])
def test_device_power_refused_during_panda_flash(monkeypatch, route, _param, flash):
  client, params, hardware_calls = _power_client(monkeypatch, PARKED)
  if flash == "param":
    params.memory["FlashPanda"] = True
  elif flash == "param_unreadable":
    params.memory["FlashPanda"] = OSError("params unavailable")  # fail closed, not a 500
  elif flash == "lock":
    the_galaxy._PANDA_FLASH_REBOOT_LOCK.acquire()

  try:
    assert client.post(route).status_code == 409
  finally:
    if flash == "lock":
      the_galaxy._PANDA_FLASH_REBOOT_LOCK.release()
  assert params.writes == []
  assert params.ignition_probes == []
  assert hardware_calls == []


@pytest.mark.parametrize("route,_param", ROUTES)
def test_device_power_rechecks_panda_flash_before_writing(monkeypatch, route, _param):
  # A panda flash that starts while the ignition probe runs must still block the write.
  client, params, hardware_calls = _power_client(monkeypatch, PARKED)

  def probe_while_flash_starts():
    params.memory["FlashPanda"] = True
    return False
  monkeypatch.setattr(the_galaxy, "_device_ignition_on", probe_while_flash_starts)

  assert client.post(route).status_code == 409
  assert params.writes == []
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
  # Written under the update lock, so an update cannot start between the recheck and the write.
  assert params.write_locked == [True]
  # manager performs the reboot after a clean process stop.
  assert hardware_calls == []


def test_power_off_parked_clears_pending_reboot_before_shutdown(monkeypatch):
  # manager checks DoReboot/DoUserReboot before DoShutdown, so a leftover reboot request would win.
  client, params, hardware_calls = _power_client(monkeypatch, {**PARKED, "DoReboot": True, "DoUserReboot": True})

  response = client.post("/api/system/power_off")

  assert response.status_code == 200
  assert response.get_json()["success"] is True
  assert params.writes == [("DoReboot", False), ("DoUserReboot", False), ("DoShutdown", True)]
  assert params.write_locked == [True, True, True]
  assert hardware_calls == []


@pytest.mark.parametrize("route,_param", ROUTES)
def test_device_power_rejects_get(monkeypatch, route, _param):
  client, params, _ = _power_client(monkeypatch, PARKED)

  assert client.get(route).status_code == 405
  assert params.writes == []


# --- ignition probe ---

PANDA_TYPES = SimpleNamespace(unknown="unknown", tres="tres")


def _panda(ignition_line=False, ignition_can=False, panda_type="tres"):
  return SimpleNamespace(ignitionLine=ignition_line, ignitionCan=ignition_can, pandaType=panda_type)


@pytest.fixture
def fake_log(monkeypatch):
  # The Galaxy test import stubs cereal.log; only the PandaType enum is needed here.
  monkeypatch.setattr(the_galaxy, "log", SimpleNamespace(PandaState=SimpleNamespace(PandaType=PANDA_TYPES)))


@pytest.mark.parametrize("valid,pandas,expected", [
  (True, [_panda()], False),
  (True, [_panda(ignition_line=True)], True),
  (True, [_panda(ignition_can=True)], True),
  (True, [_panda(), _panda(ignition_can=True)], True),
  # Nothing confirms the car is off: fail closed.
  (True, [], None),
  (True, [_panda(panda_type="unknown")], None),
  (True, [_panda(ignition_line=True, panda_type="unknown")], None),
  (False, [_panda()], None),  # pandad marks the message invalid when panda comms are unhealthy
])
def test_panda_states_ignition_rule(fake_log, valid, pandas, expected):
  assert the_galaxy._panda_states_ignition(valid, pandas) is expected


class _FakeSubMaster:
  """pandaStates subscriber fed by tests through publish(valid, pandas); can fail creation or an update."""

  def __init__(self):
    self.queue = queue.Queue()
    self.updated = {"pandaStates": False}
    self.valid = {"pandaStates": False}
    self.data = None
    self.created_on = []
    self.fail_creates = 0
    self.fail_updates = 0

  def __call__(self, services):
    assert services == ["pandaStates"]
    if self.fail_creates:
      self.fail_creates -= 1
      raise RuntimeError("no msgq reader slot")
    self.created_on.append(threading.current_thread())
    return self

  def publish(self, valid, pandas):
    self.queue.put((valid, pandas))

  def update(self, timeout):
    del timeout
    if self.fail_updates:
      self.fail_updates -= 1
      raise RuntimeError("recv failed")
    try:
      msg = self.queue.get(timeout=0.02)
    except queue.Empty:
      msg = None
    self.updated["pandaStates"] = msg is not None
    if msg is not None:
      self.valid["pandaStates"], self.data = msg

  def __getitem__(self, key):
    assert key == "pandaStates"
    return self.data


@pytest.fixture
def ignition_reader(monkeypatch, fake_log):
  monkeypatch.setattr(the_galaxy, "_panda_ignition_cond", threading.Condition())
  monkeypatch.setattr(the_galaxy, "_panda_ignition_seq", 0)
  monkeypatch.setattr(the_galaxy, "_panda_ignition_latest", None)
  monkeypatch.setattr(the_galaxy, "_panda_ignition_thread", None)
  stop = threading.Event()
  monkeypatch.setattr(the_galaxy, "_panda_ignition_stop", stop)
  fake = _FakeSubMaster()
  monkeypatch.setattr(the_galaxy.messaging, "SubMaster", fake)
  started = []
  real_thread = threading.Thread

  def tracking_thread(*args, **kwargs):
    thread = real_thread(*args, **kwargs)
    started.append(thread)
    return thread
  monkeypatch.setattr(the_galaxy.threading, "Thread", tracking_thread)
  yield fake
  stop.set()
  for thread in started:
    thread.join(timeout=3.0)
    assert not thread.is_alive()


def _wait_until(condition, timeout=3.0):
  deadline = time.monotonic() + timeout
  while not condition():
    assert time.monotonic() < deadline, "timed out"
    time.sleep(0.005)


def _probe(timeout_s, while_waiting=None):
  """Call _device_ignition_on on a request-like thread; run while_waiting once it is blocked in the wait,
  so anything published there is a message received after the call (no sleeps racing the reader)."""
  result = []
  caller = threading.Thread(target=lambda: result.append(the_galaxy._device_ignition_on(timeout_s=timeout_s)))
  caller.start()
  _wait_until(lambda: len(the_galaxy._panda_ignition_cond._waiters) > 0 or not caller.is_alive())
  if while_waiting is not None:
    while_waiting()
  caller.join(timeout=timeout_s + 3.0)
  assert not caller.is_alive()
  return result[0], caller


def test_device_ignition_on_uses_one_subscriber_on_its_own_thread(ignition_reader):
  # msgq never frees reader slots and signals readers by the subscribing TID: one subscriber, on a thread
  # that lives as long as Galaxy, never on the (short-lived) request thread.
  callers = []
  for _ in range(3):
    result, caller = _probe(1.0, lambda: ignition_reader.publish(True, [_panda()]))
    assert result is False
    callers.append(caller)

  assert len(ignition_reader.created_on) == 1
  reader = ignition_reader.created_on[0]
  assert reader is the_galaxy._panda_ignition_thread and reader.is_alive() and reader.daemon
  assert reader not in callers and reader is not threading.current_thread()


def test_device_ignition_on_needs_a_message_after_the_call(ignition_reader):
  assert _probe(1.0, lambda: ignition_reader.publish(True, [_panda()]))[0] is False

  # Delivered before the next call: its result is cached, but it is not fresh, so refuse.
  seq = the_galaxy._panda_ignition_seq
  ignition_reader.publish(True, [_panda()])
  _wait_until(lambda: the_galaxy._panda_ignition_seq > seq)
  assert _probe(0.3)[0] is None


def test_device_ignition_on_invalid_fresh_message_refuses(ignition_reader):
  assert _probe(0.3, lambda: ignition_reader.publish(False, [_panda()]))[0] is None


def test_device_ignition_on_one_bad_sample_does_not_decide(ignition_reader):
  # An invalid message followed by a good one within the timeout: the good one decides.
  def publish():
    ignition_reader.publish(False, [_panda()])
    ignition_reader.publish(True, [_panda()])
  assert _probe(1.0, publish)[0] is False


def test_device_ignition_on_survives_reader_errors(ignition_reader):
  _probe(1.0, lambda: ignition_reader.publish(True, [_panda()]))
  reader = the_galaxy._panda_ignition_thread

  def fail_then_publish():
    ignition_reader.fail_updates = 1
    ignition_reader.publish(True, [_panda(ignition_can=True)])
  assert _probe(3.0, fail_then_publish)[0] is True
  assert the_galaxy._panda_ignition_thread is reader and reader.is_alive()


def test_device_ignition_on_retries_subscriber_creation_on_the_same_thread(ignition_reader):
  ignition_reader.fail_creates = 1
  assert _probe(3.0, lambda: ignition_reader.publish(True, [_panda()]))[0] is False
  assert len(ignition_reader.created_on) == 1
  assert ignition_reader.created_on[0] is the_galaxy._panda_ignition_thread


def test_device_ignition_on_restarts_a_dead_reader(ignition_reader):
  dead = threading.Thread(target=lambda: None)
  dead.start()
  dead.join()
  the_galaxy._panda_ignition_thread = dead

  assert _probe(1.0, lambda: ignition_reader.publish(True, [_panda()]))[0] is False
  assert the_galaxy._panda_ignition_thread is not dead and the_galaxy._panda_ignition_thread.is_alive()


def test_device_ignition_on_no_publisher(ignition_reader):
  assert _probe(0.2)[0] is None


def test_device_ignition_on_concurrent_callers_do_not_queue(ignition_reader):
  # Waiting callers share the condition, so N callers take about one probe, not N probes in a row.
  results = []
  threads = [threading.Thread(target=lambda: results.append(the_galaxy._device_ignition_on(timeout_s=0.3))) for _ in range(5)]
  start = time.monotonic()
  for t in threads:
    t.start()
  for t in threads:
    t.join()

  assert results == [None] * 5
  assert time.monotonic() - start < 1.0
