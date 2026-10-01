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
@pytest.mark.parametrize("update_kind", ["running", "rebooting"])
def test_device_power_refused_while_update_active(monkeypatch, route, _param, update_kind):
  # Timestamps are taken here, not at collection, so a slow suite can't age them out.
  update_state = {
    "running": {"running": True, "stage": "downloading"},
    # _finish_update_and_reboot: running is already False while it waits to call HARDWARE.reboot().
    "rebooting": {"running": False, "stage": "rebooting", "finishedAt": time.time()},
  }[update_kind]
  monkeypatch.setattr(the_galaxy, "_update_reboot_busy_until_mono", time.monotonic() + the_galaxy._UPDATE_REBOOT_BUSY_SECONDS)
  client, params, hardware_calls = _power_client(monkeypatch, PARKED, update_state=update_state)

  assert client.post(route).status_code == 409
  assert params.writes == []
  assert params.ignition_probes == []
  assert hardware_calls == []


@pytest.mark.parametrize("route,param", ROUTES)
def test_device_power_allowed_after_stuck_update_reboot(monkeypatch, route, param):
  # The update's own reboot never happened; the stale "rebooting" stage must not block recovery forever.
  monkeypatch.setattr(the_galaxy, "_update_reboot_busy_until_mono", time.monotonic() - 1.0)
  client, params, _ = _power_client(monkeypatch, PARKED, update_state={"running": False, "stage": "rebooting"})

  assert client.post(route).status_code == 200
  assert params.writes[-1] == (param, True)


def test_finish_update_and_reboot_marks_busy_before_rebooting(monkeypatch):
  # The deadline is process-local and kept out of the status dict that /api/update/fast/status returns.
  state = dict(the_galaxy._fast_update_state, running=True, stage="finalizing")
  monkeypatch.setattr(the_galaxy, "_fast_update_state", state)
  monkeypatch.setattr(the_galaxy, "_update_reboot_busy_until_mono", 0.0)
  monkeypatch.setattr(the_galaxy, "_FAST_UPDATE_REBOOT_NOTICE_SECONDS", 0.0)
  seen_at_reboot = []
  monkeypatch.setattr(the_galaxy.HARDWARE, "reboot", lambda: seen_at_reboot.append(the_galaxy._update_action_busy(dict(state))),
                      raising=False)

  the_galaxy._finish_update_and_reboot("done")

  assert state["running"] is False and state["stage"] == "rebooting"
  assert seen_at_reboot == [True]
  assert "rebootingSinceMono" not in state and "rebootBusyUntil" not in state


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
    response = client.post(route)
  finally:
    if flash == "lock":
      the_galaxy._PANDA_FLASH_REBOOT_LOCK.release()
  assert response.status_code == 409
  # An unreadable flash state says so instead of claiming a flash is running.
  assert ("could not read the panda firmware flash state" in response.get_json()["message"]) == (flash == "param_unreadable")
  assert params.writes == []
  assert params.ignition_probes == []
  assert hardware_calls == []


@pytest.mark.parametrize("route,_param", ROUTES)
def test_device_power_rechecks_galaxy_flash_inside_update_lock(monkeypatch, route, _param):
  # Galaxy's own flash starts after the last FlashPanda read (e.g. while the request waited for the update
  # lock): the in-lock check of _PANDA_FLASH_REBOOT_LOCK must still refuse.
  client, params, hardware_calls = _power_client(monkeypatch, PARKED)
  monkeypatch.setattr(the_galaxy, "_panda_flash_state", lambda: "idle")

  def probe_while_galaxy_flash_starts():
    the_galaxy._PANDA_FLASH_REBOOT_LOCK.acquire()
    return False
  monkeypatch.setattr(the_galaxy, "_device_ignition_on", probe_while_galaxy_flash_starts)

  try:
    assert client.post(route).status_code == 409
  finally:
    the_galaxy._PANDA_FLASH_REBOOT_LOCK.release()
  assert params.writes == []
  assert hardware_calls == []


