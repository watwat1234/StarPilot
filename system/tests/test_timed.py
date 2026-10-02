import datetime
import time
from types import SimpleNamespace

from openpilot.common.time_helpers import min_date
from openpilot.system.timed import usable_gps

# relative to min_date() (the host's systemd build date), so the tests don't age out
REAL_TIME = (min_date() + datetime.timedelta(days=30)).replace(microsecond=0)
REAL_MS = int(REAL_TIME.replace(tzinfo=datetime.UTC).timestamp() * 1000)
STALE_NS = int((time.monotonic() - 10.0) * 1e9)


class FakeSubMaster:
  def __init__(self, msgs):
    # msgs: service -> (gps, updated, logMonoTime ns)
    self.msgs = msgs
    self.updated = {s: m[1] for s, m in msgs.items()}
    self.logMonoTime = {s: m[2] for s, m in msgs.items()}

  def __getitem__(self, service):
    return self.msgs[service][0]


def gps(ms=REAL_MS, fix=True):
  return SimpleNamespace(unixTimestampMillis=ms, hasFix=fix)


def fresh(g, updated=True):
  return (g, updated, time.monotonic_ns())


SERVICES = ["gpsLocationExternal", "gpsLocation"]


def test_car_feed_without_time_falls_back_to_device_gps():
  ublox = gps()
  sm = FakeSubMaster({"gpsLocationExternal": fresh(gps(ms=0)), "gpsLocation": fresh(ublox)})
  msg, t = usable_gps(sm, SERVICES)
  assert msg is ublox
  assert t == REAL_TIME


def test_primary_service_wins_when_both_are_usable():
  car = gps()
  sm = FakeSubMaster({"gpsLocationExternal": fresh(car), "gpsLocation": fresh(gps(ms=REAL_MS + 1000))})
  msg, t = usable_gps(sm, SERVICES)
  assert msg is car
  assert t == REAL_TIME


def test_rejects_stale_no_fix_not_updated_and_out_of_range():
  before_min = int((min_date() - datetime.timedelta(days=1)).replace(tzinfo=datetime.UTC).timestamp() * 1000)
  cases = [
    (gps(), True, STALE_NS),     # stale
    fresh(gps(fix=False)),       # no fix
    fresh(gps(), updated=False),  # not updated this cycle
    fresh(gps(ms=before_min)),   # before min_date (the device clock echoed back)
  ]
  for case in cases:
    sm = FakeSubMaster({"gpsLocationExternal": case, "gpsLocation": fresh(gps(ms=0))})
    assert usable_gps(sm, SERVICES) == (None, None)
