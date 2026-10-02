"""Whole-route playback for the Galaxy: one HLS playlist per route over the segments' qcamera.ts files.

qcamera.ts is already an MPEG-TS with H.264 and real PTS, so each file is served as-is as an HLS media
segment; nothing is remuxed. Kept out of the_galaxy.py so upstream ingests don't conflict with it.
"""
import math
import os

from flask import Blueprint, Response, send_file

from openpilot.starpilot.system.the_galaxy import utilities

QCAMERA_FILENAME = "qcamera.ts"
SEGMENT_SECONDS = 60.0


def _segment_lock_path(path):
  # loggerd writes "<file>.lock" next to a video while it is still being recorded
  return f"{path}.lock"


def _playable_segments(route_name, footage_paths):
  """[(segment, qcamera path)] for the first footage path holding finished qcamera files of the route."""
  for footage_path in footage_paths:
    try:
      segments = utilities.get_segments_in_route(route_name, footage_path)
    except OSError:
      continue
    playable = []
    for segment in segments:
      path = os.path.join(footage_path, segment, QCAMERA_FILENAME)
      if os.path.isfile(path) and not os.path.exists(_segment_lock_path(path)):
        playable.append((segment, path))
    if playable:
      return playable
  return []


def _last_segment_seconds(path):
  # Only the last segment of a route is normally short. One ffprobe call; fall back to a full minute.
  try:
    seconds = float(utilities.get_video_duration(path))
  except (OSError, ValueError):
    return SEGMENT_SECONDS
  return seconds if math.isfinite(seconds) and seconds > 0 else SEGMENT_SECONDS


def build_playlist(segments, last_seconds):
  durations = [SEGMENT_SECONDS] * len(segments)
  durations[-1] = last_seconds

  lines = [
    "#EXTM3U",
    "#EXT-X-VERSION:3",
    "#EXT-X-PLAYLIST-TYPE:VOD",
    f"#EXT-X-TARGETDURATION:{math.ceil(max(durations))}",
    "#EXT-X-MEDIA-SEQUENCE:0",
  ]
  for (segment, _), seconds in zip(segments, durations):
    # Every segment restarts its timestamps (and gaps from aged-out segments are possible).
    lines.append("#EXT-X-DISCONTINUITY")
    lines.append(f"#EXTINF:{seconds:.3f},")
    # Relative to /route-playback/<route>/, so it also resolves behind the Galaxy tunnel prefix.
    lines.append(f"../segment/{segment}/{QCAMERA_FILENAME}")
  lines.append("#EXT-X-ENDLIST")
  return "\n".join(lines) + "\n"


def create_blueprint(footage_paths):
  blueprint = Blueprint("route_playback", __name__, url_prefix="/route-playback")

  @blueprint.route("/<route_name>/qcamera.m3u8", methods=["GET"])
  def qcamera_playlist(route_name):
    if not utilities.ROUTE_RE.fullmatch(route_name or ""):
      return {"error": "Invalid route name"}, 400

    segments = _playable_segments(route_name, footage_paths)
    if not segments:
      return {"error": "No playable video for this route"}, 404

    response = Response(build_playlist(segments, _last_segment_seconds(segments[-1][1])), mimetype="application/vnd.apple.mpegurl")
    response.headers["Cache-Control"] = "no-store"
    return response

  @blueprint.route("/segment/<segment>/qcamera.ts", methods=["GET"])
  def qcamera_segment(segment):
    if not utilities.SEGMENT_RE.fullmatch(segment or ""):
      return {"error": "Invalid segment name"}, 400

    for footage_path in footage_paths:
      path = os.path.join(footage_path, segment, QCAMERA_FILENAME)
      if not os.path.isfile(path):
        continue
      if os.path.exists(_segment_lock_path(path)):
        return {"error": "Segment is still recording"}, 409
      # send_file handles Range and ETag itself.
      return send_file(path, mimetype="video/mp2t", conditional=True)
    return {"error": "Video not found"}, 404

  return blueprint


def register(app, footage_paths):
  app.register_blueprint(create_blueprint(footage_paths))
