"""Restore the wall clock at boot from the PMIC RTC.

The RTC keeps counting while the device is off but is never set to real time (AGNOS masks hwclock, and
timesyncd's clock file is on tmpfs). After a trusted sync (NTP or GPS) the offset UTC - RTC is saved; at boot
the clock is set to RTC + offset, so it is valid before any network or GPS fix.

Known limit: if the RTC is reset and then runs past the saved RTC value with no trusted sync in between, the
restored time is early by however long the RTC was off. GPS or NTP corrects it, and a restore is never saved.
"""
import datetime
import json
import os
import time

from openpilot.common.swaglog import cloudlog
from openpilot.common.time_helpers import min_date, MAX_DATE, system_time_valid

RTC_PATH = "/sys/class/rtc/rtc0/since_epoch"
OFFSET_PATH = "/data/starpilot/rtc_offset.json"
NTP_SYNCED_PATH = "/run/systemd/timesync/synchronized"
MAX_AGE = 60 * 24 * 3600  # seconds of RTC since the last save
RESAVE_DRIFT_S = 2
RESAVE_AGE = 24 * 3600  # keep the saved RTC recent when the device stays up for days
CHECK_INTERVAL = 60.
GPS_MATCH_S = 10  # timed's set_time skips smaller changes


def read_rtc(path=RTC_PATH):
  try:
    with open(path) as f:
      return int(f.read().strip())
  except (OSError, ValueError):
    return None


def load_offset(path=OFFSET_PATH):
  try:
    with open(path) as f:
      saved = json.load(f)
    return {"rtc": int(saved["rtc"]), "utc": int(saved["utc"]), "source": str(saved.get("source", ""))}
  except FileNotFoundError:
    return None
  except (OSError, ValueError, TypeError, KeyError):
    cloudlog.exception("rtc_clock.load_failed")
    return None


def save_offset(rtc, utc, source, path=OFFSET_PATH):
  saved = {"rtc": int(rtc), "utc": int(utc), "source": source}
  tmp = f"{path}.tmp"
  try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as f:
      json.dump(saved, f)
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp, path)
    return saved
  except OSError:
    cloudlog.exception("rtc_clock.save_failed")
    return None


def time_from_rtc(saved, rtc_now):
  """UTC time from the saved offset and the current RTC, as (naive datetime, None) or (None, reason)."""
  if saved is None:
    return None, "no saved offset"
  if rtc_now is None:
    return None, "no RTC"
  if rtc_now < saved["rtc"]:
    return None, f"RTC reset ({rtc_now} < {saved['rtc']})"
  if rtc_now - saved["rtc"] > MAX_AGE:
    return None, f"saved offset too old ({(rtc_now - saved['rtc']) // 86400} days)"
  t = datetime.datetime.fromtimestamp(saved["utc"] + rtc_now - saved["rtc"], datetime.UTC).replace(tzinfo=None)
  if not (min_date() <= t <= MAX_DATE):
    return None, f"out of range ({t})"
  return t, None


def ntp_synced(path=NTP_SYNCED_PATH):
  return os.path.exists(path)


class RtcClock:
  def __init__(self, rtc_path=RTC_PATH, offset_path=OFFSET_PATH, ntp_path=NTP_SYNCED_PATH):
    self.rtc_path = rtc_path
    self.offset_path = offset_path
    self.ntp_path = ntp_path
    self.saved = load_offset(offset_path)
    self.saved_this_boot = False
    self.last_check = -CHECK_INTERVAL
    self.restored = False
    self.correction_logged = False
    self.gps_trusted = False

  def restore(self, set_time):
    if system_time_valid():
      cloudlog.info("rtc_clock: clock already valid, not restoring")
      return
    rtc = read_rtc(self.rtc_path)
    t, reason = time_from_rtc(self.saved, rtc)
    if t is None:
      cloudlog.warning(f"rtc_clock: not restoring: {reason}")
      return
    s = self.saved
    cloudlog.warning(f"rtc_clock: restoring time to {t} (rtc {rtc}, offset {s['utc'] - s['rtc']}, from {s['source']})")
    set_time(t)
    self.restored = True

  def gps_set(self, gps_time):
    """Call after timed's set_time(gps_time). Trusts GPS only if the clock now matches it (date -s can fail)."""
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    if abs(now - gps_time) < datetime.timedelta(seconds=GPS_MATCH_S):
      self.gps_trusted = True

  def update(self):
    """Save the offset when the clock is trusted: on the first trusted cycle of the boot, then when it drifts.

    Trusted means NTP synced or GPS set the clock this boot; never the car clock or the restore itself.
    """
    # at most once a minute, also after a failed save (no log spam)
    if time.monotonic() - self.last_check < CHECK_INTERVAL:
      return
    source = "ntp" if ntp_synced(self.ntp_path) else "gps" if self.gps_trusted else None
    if source is None:
      return
    self.last_check = time.monotonic()
    rtc = read_rtc(self.rtc_path)
    if rtc is None:
      return
    utc = time.time_ns() // 1_000_000_000
    if self.saved_this_boot and self.saved is not None:
      drift = abs((utc - rtc) - (self.saved["utc"] - self.saved["rtc"]))
      if drift < RESAVE_DRIFT_S and rtc - self.saved["rtc"] < RESAVE_AGE:
        return
    saved = save_offset(rtc, utc, source, self.offset_path)
    if saved is not None:
      cloudlog.info(f"rtc_clock: saved offset {utc - rtc} from {source}")
      self.saved = saved
      self.saved_this_boot = True

  def log_gps_correction(self, gps_time):
    """Once per restored boot, log how far the restored clock was from GPS (drift data)."""
    if not self.restored or self.correction_logged:
      return
    self.correction_logged = True
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    cloudlog.warning(f"rtc_clock: first GPS time after restore, GPS - system = {(gps_time - now).total_seconds():.1f} s")
