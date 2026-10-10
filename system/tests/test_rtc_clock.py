import datetime
import json
from types import SimpleNamespace

import pytest

from openpilot.common.time_helpers import min_date
from openpilot.system import rtc_clock
from openpilot.system.rtc_clock import (MAX_AGE, RESAVE_AGE, RtcClock, load_offset, read_rtc, save_offset,
                                        time_from_rtc)

# relative to min_date() (the host's systemd build date), so the tests don't age out
REAL_TIME = (min_date() + datetime.timedelta(days=30)).replace(microsecond=0)
REAL_UTC = int(REAL_TIME.replace(tzinfo=datetime.UTC).timestamp())
RTC = 1_000_000


def saved(rtc=RTC, utc=REAL_UTC):
  return {"rtc": rtc, "utc": utc, "source": "ntp"}


def test_time_from_rtc_adds_elapsed_rtc():
  t, reason = time_from_rtc(saved(), RTC + 3600)
  assert reason is None
  assert t == REAL_TIME + datetime.timedelta(hours=1)


@pytest.mark.parametrize("s, rtc_now, reason", [
  (saved(), RTC - 1, "RTC reset"),
  (saved(), RTC + MAX_AGE + 1, "too old"),
  (saved(utc=REAL_UTC - 60 * 86400), RTC, "out of range"),
  (None, RTC, "no saved offset"),
  (saved(), None, "no RTC"),
  (saved(utc=10**20), RTC, "bad saved offset"),
])
def test_time_from_rtc_rejects(s, rtc_now, reason):
  t, why = time_from_rtc(s, rtc_now)
  assert t is None
  assert reason in why


def test_read_rtc(tmp_path):
  p = tmp_path / "since_epoch"
  p.write_text("1234\n")
  assert read_rtc(str(p)) == 1234
  p.write_text("junk")
  assert read_rtc(str(p)) is None
  assert read_rtc(str(tmp_path / "missing")) is None


def test_save_load_round_trip(tmp_path):
  path = tmp_path / "new_dir" / "rtc_offset.json"
  assert save_offset(RTC, REAL_UTC, "gps", str(path)) == {"rtc": RTC, "utc": REAL_UTC, "source": "gps"}
  assert load_offset(str(path)) == {"rtc": RTC, "utc": REAL_UTC, "source": "gps"}
  assert [f.name for f in path.parent.iterdir()] == ["rtc_offset.json"]


def test_load_missing_or_corrupt(tmp_path):
  path = tmp_path / "rtc_offset.json"
  assert load_offset(str(path)) is None
  path.write_text("{not json")
  assert load_offset(str(path)) is None
  path.write_text(json.dumps({"rtc": 1}))
  assert load_offset(str(path)) is None


class Env:
  def __init__(self, tmp_path, monkeypatch, valid=False):
    self.rtc_path = tmp_path / "since_epoch"
    self.offset_path = tmp_path / "rtc_offset.json"
    self.ntp_path = tmp_path / "synchronized"
    self.now = float(REAL_UTC)
    self.mono = 1000.
    self.set_rtc(RTC)
    monkeypatch.setattr(rtc_clock, "system_time_valid", lambda: valid)
    monkeypatch.setattr(rtc_clock, "time", SimpleNamespace(time_ns=lambda: int(self.now * 1e9), monotonic=lambda: self.mono))

  def set_rtc(self, rtc):
    self.rtc_path.write_text(f"{rtc}\n")

  def clock(self):
    return RtcClock(str(self.rtc_path), str(self.offset_path), str(self.ntp_path))

  def saved(self):
    return load_offset(str(self.offset_path))


def test_restore_sets_time(tmp_path, monkeypatch):
  env = Env(tmp_path, monkeypatch)
  save_offset(RTC, REAL_UTC, "ntp", str(env.offset_path))
  env.set_rtc(RTC + 600)
  calls = []
  c = env.clock()
  c.restore(calls.append)
  assert calls == [REAL_TIME + datetime.timedelta(minutes=10)]
  assert c.restored


