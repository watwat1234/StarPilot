"""Engagement spans and the thumbnail of one segment's qlog, for the Galaxy route timeline.

Run as `python -m openpilot.starpilot.system.the_galaxy.route_timeline <qlog> <out_dir> [<qlog> <out_dir> ...]` in a
nice'd subprocess; cereal is only imported there, once per batch.
"""
import bz2
import json
import os
import re
import sys

QLOG_FILENAMES = ("qlog.zst", "qlog.bz2", "qlog")
TIMELINE_FILENAME = "timeline.json"
THUMBNAIL_FILENAME = "thumbnail.jpg"
# Part of the timeline cache key: bump it when the parsed output changes, so cached minutes are parsed again.
CACHE_VERSION = 2
# The camera writes one thumbnail per minute, a few seconds in; one this far in is the next minute's, written just
# before the rotation.
LATE_THUMBNAIL_SECONDS = 30
SEGMENT_DIR = re.compile(r"^(.+--.+)--(\d+)$")
ALERT_LEVELS = {"normal": 0, "userPrompt": 1, "critical": 2}


def _state(selfdrive_state):
  if selfdrive_state.active:
    return "engaged"
  return "overriding" if selfdrive_state.enabled else "disengaged"


def summarize(events):
  """events: (logMonoTime ns, which, message) in log order -> (timeline dict, thumbnail bytes or None, late thumbnail
  bytes or None).

  Every segment's qlog starts with a copy of initData stamped at the *route* start; the next message (the logger's
  sentinel) is the segment's t=0. A few messages logged before it are clamped to 0.
  """
  start = None
  spans = []
  thumbnail, thumbnail_at, late = None, None, None
  for mono_time, which, message in events:
    if start is None:
      if which != "initData":
        start = mono_time
      continue
    seconds = round(max(0.0, (mono_time - start) / 1e9), 2)
    if which == "selfdriveState":
      key = [_state(message), ALERT_LEVELS.get(str(message.alertStatus), 0)]
      if spans and spans[-1][2:] == key:
        spans[-1][1] = seconds
      else:
        if spans:
          spans[-1][1] = seconds
        spans.append([seconds, seconds, *key])
    elif which == "thumbnail" and seconds >= LATE_THUMBNAIL_SECONDS:
      late = bytes(message.thumbnail)
    elif which == "thumbnail" and thumbnail is None:
      thumbnail, thumbnail_at = bytes(message.thumbnail), seconds
  return {"spans": spans, "thumbnailAt": thumbnail_at}, thumbnail, late


def read_events(qlog_path):
  import capnp
  from cereal import log

  with open(qlog_path, "rb") as file:
    if qlog_path.endswith(".zst"):
      import zstandard
      data = zstandard.ZstdDecompressor().stream_reader(file).read()
    elif qlog_path.endswith(".bz2"):
      data = bz2.decompress(file.read())
    else:
      data = file.read()
  try:
    for event in log.Event.read_multiple_bytes(data):
      which = event.which()
      yield event.logMonoTime, which, getattr(event, which) if which in ("selfdriveState", "thumbnail") else None
  except capnp.KjException:
    return  # cut off mid-message (power lost while recording): keep what was read, as LogReader does


def previous_late_thumbnail(qlog_path):
  """This minute's thumbnail when it landed at the end of the previous segment's qlog, or None."""
  segment_dir = os.path.dirname(qlog_path)
  match = SEGMENT_DIR.match(os.path.basename(segment_dir))
  if match is None or int(match.group(2)) == 0:
    return None
  previous_dir = os.path.join(os.path.dirname(segment_dir), f"{match.group(1)}--{int(match.group(2)) - 1}")
  try:
    previous_qlog = next(path for name in QLOG_FILENAMES if os.path.isfile(path := os.path.join(previous_dir, name)))
    return summarize(read_events(previous_qlog))[2]
  except Exception:
    return None


def parse(qlog_path, out_dir):
  timeline, thumbnail, _ = summarize(read_events(qlog_path))
  if thumbnail is None:
    thumbnail = previous_late_thumbnail(qlog_path)
    if thumbnail is not None:
      timeline["thumbnailAt"] = 0.0  # written just before this minute started
  if thumbnail is not None:
    with open(os.path.join(out_dir, THUMBNAIL_FILENAME), "wb") as file:
      file.write(thumbnail)
  # timeline.json marks a finished pair (kept even if the batch is killed later), so it appears whole or not at all.
  partial_path = os.path.join(out_dir, TIMELINE_FILENAME + ".partial")
  with open(partial_path, "w") as file:
    json.dump(timeline, file, separators=(",", ":"))
  os.replace(partial_path, os.path.join(out_dir, TIMELINE_FILENAME))


def main(args):
  """Parse (qlog, out_dir) pairs in order. A failed pair doesn't stop the rest; only the first (requested) one sets
  the exit status."""
  status = 0
  for index in range(0, len(args) - 1, 2):
    try:
      parse(args[index], args[index + 1])
    except Exception as error:
      print(f"route_timeline: {args[index]}: {error!r}", file=sys.stderr)
      if index == 0:
        status = 1
  return status


if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