def test_device_power_update_busy_message_wins_over_unreadable_flash(monkeypatch):
  client, params, _ = _power_client(monkeypatch, PARKED, update_state={"running": True, "stage": "downloading"})
  params.memory["FlashPanda"] = OSError("params unavailable")
  reads = []
  monkeypatch.setattr(the_galaxy, "_panda_flash_state", lambda: reads.append(True) or "unknown")

  response = client.post("/api/system/reboot")

  assert response.status_code == 409
  assert "Wait for the current update" in response.get_json()["message"]
  assert reads == []  # not read at all while an update already blocks


def test_panda_flash_state_logs_unreadable_once_per_streak(monkeypatch):
  monkeypatch.setattr(the_galaxy, "_PANDA_FLASH_REBOOT_LOCK", threading.Lock())
  monkeypatch.setattr(the_galaxy, "_panda_flash_read_failing", False)
  value = {"read": OSError("params unavailable")}

  def get_bool(key):
    if isinstance(value["read"], Exception):
      raise value["read"]
    return value["read"]
  monkeypatch.setattr(the_galaxy, "params_memory", SimpleNamespace(get_bool=get_bool))
  logged = []
  monkeypatch.setattr(the_galaxy.cloudlog, "exception", lambda *args, **kwargs: logged.append(args), raising=False)

  assert [the_galaxy._panda_flash_state() for _ in range(3)] == ["unknown"] * 3
  assert len(logged) == 1
  value["read"] = False
  assert the_galaxy._panda_flash_state() == "idle"
  value["read"] = OSError("again")
  assert the_galaxy._panda_flash_state() == "unknown"
  assert len(logged) == 2


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
    self.create_attempts = 0

  def __call__(self, services):
    assert services == ["pandaStates"]
    self.create_attempts += 1
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


class _ObservedCondition(threading.Condition):
  """Counts entries into wait(), so tests can act exactly when a caller is blocked (wait_for calls wait)."""

  def __init__(self):
    super().__init__()
    self.wait_entries = 0

  def wait(self, timeout=None):
    self.wait_entries += 1
    return super().wait(timeout)


@pytest.fixture
def ignition_reader(monkeypatch, fake_log):
  monkeypatch.setattr(the_galaxy, "_panda_ignition_cond", _ObservedCondition())
  monkeypatch.setattr(the_galaxy, "_panda_ignition_seq", 0)
  monkeypatch.setattr(the_galaxy, "_panda_ignition_latest", None)
  monkeypatch.setattr(the_galaxy, "_panda_ignition_thread", None)
  monkeypatch.setattr(the_galaxy, "_panda_ignition_gave_up", False)
  monkeypatch.setattr(the_galaxy, "_PANDA_IGNITION_RETRY_S", 0.05)
  stop = threading.Event()
  monkeypatch.setattr(the_galaxy, "_panda_ignition_stop", stop)
  fake = _FakeSubMaster()
  monkeypatch.setattr(the_galaxy.messaging, "SubMaster", fake)
  before = set(threading.enumerate())
  yield fake
  stop.set()
  # Only readers this test started (they use this test's stop event).
  for thread in set(threading.enumerate()) - before:
    if thread.name == "galaxy-panda-ignition":
      thread.join(timeout=3.0)
      assert not thread.is_alive()


def _wait_until(condition, timeout=3.0):
  deadline = time.monotonic() + timeout
  while not condition():
    assert time.monotonic() < deadline, "timed out"
    time.sleep(0.005)


def _probe(timeout_s, while_waiting=None):
  """Call _device_ignition_on on a request-like thread; run while_waiting(caller) once it is blocked in the
  wait, so anything published there is a message received after the call (no sleeps racing the reader)."""
  cond = the_galaxy._panda_ignition_cond
  entries = cond.wait_entries
  result = []
  caller = threading.Thread(target=lambda: result.append(the_galaxy._device_ignition_on(timeout_s=timeout_s)))
  caller.start()
  _wait_until(lambda: cond.wait_entries > entries or not caller.is_alive())
  if while_waiting is not None:
    while_waiting(caller)
  caller.join(timeout=timeout_s + 3.0)
  assert not caller.is_alive()
  return result[0], caller


def test_device_ignition_on_uses_one_subscriber_on_its_own_thread(ignition_reader):
  # msgq never frees reader slots and signals readers by the subscribing TID: one subscriber, on a thread
  # that lives as long as Galaxy, never on the (short-lived) request thread.
  callers = []
  for _ in range(3):
    result, caller = _probe(1.0, lambda _: ignition_reader.publish(True, [_panda()]))
    assert result is False
    callers.append(caller)

  assert len(ignition_reader.created_on) == 1
  reader = ignition_reader.created_on[0]
  assert reader is the_galaxy._panda_ignition_thread and reader.is_alive() and reader.daemon
  assert reader not in callers and reader is not threading.current_thread()


