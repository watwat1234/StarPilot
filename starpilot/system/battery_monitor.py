"""12V battery history: 10-minute voltage buckets plus one summary row per drive or park.

hardwared feeds BatteryMonitor.update() with the panda's instant voltage at 2 Hz. update() only does in-memory
work; SQLite writes happen on a background writer thread, so a slow disk can never stall hardwared's loop.
The Galaxy reads the database through read_history() and latest_sample().

The device is off for most of a parked car's life (it shuts down DeviceShutdown hours after parking), so the
long-term signal is the per-park summary: the smoothed voltage 1/3/6 hours after parking, with the device's own
load included, plus the parked drop rate. Raw buckets are only kept for SAMPLE_RETENTION_S.
"""
import math
import os
import queue
import sqlite3
import threading
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
STOP_TIMEOUT_S = 2.0                   # how long stop() waits for the writer; manager SIGKILLs hardwared after 5 s
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
  seq: int = 0          # identifies the session to the writer, which owns its database id
  v_start: float | None = None
  v_end: float | None = None
  v_min: float = math.inf
  v_max: float = -math.inf
  v_sum: float = 0.
  n: int = 0
  v_at: dict = field(default_factory=dict)
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
    self._writer = _Writer(self.db_path)
    self._offset: float | None = None   # wall time minus monotonic time, measured at each flush once the clock is valid
    self._pending: list[_Bucket] = []   # finished buckets not yet handed to the writer
    self._bucket: _Bucket | None = None
    self._session: _Session | None = None
    self._ended: list[_Session] = []    # finished sessions not yet handed to the writer
    self._seq = 0
    self._closed = False

  def update(self, now: float, voltage_mv: float | None, ignition: bool, power_draw_w: float = 0.):
    """Feed one reading. now is time.monotonic(); voltage_mv is the panda's instant voltage."""
    if self._closed or not voltage_mv or voltage_mv <= 0:
      return
    self._apply_resume(now)
    v = voltage_mv / 1000.
    smoothed = self._filter.update(v)

    kind = "drive" if ignition else "park"
    edge = False
    if self._session is None:
      self._session = self._new_session(kind, now, "ign_on_at_boot" if ignition else "ign_off_at_boot")
    elif self._session.kind != kind:
      self._end_bucket()
      self._end_session("ignition")
      self._session = self._new_session(kind, now, "clean")
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
    """End the open session (e.g. reason="low_voltage" when hardwared decides to shut down) and hand everything
    to the writer. Doesn't wait for it; stop() does that once hardwared's loop exits."""
    if self._closed:
      return
    now = time.monotonic() if now is None else now
    self._end_bucket()
    if self._session is not None:
      self._end_session(reason)
    self._flush(now, final=True)
    self._closed = True

  def stop(self, now: float | None = None, timeout: float | None = STOP_TIMEOUT_S) -> bool:
    """hardwared is exiting: hand off the open bucket and session, then wait up to timeout for the writer.
    The session is left open, so the next run resumes it or closes it as "unknown"."""
    if not self._closed:
      now = time.monotonic() if now is None else now
      self._end_bucket()
      self._flush(now, final=True)
      self._closed = True
    return self._writer.drain(timeout)

  def drain(self, timeout: float | None = None) -> bool:
    """Wait until everything handed to the writer so far is processed."""
    return self._writer.drain(timeout)

  def _new_session(self, kind: str, now: float, start_flag: str) -> _Session:
    self._seq += 1
    return _Session(kind, now, now, start_flag, seq=self._seq)

  def _apply_resume(self, now: float):
    # The writer found that this run continues a session the previous run left open: adopt its start, so
    # v_at_Nh is measured from the real start of the park
    resumed = self._writer.resumed()
    if resumed is None or self._session is None or self._session.seq != resumed["seq"]:
      return
    session = self._session
    session.start = resumed["start_ts"] - self._offset
    v_at = {}
    for hours in PARK_OFFSETS_H:
      if hours in resumed["v_at"]:
        v_at[hours] = resumed["v_at"][hours]
      elif session.start + hours * 3600 <= now:
        # the mark fell while the device was off (or earlier in this run, timed from the wrong start): there is
        # no reading from then, so leave it empty rather than store a later one
        v_at[hours] = None
    session.v_at = v_at

  def _end_bucket(self):
    if self._bucket is not None and self._bucket.n:
      self._pending.append(self._bucket)
      del self._pending[:-MAX_PENDING_BUCKETS]
    self._bucket = None

  def _end_session(self, reason: str):
    self._session.end_reason = reason
    self._ended.append(self._session)
    self._session = None

  def _flush(self, now: float, final: bool = False):
    # Measured again at every flush, so a later NTP/GPS correction also moves the rows stamped after it
    if self._clock_valid():
      self._offset = self._wall_time() - now
    elif self._offset is None:
      if final:
        cloudlog.warning(f"battery_monitor: wall clock never became valid, dropping {len(self._pending)} buckets")
      return

    sessions = self._ended + ([self._session] if self._session is not None else [])
    self._writer.submit(_Snapshot(
      rows=[b.row(self._offset) for b in self._pending],
      sessions=[(s.seq, s.values(self._offset)) for s in sessions],
      wall_now=now + self._offset,
    ))
    self._pending.clear()
    self._ended.clear()


