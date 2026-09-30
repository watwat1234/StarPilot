import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import openpilot.starpilot.system.device_history as dh
from openpilot.starpilot.system.device_history import DeviceHistory, Thermal, latest_sample, read_history

WALL0 = 1_790_000_000.0
DT = 0.5


def make_monitor(db_path, wall_base=WALL0, clock_valid=lambda: True):
  return DeviceHistory(db_path=str(db_path), clock_valid=clock_valid, wall_time=lambda: wall_base + make_monitor.now)


make_monitor.now = 0.0


def feed(monitor, start, seconds, voltage_mv, ignition, draw_w=2.0, thermal=None):
  """Feed 2 Hz readings from start for seconds, then wait for the writer; returns the time after the last reading."""
  t = start
  for _ in range(int(seconds / DT)):
    make_monitor.now = t
    monitor.update(t, voltage_mv, ignition, draw_w, thermal)
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
  return str(tmp_path / "device.db")


def test_bucket_aggregates_min_mean_max(db_path):
  monitor = make_monitor(db_path)
  t = feed(monitor, 0.0, 300, 12400, False)
  t = feed(monitor, t, 1, 11000, False)
  feed(monitor, t, 300, 12400, False)

  samples = rows(db_path, "samples")
  assert len(samples) == 1
  sample = samples[0]
  assert sample["ts_start"] == pytest.approx(WALL0)
  assert sample["ts_end"] - sample["ts_start"] >= dh.SAMPLE_INTERVAL_S
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
  write = dh._Writer._write

  def slow_write(self, snapshot):
    gate.wait(5)
    write(self, snapshot)

  monkeypatch.setattr(dh._Writer, "_write", slow_write)
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
  monitor = DeviceHistory(db_path=db_path, clock_valid=lambda: True, wall_time=lambda: wall["base"] + make_monitor.now)
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
  write = dh._Writer._write

  def slow_write(self, snapshot):
    gate.wait(5)
    write(self, snapshot)

  monkeypatch.setattr(dh._Writer, "_write", slow_write)
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
  connect = dh._Writer._connect

  def flaky_connect(self):
    if fail["on"]:
      raise sqlite3.OperationalError("disk I/O error")
    return connect(self)

  monkeypatch.setattr(dh._Writer, "_connect", flaky_connect)
  monitor = make_monitor(db_path)
  t = feed(monitor, 0.0, 601, 12400, False)
  assert not os.path.exists(db_path)

  fail["on"] = False
  feed(monitor, t, 601, 12400, False)
  assert len(rows(db_path, "samples")) == 2
  (park,) = rows(db_path, "sessions")
  assert park["n"] == 2402  # every reading up to the second flush, including those from the failed one


def test_retention_trims_old_samples(db_path, monkeypatch):
  monkeypatch.setattr(dh, "SAMPLE_RETENTION_S", 1000)
  monkeypatch.setattr(dh, "TRIM_INTERVAL_S", 0)
  monitor = make_monitor(db_path)
  feed(monitor, 0.0, 3 * 601, 12400, False)

  samples = rows(db_path, "samples")
  assert len(samples) == 2
  assert samples[0]["ts_start"] > WALL0


def test_default_cutoff_matches_power_monitoring():
  from openpilot.system.hardware.power_monitoring import VBATT_PAUSE_CHARGING
  assert dh.DEFAULT_CUTOFF_V == VBATT_PAUSE_CHARGING


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


HOT = Thermal(soc=90.0, cpu=88.0, gpu=90.0, mem=80.0, intake=50.0, exhaust=60.0, fan_pct=30, fan_rpm=2500, hot=True, overheated=True)
WARM = Thermal(soc=70.0, cpu=68.0, gpu=70.0, mem=65.0, intake=40.0, exhaust=50.0, fan_pct=20, fan_rpm=1500)


def test_thermal_bucket_aggregates(db_path):
  monitor = make_monitor(db_path)
  t = feed(monitor, 0.0, 300, 12400, False, thermal=WARM)
  t = feed(monitor, t, 1, 12400, False, thermal=HOT)  # two readings
  feed(monitor, t, 300, 12400, False, thermal=WARM)

  (sample,) = rows(db_path, "samples")
  assert sample["soc_max"] == pytest.approx(90.0)
  assert 70.0 < sample["soc_mean"] < 70.1
  assert (sample["cpu_max"], sample["gpu_max"], sample["mem_max"]) == (88.0, 90.0, 80.0)
  assert sample["intake_max"] == pytest.approx(50.0)
  assert 40.0 < sample["intake_mean"] < 40.1
  assert 50.0 < sample["exhaust_mean"] < 50.1
  assert 20.0 < sample["fan_pct_mean"] < 20.1
  assert 1500 < sample["fan_rpm_mean"] < 1510
  assert sample["s_hot"] == pytest.approx(1.0)  # 0.5 s per reading
  assert sample["s_overheated"] == pytest.approx(1.0)


