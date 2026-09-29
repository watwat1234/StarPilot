import os
import sqlite3
import threading
import time

import pytest

import openpilot.starpilot.system.battery_monitor as bm
from openpilot.starpilot.system.battery_monitor import BatteryMonitor, latest_sample, read_history

WALL0 = 1_790_000_000.0
DT = 0.5


def make_monitor(db_path, wall_base=WALL0, clock_valid=lambda: True):
  return BatteryMonitor(db_path=str(db_path), clock_valid=clock_valid, wall_time=lambda: wall_base + make_monitor.now)


make_monitor.now = 0.0


def feed(monitor, start, seconds, voltage_mv, ignition, draw_w=2.0):
  """Feed 2 Hz readings from start for seconds, then wait for the writer; returns the time after the last reading."""
  t = start
  for _ in range(int(seconds / DT)):
    make_monitor.now = t
    monitor.update(t, voltage_mv, ignition, draw_w)
    t += DT
  assert monitor.drain(timeout=5)
  return t


def rows(db_path, table):
  db = sqlite3.connect(db_path)
  db.row_factory = sqlite3.Row
  try:
    return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
  finally:
    db.close()


@pytest.fixture
def db_path(tmp_path):
  return str(tmp_path / "battery.db")


def test_bucket_aggregates_min_mean_max(db_path):
  monitor = make_monitor(db_path)
  t = feed(monitor, 0.0, 300, 12400, False)
  t = feed(monitor, t, 1, 11000, False)
  feed(monitor, t, 300, 12400, False)

  samples = rows(db_path, "samples")
  assert len(samples) == 1
  sample = samples[0]
  assert sample["ts_start"] == pytest.approx(WALL0)
  assert sample["ts_end"] - sample["ts_start"] >= bm.SAMPLE_INTERVAL_S
  assert sample["v_min"] == pytest.approx(11.0)
  assert sample["v_max"] == pytest.approx(12.4)
  assert 12.39 < sample["v_mean"] < 12.4
  assert sample["onroad"] == 0
  assert sample["draw_w"] == pytest.approx(2.0)


def test_ignition_edge_cuts_bucket_and_session(db_path):
  monitor = make_monitor(db_path)
  t = feed(monitor, 0.0, 300, 12500, False)
  feed(monitor, t, 60, 14100, True)

  samples = rows(db_path, "samples")
  assert [s["onroad"] for s in samples] == [0]  # the drive bucket is still open
  assert samples[0]["v_max"] == pytest.approx(12.5)

  park, drive = rows(db_path, "sessions")
  assert (park["kind"], park["start_flag"], park["end_reason"]) == ("park", "ign_off_at_boot", "ignition")
  assert (drive["kind"], drive["start_flag"], drive["end_reason"]) == ("drive", "clean", None)
  assert drive["v_start"] == pytest.approx(14.1)


def test_boot_with_ignition_on_is_flagged(db_path):
  monitor = make_monitor(db_path)
  feed(monitor, 0.0, 601, 13900, True)

  (drive,) = rows(db_path, "sessions")
  assert drive["start_flag"] == "ign_on_at_boot"


def test_park_records_voltage_at_offsets(db_path):
  monitor = make_monitor(db_path)
  t = feed(monitor, 0.0, 60, 14000, True)
  t = feed(monitor, t, 2 * 3600, 12600, False)
  feed(monitor, t, 1.5 * 3600, 12300, False)

  park = rows(db_path, "sessions")[1]
  assert park["kind"] == "park"
  assert park["v_at_1h"] == pytest.approx(12.6, abs=0.01)
  assert park["v_at_3h"] == pytest.approx(12.3, abs=0.01)
  assert park["v_at_6h"] is None
  assert park["v_start"] == pytest.approx(12.6)
  assert park["v_min"] == pytest.approx(12.3)


def test_close_ends_session_and_stops_recording(db_path):
  monitor = make_monitor(db_path)
  t = feed(monitor, 0.0, 120, 11700, False)
  monitor.close("low_voltage", now=t)
  feed(monitor, t, 900, 11600, False)

  (park,) = rows(db_path, "sessions")
  assert park["end_reason"] == "low_voltage"
  assert park["v_min"] == pytest.approx(11.7)
  assert len(rows(db_path, "samples")) == 1