@dataclass
class _Snapshot:
  rows: list[tuple]                 # finished buckets
  sessions: list[tuple[int, dict]]  # (seq, column values), oldest first; the open session last
  wall_now: float


class _Writer:
  """Owns the SQLite connection and does every write on its own thread. Data from a failed write is kept and
  retried with the next snapshot."""
  def __init__(self, db_path: str):
    self.db_path = db_path
    self._queue: queue.Queue = queue.Queue()
    self._resumed: queue.Queue = queue.Queue()
    self._thread: threading.Thread | None = None
    self._db: sqlite3.Connection | None = None
    self._rows: list[tuple] = []        # not yet written
    self._sessions: dict[int, dict] = {}  # seq -> latest values, not yet written
    self._ids: dict[int, int] = {}      # seq -> sessions.id
    self._base: dict[int, dict] = {}    # seq -> the row a resumed session continues
    self._reconciled = False
    self._last_trim: float | None = None

  def submit(self, snapshot: _Snapshot):
    if self._thread is None:
      self._thread = threading.Thread(target=self._run, name="battery-monitor-writer", daemon=True)
      self._thread.start()
    self._queue.put(snapshot)

  def drain(self, timeout: float | None = None) -> bool:
    if self._thread is None:
      return True
    done = threading.Event()
    self._queue.put(done)
    return done.wait(timeout)

  def resumed(self) -> dict | None:
    try:
      return self._resumed.get_nowait()
    except queue.Empty:
      return None

  def _run(self):
    while True:
      item = self._queue.get()
      if isinstance(item, threading.Event):
        item.set()
        continue
      try:
        self._write(item)
      except Exception:
        cloudlog.exception("battery_monitor: writer failed")

  def _write(self, snapshot: _Snapshot):
    self._rows += snapshot.rows
    del self._rows[:-MAX_PENDING_BUCKETS]
    for seq, values in snapshot.sessions:
      self._sessions[seq] = values

    inserted: list[int] = []
    try:
      db = self._connect()
      if not self._reconciled:
        self._reconcile(db)
        self._reconciled = True

      trim = self._last_trim is None or snapshot.wall_now - self._last_trim >= TRIM_INTERVAL_S
      try:
        with db:
          db.executemany("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)", self._rows)
          for seq, values in self._sessions.items():
            if self._write_session(db, seq, self._merged(seq, values)):
              inserted.append(seq)
          if trim:
            db.execute("DELETE FROM samples WHERE ts_end < ?", (snapshot.wall_now - SAMPLE_RETENTION_S,))
      except sqlite3.Error:
        for seq in inserted:  # rolled back, so insert them again next time
          del self._ids[seq]
        raise
    except (sqlite3.Error, OSError):
      cloudlog.exception("battery_monitor: write failed")
      return

    if trim:
      self._last_trim = snapshot.wall_now
    for seq, values in self._sessions.items():
      if values["end_reason"] is not None:  # finished; it won't be sent again
        self._ids.pop(seq, None)
        self._base.pop(seq, None)
    self._rows.clear()
    self._sessions.clear()

  def _connect(self) -> sqlite3.Connection:
    if self._db is None:
      os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
      db = sqlite3.connect(self.db_path, timeout=1.0)
      db.execute("PRAGMA journal_mode=WAL")
      db.execute("PRAGMA synchronous=NORMAL")
      db.executescript(SCHEMA)
      self._db = db
    return self._db

  def _reconcile(self, db: sqlite3.Connection):
    """Close sessions a previous run left open, or continue one if this run started right where it stopped."""
    columns = "id, kind, start_ts, end_ts, v_start, v_min, v_max, v_mean, n, v_at_1h, v_at_3h, v_at_6h, start_flag"
    rows = db.execute(f"SELECT {columns} FROM sessions WHERE end_reason IS NULL ORDER BY end_ts").fetchall()
    if not rows:
      return
    seq, first = next(iter(self._sessions.items()), (None, None))
    *stale, last = rows
    sid, kind, start_ts, end_ts, v_start, v_min, v_max, v_mean, n, *v_at, start_flag = last
    resume = first is not None and first["start_flag"] != "clean" and first["kind"] == kind and \
             0 <= first["start_ts"] - end_ts <= RESUME_GAP_S
    if not resume:
      stale.append(last)
    with db:
      db.executemany("UPDATE sessions SET end_reason = 'unknown' WHERE id = ?", [(row[0],) for row in stale])

    # Record the merge only after the commit, so a failed write can't leave it half done
    if resume:
      self._ids[seq] = sid
      at = {hours: value for hours, value in zip(PARK_OFFSETS_H, v_at, strict=True) if value is not None}
      self._base[seq] = {"start_ts": start_ts, "start_flag": start_flag, "v_start": v_start, "v_min": v_min,
                         "v_max": v_max, "v_mean": v_mean, "n": n, "v_at": at}
      self._resumed.put({"seq": seq, "start_ts": start_ts, "v_at": at})

  def _merged(self, seq: int, values: dict) -> dict:
    base = self._base.get(seq)
    if base is None:
      return values
    merged = {**values, "start_ts": base["start_ts"], "start_flag": base["start_flag"]}
    if base["v_start"] is not None:
      merged["v_start"] = base["v_start"]
    if base["n"]:
      n = values["n"] + base["n"]
      merged["n"] = n
      merged["v_min"] = min(v for v in (values["v_min"], base["v_min"]) if v is not None)
      merged["v_max"] = max(v for v in (values["v_max"], base["v_max"]) if v is not None)
      merged["v_mean"] = _r(((values["v_mean"] or 0.) * values["n"] + base["v_mean"] * base["n"]) / n)
    for hours, value in base["v_at"].items():
      merged[f"v_at_{hours}h"] = value
    return merged

  def _write_session(self, db: sqlite3.Connection, seq: int, values: dict) -> bool:
    """Insert or update one session row; True when it was inserted."""
    sid = self._ids.get(seq)
    if sid is None:
      columns = ", ".join(values)
      cursor = db.execute(f"INSERT INTO sessions ({columns}) VALUES ({', '.join('?' * len(values))})", tuple(values.values()))
      self._ids[seq] = cursor.lastrowid
      return True
    assignments = ", ".join(f"{column} = ?" for column in values)
    db.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", (*values.values(), sid))
    return False


def _read_connection(db_path: str) -> sqlite3.Connection | None:
  if not os.path.isfile(db_path):
    return None
  db = sqlite3.connect(db_path, timeout=1.0)
  db.execute("PRAGMA query_only = ON")
  db.row_factory = sqlite3.Row
  return db


def read_history(days: float, db_path: str | None = None, now: float | None = None, include_samples: bool = True) -> dict:
  """Samples and sessions that ended within the last `days` days (open sessions included), oldest first."""
  now = time.time() if now is None else now  # noqa: TID251
  since = now - days * 86400
  db = _read_connection(db_path or default_db_path())
  if db is None:
    return {"samples": [], "sessions": []}
  try:
    samples = []
    if include_samples:
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