def test_device_ignition_on_needs_a_message_after_the_call(ignition_reader):
  assert _probe(1.0, lambda _: ignition_reader.publish(True, [_panda()]))[0] is False

  # Delivered before the next call: its result is cached, but it is not fresh, so refuse.
  seq = the_galaxy._panda_ignition_seq
  ignition_reader.publish(True, [_panda()])
  _wait_until(lambda: the_galaxy._panda_ignition_seq > seq)
  assert _probe(0.3)[0] is None


def test_device_ignition_on_invalid_fresh_message_refuses(ignition_reader):
  assert _probe(0.3, lambda _: ignition_reader.publish(False, [_panda()]))[0] is None


def _deliver_bad_sample_then(deliver_bad, deliver_good):
  # Let the caller see the inconclusive sample first: wait until it has gone back to waiting (or returned),
  # and only then deliver the good message.
  def run(caller):
    cond = the_galaxy._panda_ignition_cond
    seq, entries = the_galaxy._panda_ignition_seq, cond.wait_entries
    deliver_bad()
    _wait_until(lambda: the_galaxy._panda_ignition_seq > seq)
    _wait_until(lambda: cond.wait_entries > entries or not caller.is_alive())
    deliver_good()
  return run


def test_device_ignition_on_one_bad_sample_does_not_decide(ignition_reader):
  # An invalid message followed by a good one within the timeout: the good one decides.
  run = _deliver_bad_sample_then(lambda: ignition_reader.publish(False, [_panda()]),
                                 lambda: ignition_reader.publish(True, [_panda()]))
  assert _probe(2.0, run)[0] is False


def test_device_ignition_on_survives_reader_errors(ignition_reader):
  _probe(1.0, lambda _: ignition_reader.publish(True, [_panda()]))
  reader = the_galaxy._panda_ignition_thread

  # The reader error is delivered (and seen by the caller) before the good message.
  def fail():
    ignition_reader.fail_updates = 1
  run = _deliver_bad_sample_then(fail, lambda: ignition_reader.publish(True, [_panda(ignition_can=True)]))
  assert _probe(2.0, run)[0] is True
  assert ignition_reader.fail_updates == 0
  assert the_galaxy._panda_ignition_thread is reader and reader.is_alive()


def test_device_ignition_on_retries_subscriber_creation_on_the_same_thread(ignition_reader):
  ignition_reader.fail_creates = 1
  assert _probe(2.0, lambda _: ignition_reader.publish(True, [_panda()]))[0] is False
  assert ignition_reader.create_attempts == 2
  assert len(ignition_reader.created_on) == 1
  assert ignition_reader.created_on[0] is the_galaxy._panda_ignition_thread


def test_device_ignition_on_gives_up_after_repeated_creation_failures(ignition_reader):
  # Each failed creation may have cost a msgq reader slot, so stop retrying and refuse from then on.
  ignition_reader.fail_creates = 100
  started = time.monotonic()
  result, _ = _probe(2.0)
  assert result is None
  assert the_galaxy._panda_ignition_gave_up
  # The give-up wakes the waiting caller; it doesn't sit out its 2 s timeout (backoff here is 0.05 + 0.1 s).
  assert time.monotonic() - started < 1.0
  assert ignition_reader.create_attempts == the_galaxy._PANDA_IGNITION_CREATE_ATTEMPTS

  started = time.monotonic()
  assert the_galaxy._device_ignition_on(timeout_s=1.0) is None
  assert time.monotonic() - started < 0.1
  assert ignition_reader.create_attempts == the_galaxy._PANDA_IGNITION_CREATE_ATTEMPTS


def test_device_ignition_on_restarts_a_dead_reader(ignition_reader):
  dead = threading.Thread(target=lambda: None)
  dead.start()
  dead.join()
  the_galaxy._panda_ignition_thread = dead

  assert _probe(1.0, lambda _: ignition_reader.publish(True, [_panda()]))[0] is False
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