def test_restore_survives_absurd_offset(tmp_path, monkeypatch):
  env = Env(tmp_path, monkeypatch)
  save_offset(RTC, 10**20, "ntp", str(env.offset_path))
  calls = []
  env.clock().restore(calls.append)
  assert calls == []


def test_restore_skips_valid_clock_and_reset_rtc(tmp_path, monkeypatch):
  env = Env(tmp_path, monkeypatch, valid=True)
  save_offset(RTC, REAL_UTC, "ntp", str(env.offset_path))
  calls = []
  env.clock().restore(calls.append)
  monkeypatch.setattr(rtc_clock, "system_time_valid", lambda: False)
  env.set_rtc(5)
  env.clock().restore(calls.append)
  assert calls == []


def test_update_saves_only_when_trusted(tmp_path, monkeypatch):
  env = Env(tmp_path, monkeypatch)
  c = env.clock()
  c.update()
  assert env.saved() is None

  env.ntp_path.write_text("")
  c.update()
  assert env.saved() == {"rtc": RTC, "utc": REAL_UTC, "source": "ntp"}


def test_update_saves_first_trusted_cycle_even_if_unchanged(tmp_path, monkeypatch):
  env = Env(tmp_path, monkeypatch)
  save_offset(RTC - 500, REAL_UTC - 500, "ntp", str(env.offset_path))  # same offset, older RTC
  env.ntp_path.write_text("")
  env.clock().update()
  assert env.saved()["rtc"] == RTC


def test_gps_trusted_only_if_clock_matches(tmp_path, monkeypatch):
  env = Env(tmp_path, monkeypatch)
  monkeypatch.setattr(rtc_clock, "datetime", SimpleNamespace(datetime=_fixed_now(REAL_TIME), UTC=datetime.UTC,
                                                             timedelta=datetime.timedelta))
  c = env.clock()
  c.gps_set(REAL_TIME + datetime.timedelta(minutes=5))  # set_time failed
  c.update()
  assert env.saved() is None

  # clock 5 s behind GPS: set_time leaves it, so the save uses GPS time
  env.mono += 60
  c.gps_set(REAL_TIME + datetime.timedelta(seconds=5))
  c.update()
  assert env.saved() == {"rtc": RTC, "utc": REAL_UTC + 5, "source": "gps"}


def test_update_resaves_on_drift_or_age_only(tmp_path, monkeypatch):
  env = Env(tmp_path, monkeypatch)
  env.ntp_path.write_text("")
  c = env.clock()
  c.update()

  # 1 s of drift: kept
  env.mono += 60
  env.now += 100
  env.set_rtc(RTC + 99)
  c.update()
  assert env.saved()["rtc"] == RTC

  # rate limited: 5 s of drift but within a minute
  env.mono += 1
  env.now += 5
  c.update()
  assert env.saved()["rtc"] == RTC

  # 5 s of drift after a minute: saved
  env.mono += 60
  c.update()
  assert env.saved() == {"rtc": RTC + 99, "utc": REAL_UTC + 105, "source": "ntp"}

  # no drift but a day of RTC: saved
  env.mono += 60
  env.now += RESAVE_AGE
  env.set_rtc(RTC + 99 + RESAVE_AGE)
  c.update()
  assert env.saved()["rtc"] == RTC + 99 + RESAVE_AGE


def test_log_gps_correction_only_after_restore(tmp_path, monkeypatch):
  env = Env(tmp_path, monkeypatch)
  logs = []
  monkeypatch.setattr(rtc_clock.cloudlog, "warning", logs.append)
  c = env.clock()
  c.log_gps_correction(REAL_TIME)
  assert logs == []
  c.restored = True
  c.log_gps_correction(REAL_TIME)
  c.log_gps_correction(REAL_TIME)
  assert len(logs) == 1


def _fixed_now(now):
  class FixedDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
      return now.replace(tzinfo=tz)
  return FixedDatetime