def test_stop_writes_open_bucket_and_leaves_session_open(db_path):
  monitor = make_monitor(db_path)
  t = feed(monitor, 0.0, 120, 12400, False)
  assert monitor.stop(now=t - DT, timeout=5)
  feed(monitor, t, 900, 12000, False)  # ignored after stop

  (sample,) = rows(db_path, "samples")
  assert sample["v_mean"] == pytest.approx(12.4)
  (park,) = rows(db_path, "sessions")
  assert park["end_reason"] is None  # the next run resumes it or closes it as unknown
  assert park["n"] == 240


def test_close_does_not_wait_for_the_writer(db_path, monkeypatch):
  gate = threading.Event()
  write = bm._Writer._write

  def slow_write(self, snapshot):
    gate.wait(5)
    write(self, snapshot)

  monkeypatch.setattr(bm._Writer, "_write", slow_write)
  monitor = make_monitor(db_path)
  t = 0.0
  for _ in range(240):
    make_monitor.now = t
    monitor.update(t, 12400, False, 2.0)
    t += DT
  started = time.monotonic()
  monitor.close("low_voltage", now=t)
  assert time.monotonic() - started < 0.5
  gate.set()
  assert monitor.stop(timeout=5)
  (park,) = rows(db_path, "sessions")
  assert park["end_reason"] == "low_voltage"


def test_resume_leaves_marks_in_the_off_gap_empty(db_path):
  first = make_monitor(db_path)
  t = feed(first, 0.0, 3000, 12500, False)  # parked 50 min
  first.stop(now=t - DT, timeout=5)

  # back 12 min later: the 1 h mark fell while the device was off
  second = make_monitor(db_path, wall_base=WALL0 + t - DT + 720)
  t = feed(second, 0.0, 601, 12300, False)  # the first flush finds the open session; drained, so the resume lands now
  feed(second, t, 2.5 * 3600, 12300, False)

  (park,) = rows(db_path, "sessions")
  assert park["start_ts"] == pytest.approx(WALL0)
  assert park["v_at_1h"] is None
  assert park["v_at_3h"] == pytest.approx(12.3, abs=0.01)


def test_wall_clock_step_moves_later_rows(db_path):
  wall = {"base": WALL0}
  monitor = BatteryMonitor(db_path=db_path, clock_valid=lambda: True, wall_time=lambda: wall["base"] + make_monitor.now)
  t = feed(monitor, 0.0, 601, 12400, False)
  wall["base"] += 3600  # NTP correction after the first flush
  feed(monitor, t, 601, 12400, False)

  first, second = rows(db_path, "samples")
  assert first["ts_start"] == pytest.approx(WALL0)
  assert second["ts_start"] == pytest.approx(WALL0 + 3600 + t - DT)  # the second bucket starts at the last reading fed


def test_invalid_clock_buffers_until_valid(db_path):
  valid = {"ok": False}
  monitor = make_monitor(db_path, clock_valid=lambda: valid["ok"])
  t = feed(monitor, 0.0, 1300, 12400, False)
  assert not os.path.exists(db_path)

  valid["ok"] = True
  feed(monitor, t, 1200, 12400, False)

  samples = rows(db_path, "samples")
  assert len(samples) == 4
  assert samples[0]["ts_start"] == pytest.approx(WALL0)
  assert samples[1]["ts_start"] == pytest.approx(samples[0]["ts_end"] + DT)


def test_restart_after_long_gap_closes_stale_session(db_path):
  first = make_monitor(db_path)
  feed(first, 0.0, 1800, 12400, False)  # dies without close()

  # after a reboot the monotonic clock starts over, so the wall base moves on
  second = make_monitor(db_path, wall_base=WALL0 + 1800 + 2 * 3600)
  feed(second, 0.0, 601, 14000, True)

  stale, drive = rows(db_path, "sessions")
  assert stale["end_reason"] == "unknown"
  assert stale["end_ts"] == pytest.approx(WALL0 + 1200.5)  # last flush before it died
  assert (drive["start_flag"], drive["end_reason"]) == ("ign_on_at_boot", None)