def test_missing_zones_stay_null(db_path):
  monitor = make_monitor(db_path)
  no_intake = Thermal(soc=60.0, cpu=60.0, gpu=55.0, mem=50.0)
  t = feed(monitor, 0.0, 600, 12400, False, thermal=no_intake)  # the whole first bucket but its closing reading
  t = feed(monitor, t, 600.5, 12400, False)  # no thermal at all: closes the first bucket, fills the second
  monitor.close("test", now=t)
  assert monitor.drain(timeout=5)

  with_zones, without = rows(db_path, "samples")
  assert with_zones["soc_max"] == pytest.approx(60.0)
  assert with_zones["s_hot"] == 0
  assert (with_zones["intake_mean"], with_zones["exhaust_mean"], with_zones["fan_pct_mean"]) == (None, None, None)
  assert all(without[column] is None for column in ("soc_mean", "soc_max", "intake_mean", "s_hot", "s_overheated"))
  (park,) = rows(db_path, "sessions")
  assert park["soc_max"] == pytest.approx(60.0)
  assert park["intake_at_start"] is None
  assert park["self_heat_mean"] is None
  assert park["v_mean"] == pytest.approx(12.4)


def test_park_records_self_heating_and_intake_at_start(db_path):
  monitor = make_monitor(db_path)
  t = feed(monitor, 0.0, 60, 14000, True, thermal=WARM)
  first = Thermal(soc=75.0, intake=45.0)
  t = feed(monitor, t, 0.5, 12500, False, thermal=first)
  t = feed(monitor, t, 600, 12500, False, thermal=HOT)
  monitor.close("test", now=t)
  assert monitor.drain(timeout=5)

  drive, park = rows(db_path, "sessions")
  assert drive["intake_at_start"] == pytest.approx(40.0)
  assert drive["self_heat_mean"] == pytest.approx(30.0)
  assert park["intake_at_start"] == pytest.approx(45.0)
  assert park["soc_max"] == pytest.approx(90.0)
  assert park["intake_max"] == pytest.approx(50.0)
  assert 39.9 < park["self_heat_mean"] < 40.0  # one reading 30 over intake, the rest 40
  assert park["s_hot"] == pytest.approx(600.0)
  assert park["s_overheated"] == pytest.approx(600.0)
  assert park["fan_pct_mean"] == pytest.approx(30.0)


def test_resume_merges_thermal_columns(db_path):
  first = make_monitor(db_path)
  feed(first, 0.0, 1201, 12500, False, thermal=HOT)  # last flush at 1200.5 covers 2402 readings

  second = make_monitor(db_path, wall_base=WALL0 + 1200.5 + 300)
  feed(second, 0.0, 601, 12300, False, thermal=WARM)  # flushes at 600 with 1201 readings

  (park,) = rows(db_path, "sessions")
  assert park["n"] == 2402 + 1201
  assert park["intake_at_start"] == pytest.approx(50.0)  # from the first run
  assert park["soc_max"] == pytest.approx(90.0)
  assert park["soc_mean"] == pytest.approx((90.0 * 2402 + 70.0 * 1201) / 3603, abs=1e-3)
  assert park["intake_max"] == pytest.approx(50.0)
  assert park["s_hot"] == pytest.approx(1201.0)  # hot readings only from the first run
  assert park["s_overheated"] == pytest.approx(1201.0)


def old_schema_db(path):
  """A database from the battery-only version: its first schema, one finished park, written through the WAL."""
  db = sqlite3.connect(path)
  db.execute("PRAGMA journal_mode=WAL")
  db.executescript(dh.SCHEMA)
  with db:
    db.execute("INSERT INTO samples VALUES (?, ?, 12.1, 12.2, 12.3, 0, 1.5)", (WALL0 - 1200, WALL0 - 600))
    db.execute("INSERT INTO sessions (kind, start_ts, end_ts, v_min, n, start_flag, end_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
               ("park", WALL0 - 1200, WALL0 - 600, 12.1, 1200, "clean", "ignition"))
  return db


def test_legacy_database_is_moved_and_extended(tmp_path):
  source = tmp_path / "source.db"
  db = old_schema_db(str(source))  # kept open, so the rows are still only in the WAL
  legacy_dir = tmp_path / "battery_monitor"
  legacy_dir.mkdir()
  legacy = legacy_dir / "battery.db"
  for suffix in ("", "-wal", "-shm"):
    shutil.copyfile(f"{source}{suffix}", f"{legacy}{suffix}")
  db.close()
  assert os.path.exists(str(legacy) + "-wal")

  new = tmp_path / "device_history" / "device.db"
  monitor = DeviceHistory(db_path=str(new), legacy_path=str(legacy), clock_valid=lambda: True,
                          wall_time=lambda: WALL0 + make_monitor.now)
  feed(monitor, 0.0, 601, 12400, False, thermal=WARM)

  assert not legacy_dir.exists()
  samples = rows(str(new), "samples")
  assert len(samples) == 2
  assert samples[0]["v_mean"] == pytest.approx(12.2)
  assert samples[0]["soc_mean"] is None
  assert samples[1]["soc_mean"] == pytest.approx(70.0)
  old_park, park = rows(str(new), "sessions")
  assert (old_park["end_reason"], old_park["n"], old_park["soc_max"]) == ("ignition", 1200, None)
  assert park["soc_max"] == pytest.approx(70.0)


