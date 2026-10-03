"""Engagement spans and the thumbnail of one segment's qlog, for the Galaxy route timeline.

Run as `python -m openpilot.starpilot.system.the_galaxy.route_timeline <qlog> <out_dir>` in a nice'd subprocess;
cereal is only imported there.
"""
import bz2
import json
import os
import sys

TIMELINE_FILENAME = "timeline.json"
THUMBNAIL_FILENAME = "thumbnail.jpg"
ALERT_LEVELS = {"normal": 0, "userPrompt": 1, "critical": 2}


def _state(selfdrive_state):
  if selfdrive_state.active:
    return "engaged"
  return "overriding" if selfdrive_state.enabled else "disengaged"


def summarize(events):
  """events: (logMonoTime ns, which, message) in log order -> (timeline dict, thumbnail bytes or None).

  Every segment's qlog starts with a copy of initData stamped at the *route* start; the next message (the logger's
  sentinel) is the segment's t=0. A few messages logged before it are clamped to 0.
  """
  start = None
  spans = []
  thumbnail, thumbnail_at = None, None
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
    elif which == "thumbnail" and thumbnail is None:
      thumbnail, thumbnail_at = bytes(message.thumbnail), seconds
  return {"spans": spans, "thumbnailAt": thumbnail_at}, thumbnail


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


def main(qlog_path, out_dir):
  timeline, thumbnail = summarize(read_events(qlog_path))
  if thumbnail is not None:
    with open(os.path.join(out_dir, THUMBNAIL_FILENAME), "wb") as file:
      file.write(thumbnail)
  with open(os.path.join(out_dir, TIMELINE_FILENAME), "w") as file:
    json.dump(timeline, file, separators=(",", ":"))


if __name__ == "__main__":
  main(sys.argv[1], sys.argv[2])