def test_restart_within_gap_resumes_session(db_path):
  first = make_monitor(db_path)
  feed(first, 0.0, 1201, 12500, False)  # last flush at 1200.5 covers all 2402 readings
  first_rows = rows(db_path, "sessions")

  second = make_monitor(db_path, wall_base=WALL0 + 1200.5 + 300)
  feed(second, 0.0, 601, 12300, False)  # flushes at 600 with 1201 readings

  (park,) = rows(db_path, "sessions")
  assert park["id"] == first_rows[0]["id"]
  assert park["start_ts"] == pytest.approx(WALL0)
  assert park["start_flag"] == "ign_off_at_boot"
  assert park["end_reason"] is None
  assert park["n"] == 2402 + 1201
  assert park["v_start"] == pytest.approx(12.5)
  assert park["v_min"] == pytest.approx(12.3)


def test_update_does_not_wait_for_a_slow_write(db_path, monkeypatch):
  gate = threading.Event()
  write = bm._Writer._write

  def slow_write(self, snapshot):
    gate.wait(5)
    write(self, snapshot)

  monkeypatch.setattr(bm._Writer, "_write", slow_write)
  monitor = make_monitor(db_path)
  t = 0.0
  started = time.monotonic()
  for _ in range(int(2 * 601 / DT)):  # two bucket flushes while the writer is stuck
    make_monitor.now = t
    monitor.update(t, 12400, False, 2.0)
    t += DT
  assert time.monotonic() - started < 1.0
  assert not os.path.exists(db_path)

  gate.set()
  assert monitor.drain(timeout=5)
  assert len(rows(db_path, "samples")) == 2


def test_failed_write_is_retried(db_path, monkeypatch):
  fail = {"on": True}
  connect = bm._Writer._connect

  def flaky_connect(self):
    if fail["on"]:
      raise sqlite3.OperationalError("disk I/O error")
    return connect(self)

  monkeypatch.setattr(bm._Writer, "_connect", flaky_connect)
  monitor = make_monitor(db_path)
  t = feed(monitor, 0.0, 601, 12400, False)
  assert not os.path.exists(db_path)

  fail["on"] = False
  feed(monitor, t, 601, 12400, False)
  assert len(rows(db_path, "samples")) == 2
  (park,) = rows(db_path, "sessions")
  assert park["n"] == 2402  # every reading up to the second flush, including those from the failed one


def test_retention_trims_old_samples(db_path, monkeypatch):
  monkeypatch.setattr(bm, "SAMPLE_RETENTION_S", 1000)
  monkeypatch.setattr(bm, "TRIM_INTERVAL_S", 0)
  monitor = make_monitor(db_path)
  feed(monitor, 0.0, 3 * 601, 12400, False)

  samples = rows(db_path, "samples")
  assert len(samples) == 2
  assert samples[0]["ts_start"] > WALL0


def test_default_cutoff_matches_power_monitoring():
  from openpilot.system.hardware.power_monitoring import VBATT_PAUSE_CHARGING
  assert bm.DEFAULT_CUTOFF_V == VBATT_PAUSE_CHARGING


def test_read_history_and_latest(db_path):
  assert read_history(7, db_path=db_path) == {"samples": [], "sessions": []}
  assert latest_sample(db_path=db_path) is None

  monitor = make_monitor(db_path)
  t = feed(monitor, 0.0, 1201, 12400, False)
  feed(monitor, t, 601, 14000, True)

  now = WALL0 + t + 601
  history = read_history(1, db_path=db_path, now=now)
  assert [s["onroad"] for s in history["samples"]] == [False, False, True]
  assert [s["kind"] for s in history["sessions"]] == ["park", "drive"]
  assert read_history(1, db_path=db_path, now=now + 3 * 86400)["samples"] == []
  assert [s["kind"] for s in read_history(1, db_path=db_path, now=now + 3 * 86400)["sessions"]] == ["drive"]

  latest = latest_sample(db_path=db_path)
  assert latest["onroad"] is True
  assert latest["voltage"] == pytest.approx(14.0)