def test_legacy_move_skipped_when_new_database_exists(tmp_path):
  legacy = tmp_path / "battery_monitor" / "battery.db"
  legacy.parent.mkdir()
  old_schema_db(str(legacy)).close()
  new = tmp_path / "device.db"
  feed(make_monitor(new), 0.0, 601, 12400, False)
  monitor = DeviceHistory(db_path=str(new), legacy_path=str(legacy), clock_valid=lambda: True,
                          wall_time=lambda: WALL0 + 3600 + make_monitor.now)
  feed(monitor, 0.0, 601, 12400, False)
  assert legacy.exists()  # the new database already existed, so the old one is left alone
  assert len(rows(str(new), "samples")) == 2


def test_default_paths():
  assert dh.default_db_path().endswith(os.path.join("device_history", "device.db"))
  assert DeviceHistory(clock_valid=lambda: False)._writer.legacy_path == dh._legacy_db_path()
  assert DeviceHistory(db_path="/tmp/x.db", clock_valid=lambda: False)._writer.legacy_path is None


def test_read_history_selects_metric_columns(db_path):
  monitor = make_monitor(db_path)
  feed(monitor, 0.0, 601, 12400, False, thermal=WARM)

  now = WALL0 + 601
  battery = read_history(1, db_path=db_path, now=now)["samples"][0]
  assert set(battery) == set(dh.METRIC_SAMPLE_COLUMNS["battery"])
  thermal = read_history(1, db_path=db_path, now=now, metric="thermal")
  assert set(thermal["samples"][0]) == set(dh.METRIC_SAMPLE_COLUMNS["thermal"])
  assert thermal["samples"][0]["soc_mean"] == pytest.approx(70.0)
  assert thermal["sessions"][0]["soc_max"] == pytest.approx(70.0)
  with pytest.raises(KeyError):
    read_history(1, db_path=db_path, now=now, metric="nope")


@pytest.mark.parametrize("device_type", ["mici", "tizi"])
def test_thermal_limits_match_hardwared(device_type):
  # hardwared and fan_controller pick their thresholds at import, from the device type: check them in a fresh
  # interpreter with the type patched, instead of reloading them in this one
  code = f"""
from unittest import mock
import openpilot.system.hardware as hw
with mock.patch.object(hw.HARDWARE, "get_device_type", return_value="{device_type}"):
  from openpilot.system.hardware import hardwared, fan_controller
bands = hardwared.THERMAL_BANDS
fan = fan_controller.TiciFanController()
print(hardwared.OFFROAD_DANGER_TEMP, bands[hardwared.ThermalStatus.ok].max_temp,
      max(fan.update(110.0, False) for _ in range(200)))
"""
  out = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True).stdout.split()
  limits = dh.thermal_limits(device_type)
  assert [float(x) for x in out] == [limits["dangerC"], limits["overheatedC"], limits["parkedFanCapPct"]]


def test_boot_session_is_handed_over_at_once(db_path):
  monitor = make_monitor(db_path)
  feed(monitor, 0.0, 1, 12500, False)  # well before the first bucket closes
  (park,) = rows(db_path, "sessions")
  assert park["start_flag"] == "ign_off_at_boot"
  assert rows(db_path, "samples") == []


def test_resume_keeps_a_mark_that_fell_in_this_run(db_path):
  first = make_monitor(db_path)
  t = feed(first, 0.0, 3000, 12500, False)  # parked 50 min, then a reboot
  first.stop(now=t - DT, timeout=5)

  # back 5 min later: the 1 h mark falls 5 min into this run, before its first bucket closes
  second = make_monitor(db_path, wall_base=WALL0 + t - DT + 300)
  t = feed(second, 0.0, DT, 12300, False)  # one reading hands the session over; drained, so the resume lands next
  feed(second, t, 601, 12300, False)

  (park,) = rows(db_path, "sessions")
  assert park["start_ts"] == pytest.approx(WALL0)
  assert park["v_at_1h"] == pytest.approx(12.3, abs=0.01)


def test_readers_use_the_legacy_database_until_it_moves(tmp_path, monkeypatch):
  legacy = tmp_path / "battery_monitor" / "battery.db"
  legacy.parent.mkdir()
  old_schema_db(str(legacy)).close()
  monkeypatch.setattr(dh, "_legacy_db_path", lambda: str(legacy))
  monkeypatch.setattr(dh, "default_db_path", lambda: str(tmp_path / "device_history" / "device.db"))

  battery = read_history(1, now=WALL0)
  assert len(battery["samples"]) == 1
  assert battery["sessions"][0]["end_reason"] == "ignition"
  thermal = read_history(1, now=WALL0, metric="thermal")  # no temperature columns in the old schema
  assert thermal["samples"] == []
  assert len(thermal["sessions"]) == 1
  assert latest_sample()["voltage"] == pytest.approx(12.2)
