"""12V battery history: 10-minute voltage buckets plus one summary row per drive or park.

hardwared feeds BatteryMonitor.update() with the panda's instant voltage at 2 Hz. The Galaxy reads the
database through read_history() and latest_sample().

The device is off for most of a parked car's life (it shuts down DeviceShutdown hours after parking), so the
long-term signal is the per-park summary: the smoothed voltage 1/3/6 hours after parking, with the device's own
load included, plus the parked drop rate. Raw buckets are only kept for SAMPLE_RETENTION_S.
"""
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import DT_HW
from openpilot.common.swaglog import cloudlog
from openpilot.common.time_helpers import system_time_valid
from openpilot.system.hardware import PC
from openpilot.system.hardware.hw import Paths

SAMPLE_INTERVAL_S = 600
SAMPLE_RETENTION_S = 30 * 24 * 3600
TRIM_INTERVAL_S = 24 * 3600
RESUME_GAP_S = 15 * 60                 # a restart within this gap continues the open session instead of splitting it
PARK_OFFSETS_H = (1, 3, 6)
SMOOTHING_TAU_S = 45.                  # matches PowerMonitoring's car voltage filter
MAX_PENDING_BUCKETS = 24 * 3600 // SAMPLE_INTERVAL_S  # buckets held in memory while the clock is invalid
DEFAULT_CUTOFF_V = 11.8                # PowerMonitoring's VBATT_PAUSE_CHARGING, without importing it into the Galaxy

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts_start REAL NOT NULL,
  ts_end REAL NOT NULL,
  v_min REAL NOT NULL,
  v_mean REAL NOT NULL,
  v_max REAL NOT NULL,
  onroad INTEGER NOT NULL,
  draw_w REAL
);
CREATE INDEX IF NOT EXISTS samples_ts_end ON samples(ts_end);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  start_ts REAL NOT NULL,
  end_ts REAL NOT NULL,
  v_start REAL,
  v_end REAL,
  v_min REAL,
  v_max REAL,
  v_mean REAL,
  n INTEGER NOT NULL DEFAULT 0,
  v_at_1h REAL,
  v_at_3h REAL,
  v_at_6h REAL,
  start_flag TEXT NOT NULL,
  end_reason TEXT
);
CREATE INDEX IF NOT EXISTS sessions_end_ts ON sessions(end_ts);
"""


def default_db_path() -> str:
  root = Path(Paths.comma_home()) / "battery_monitor" if PC else Path("/data/battery_monitor")
  return str(root / "battery.db")


def _r(value: float | None) -> float | None:
  return None if value is None else round(value, 3)


@dataclass
class _Bucket:
  start: float
  end: float
  onroad: bool
  v_min: float = math.inf
  v_max: float = -math.inf
  v_sum: float = 0.
  draw_sum: float = 0.
  n: int = 0

  def add(self, now: float, v: float, draw_w: float):
    self.end = now
    self.v_min = min(self.v_min, v)
    self.v_max = max(self.v_max, v)
    self.v_sum += v
    self.draw_sum += draw_w
    self.n += 1

  def row(self, offset: float) -> tuple:
    return (self.start + offset, self.end + offset, _r(self.v_min), _r(self.v_sum / self.n), _r(self.v_max),
            int(self.onroad), round(self.draw_sum / self.n, 2))


@dataclass
class _Session:
  kind: str             # "drive" or "park"
  start: float          # monotonic
  last: float
  start_flag: str       # "clean" (ignition edge), "ign_on_at_boot" or "ign_off_at_boot"
  v_start: float | None = None
  v_end: float | None = None
  v_min: float = math.inf
  v_max: float = -math.inf
  v_sum: float = 0.
  n: int = 0
  v_at: dict = field(default_factory=dict)
  id: int | None = None
  end_reason: str | None = None

  def add(self, now: float, v: float, smoothed: float):
    if self.v_start is None:
      self.v_start = v
    self.last = now
    self.v_end = smoothed
    self.v_min = min(self.v_min, v)
    self.v_max = max(self.v_max, v)
    self.v_sum += v
    self.n += 1
    if self.kind == "park":
      for hours in PARK_OFFSETS_H:
        if hours not in self.v_at and now - self.start >= hours * 3600:
          self.v_at[hours] = smoothed

  def values(self, offset: float) -> dict:
    return {
      "kind": self.kind,
      "start_ts": self.start + offset,
      "end_ts": self.last + offset,
      "v_start": _r(self.v_start),
      "v_end": _r(self.v_end),
      "v_min": _r(self.v_min) if self.n else None,
      "v_max": _r(self.v_max) if self.n else None,
      "v_mean": _r(self.v_sum / self.n) if self.n else None,
      "n": self.n,
      "v_at_1h": _r(self.v_at.get(1)),
      "v_at_3h": _r(self.v_at.get(3)),
      "v_at_6h": _r(self.v_at.get(6)),
      "start_flag": self.start_flag,
      "end_reason": self.end_reason,
    }


class BatteryMonitor:
  # Rows outlive reboots, so they are stamped with wall time (once it is valid), not monotonic time
  def __init__(self, db_path: str | None = None, clock_valid=system_time_valid, wall_time=time.time):  # noqa: TID251
    self.db_path = db_path or default_db_path()
    self._clock_valid = clock_valid
    self._wall_time = wall_time
    self._filter = FirstOrderFilter(0., SMOOTHING_TAU_S, DT_HW, initialized=False)
    self._db: sqlite3.Connection | None = None
    self._offset: float | None = None   # wall time minus monotonic time, fixed once the clock is valid
    self._pending: list[_Bucket] = []
    self._bucket: _Bucket | None = None
    self._session: _Session | None = None
    self._ended: list[_Session] = []    # finished sessions not yet written
    self._reconciled = False
    self._last_trim: float | None = None
    self._closed = False

  def update(self, now: float, voltage_mv: float | None, ignition: bool, power_draw_w: float = 0.):
    """Feed one reading. now is time.monotonic(); voltage_mv is the panda's instant voltage."""
    if self._closed or not voltage_mv or voltage_mv <= 0:
      return
    v = voltage_mv / 1000.
    smoothed = self._filter.update(v)

    kind = "drive" if ignition else "park"
    edge = False
    if self._session is None:
      self._session = _Session(kind, now, now, "ign_on_at_boot" if ignition else "ign_off_at_boot")
    elif self._session.kind != kind:
      self._end_bucket()
      self._end_session("ignition")
      self._session = _Session(kind, now, now, "clean")
      edge = True

    if self._bucket is None:
      self._bucket = _Bucket(now, now, ignition)
    self._bucket.add(now, v, power_draw_w)
    self._session.add(now, v, smoothed)

    if now - self._bucket.start >= SAMPLE_INTERVAL_S:
      self._end_bucket()
      self._flush(now)
    elif edge:
      self._flush(now)

  def close(self, reason: str, now: float | None = None):
    """End the open session (e.g. reason="low_voltage" before a shutdown) and write everything out."""
    if self._closed:
      return
    now = time.monotonic() if now is None else now
    self._end_bucket()
    if self._session is not None:
      self._end_session(reason)
    self._flush(now)
    self._closed = True

  def _end_bucket(self):
    if self._bucket is not None and self._bucket.n:
      self._pending.append(self._bucket)
      del self._pending[:-MAX_PENDING_BUCKETS]
    self._bucket = None

  def _end_session(self, reason: str):
    self._session.end_reason = reason
    self._ended.append(self._session)
    self._session = None

  def _flush(self, now: float):
    if self._offset is None:
      if not self._clock_valid():
        return
      self._offset = self._wall_time() - now

    sessions = self._ended + ([self._session] if self._session is not None else [])
    try:
      db = self._connect()
      if not self._reconciled:
        self._reconcile(db, sessions)
        self._reconciled = True

      inserted = [session for session in sessions if session.id is None]
      trim = self._last_trim is None or now - self._last_trim >= TRIM_INTERVAL_S
      try:
        with db:
          db.executemany("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)", [b.row(self._offset) for b in self._pending])
          for session in sessions:
            self._write_session(db, session)
          if trim:
            db.execute("DELETE FROM samples WHERE ts_end < ?", (now + self._offset - SAMPLE_RETENTION_S,))
      except sqlite3.Error:
        for session in inserted:  # rolled back, so insert them again next time
          session.id = None
        raise
    except (sqlite3.Error, OSError):
      cloudlog.exception("battery_monitor: write failed")
      return

    if trim:
      self._last_trim = now
    self._pending.clear()
    self._ended.clear()

  def _connect(self) -> sqlite3.Connection:
    if self._db is None:
      os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
      db = sqlite3.connect(self.db_path, timeout=1.0)
      db.execute("PRAGMA journal_mode=WAL")
      db.execute("PRAGMA synchronous=NORMAL")
      db.executescript(SCHEMA)
      self._db = db
    return self._db

  def _reconcile(self, db: sqlite3.Connection, sessions: list[_Session]):
    """Close sessions a previous run left open, or continue one if this run started right where it stopped."""
    columns = "id, kind, start_ts, end_ts, v_start, v_min, v_max, v_mean, n, v_at_1h, v_at_3h, v_at_6h, start_flag"
    rows = db.execute(f"SELECT {columns} FROM sessions WHERE end_reason IS NULL ORDER BY end_ts").fetchall()
    if not rows:
      return
    first = sessions[0] if sessions else None
    *stale, last = rows
    sid, kind, start_ts, end_ts, v_start, v_min, v_max, v_mean, n, *v_at, start_flag = last
    resume = first is not None and first.start_flag != "clean" and first.kind == kind and \
             0 <= first.start + self._offset - end_ts <= RESUME_GAP_S
    if not resume:
      stale.append(last)
    with db:
      db.executemany("UPDATE sessions SET end_reason = 'unknown' WHERE id = ?", [(row[0],) for row in stale])

    # Merge only after the commit, so a failed write can't leave the in-memory session half-merged
    if resume:
      first.id = sid
      first.start = start_ts - self._offset
      first.start_flag = start_flag
      first.v_start = v_start if v_start is not None else first.v_start
      if n:
        first.v_min = min(first.v_min, v_min)
        first.v_max = max(first.v_max, v_max)
        first.v_sum += v_mean * n
        first.n += n
      for hours, value in zip(PARK_OFFSETS_H, v_at, strict=True):
        if value is not None:
          first.v_at[hours] = value

  def _write_session(self, db: sqlite3.Connection, session: _Session):
    values = session.values(self._offset)
    if session.id is None:
      columns = ", ".join(values)
      cursor = db.execute(f"INSERT INTO sessions ({columns}) VALUES ({', '.join('?' * len(values))})", tuple(values.values()))
      session.id = cursor.lastrowid
    else:
      assignments = ", ".join(f"{column} = ?" for column in values)
      db.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", (*values.values(), session.id))


def _read_connection(db_path: str) -> sqlite3.Connection | None:
  if not os.path.isfile(db_path):
    return None
  db = sqlite3.connect(db_path, timeout=1.0)
  db.execute("PRAGMA query_only = ON")
  db.row_factory = sqlite3.Row
  return db


def read_history(days: float, db_path: str | None = None, now: float | None = None) -> dict:
  """Samples and sessions that ended within the last `days` days (open sessions included), oldest first."""
  now = time.time() if now is None else now  # noqa: TID251
  since = now - days * 86400
  db = _read_connection(db_path or default_db_path())
  if db is None:
    return {"samples": [], "sessions": []}
  try:
    samples = db.execute("SELECT ts_start, ts_end, v_min, v_mean, v_max, onroad, draw_w FROM samples WHERE ts_end >= ? ORDER BY ts_start",
                         (since,)).fetchall()
    sessions = db.execute("SELECT * FROM sessions WHERE end_ts >= ? OR end_reason IS NULL ORDER BY start_ts", (since,)).fetchall()
  finally:
    db.close()
  return {
    "samples": [{**dict(row), "onroad": bool(row["onroad"])} for row in samples],
    "sessions": [dict(row) for row in sessions],
  }


def latest_sample(db_path: str | None = None) -> dict | None:
  db = _read_connection(db_path or default_db_path())
  if db is None:
    return None
  try:
    row = db.execute("SELECT ts_end, v_mean, onroad FROM samples ORDER BY ts_end DESC LIMIT 1").fetchone()
  finally:
    db.close()
  return None if row is None else {"ts": row["ts_end"], "voltage": row["v_mean"], "onroad": bool(row["onroad"])}
