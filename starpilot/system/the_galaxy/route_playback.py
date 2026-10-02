"""Whole-route playback for the Galaxy: one HLS playlist per route and camera.

Low quality: qcamera.ts is already an MPEG-TS with H.264 and real PTS, so each file is served as-is as an HLS
media segment; nothing is remuxed. Full quality: the raw f/e/dcamera.hevc of a segment is stream-copied to a
fragmented mp4 on first request and split into an fMP4 init + media part. Kept out of the_galaxy.py so upstream
ingests don't conflict with it.
"""
import hashlib
import math
import os
import shutil
import struct
import subprocess
import tempfile
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

from flask import Blueprint, Response, send_file

from openpilot.starpilot.system.the_galaxy import utilities

QCAMERA_FILENAME = "qcamera.ts"
CAMERA_FILENAMES = {"forward": "fcamera.hevc", "wide": "ecamera.hevc", "driver": "dcamera.hevc"}
INIT_FILENAME = "init.mp4"
MEDIA_FILENAME = "media.m4s"
SEGMENT_SECONDS = 60.0
FFPROBE_TIMEOUT_SECONDS = 5
# Same bounds as the upstream /video remux, whose one-worker executor this shares.
REMUX_WAIT_SECONDS = utilities.VIDEO_REMUX_TIMEOUT_SECONDS + 5
HLS_CACHE_MAX_BYTES = 512 * 1024 * 1024
# Unfinished full-quality remuxes (queued or running) allowed on the shared executor at once.
MAX_QUEUED_REMUXES = 2
TEMP_DIR_PREFIX = ".tmp-"


def _segment_lock_path(path):
  # loggerd writes "<file>.lock" next to a video while it is still being recorded
  return f"{path}.lock"


def _playable_segments(route_name, footage_paths, filename=QCAMERA_FILENAME):
  """[(segment, file path)] for the first footage path holding finished files of the route."""
  for footage_path in footage_paths:
    try:
      segments = utilities.get_segments_in_route(route_name, footage_path)
    except OSError:
      continue
    playable = []
    for segment in segments:
      path = os.path.join(footage_path, segment, filename)
      if os.path.isfile(path) and not os.path.exists(_segment_lock_path(path)):
        playable.append((segment, path))
    if playable:
      return playable
  return []


def _last_segment_seconds(path):
  # Only the last segment of a route is normally short. One bounded ffprobe call; fall back to a full minute.
  try:
    result = subprocess.run([
      utilities.FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
      "-of", "default=noprint_wrappers=1:nokey=1", path,
    ], capture_output=True, text=True, check=True, timeout=FFPROBE_TIMEOUT_SECONDS)
    seconds = float(result.stdout)
  except (OSError, ValueError, subprocess.SubprocessError):
    return SEGMENT_SECONDS
  return seconds if math.isfinite(seconds) and seconds > 0 else SEGMENT_SECONDS


def build_playlist(segments, last_seconds, camera=None):
  """VOD playlist over [(segment, path)]: qcamera.ts parts, or fMP4 init + media parts for `camera`."""
  durations = [SEGMENT_SECONDS] * len(segments)
  durations[-1] = last_seconds

  lines = [
    "#EXTM3U",
    "#EXT-X-VERSION:3" if camera is None else "#EXT-X-VERSION:7",
    "#EXT-X-PLAYLIST-TYPE:VOD",
    f"#EXT-X-TARGETDURATION:{math.ceil(max(durations))}",
    "#EXT-X-MEDIA-SEQUENCE:0",
  ]
  for (segment, _), seconds in zip(segments, durations, strict=True):
    # Every segment restarts its timestamps (and gaps from aged-out segments are possible).
    lines.append("#EXT-X-DISCONTINUITY")
    # Relative to /route-playback/<route>/, so it also resolves behind the Galaxy tunnel prefix.
    if camera is not None:
      lines.append(f'#EXT-X-MAP:URI="../segment/{segment}/{camera}/{INIT_FILENAME}"')
    lines.append(f"#EXTINF:{seconds:.3f},")
    lines.append(f"../segment/{segment}/{QCAMERA_FILENAME}" if camera is None else f"../segment/{segment}/{camera}/{MEDIA_FILENAME}")
  lines.append("#EXT-X-ENDLIST")
  return "\n".join(lines) + "\n"


def _init_size(path):
  """Byte length of the leading top-level boxes up to and including moov: the fMP4 init segment."""
  offset = 0
  with open(path, "rb") as file:
    while True:
      header = file.read(8)
      if len(header) < 8:
        raise ValueError(f"No moov box in {path}")
      size, kind = struct.unpack(">I4s", header)
      if size == 1:
        size = struct.unpack(">Q", file.read(8))[0]
      if size < 8:
        raise ValueError(f"Unsupported box size in {path}")
      offset += size
      if kind == b"moov":
        return offset
      file.seek(offset)


def _split_fmp4(joined_path, init_path, media_path):
  init_size = _init_size(joined_path)
  with open(joined_path, "rb") as source:
    with open(init_path, "wb") as init_file:
      init_file.write(source.read(init_size))
    with open(media_path, "wb") as media_file:
      shutil.copyfileobj(source, media_file)
  if os.path.getsize(media_path) == 0:
    raise ValueError(f"No media fragments in {joined_path}")


def _dir_size(path):
  total = 0
  for entry in path.iterdir():
    try:
      total += entry.stat().st_size
    except OSError:
      pass
  return total


def _prune_cache(cache_root, keep_path):
  """Evict whole segment dirs oldest-first until the cache fits its budget.

  Runs on the one-worker remux executor before each new remux, so any leftover temp dir is from a crash.
  """
  entries = []
  for path in cache_root.iterdir():
    if not path.is_dir() or path == keep_path:
      continue
    if path.name.startswith(TEMP_DIR_PREFIX):
      shutil.rmtree(path, ignore_errors=True)
      continue
    try:
      entries.append((path.stat().st_mtime, _dir_size(path), path))
    except OSError:
      continue

  total = sum(size for _, size, _ in entries)
  for _, size, path in sorted(entries):
    if total <= HLS_CACHE_MAX_BYTES:
      break
    shutil.rmtree(path, ignore_errors=True)
    total -= size


def _remux_to_fmp4(source_path, target_dir, cache_root):
  """Stream-copy one raw .hevc segment into target_dir/{init.mp4,media.m4s}. Runs on the remux executor."""
  if _is_complete(target_dir):
    return target_dir
  if os.path.exists(_segment_lock_path(source_path)):
    raise ValueError(f"File is still being recorded: {source_path}")

  cache_root.mkdir(parents=True, exist_ok=True)
  # A dir missing a part (an interrupted prune) would otherwise count as a hit forever.
  shutil.rmtree(target_dir, ignore_errors=True)
  _prune_cache(cache_root, keep_path=target_dir)
  temp_dir = Path(tempfile.mkdtemp(prefix=TEMP_DIR_PREFIX, dir=cache_root))
  try:
    joined_path = temp_dir / "joined.mp4"
    # The device ffmpeg build has no hls muxer, so write one fragmented mp4 and split it ourselves.
    subprocess.run([
      utilities.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-i", str(source_path),
      "-c", "copy", "-tag:v", "hvc1", "-f", "mp4",
      "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-y", str(joined_path),
    ], capture_output=True, check=True, timeout=utilities.VIDEO_REMUX_TIMEOUT_SECONDS)
    _split_fmp4(joined_path, temp_dir / INIT_FILENAME, temp_dir / MEDIA_FILENAME)
    joined_path.unlink()
    # Rename last, so a half-written remux is never served.
    os.rename(temp_dir, target_dir)
  except (OSError, ValueError, subprocess.SubprocessError) as error:
    shutil.rmtree(temp_dir, ignore_errors=True)
    raise ValueError(f"Cannot process video file: {source_path}") from error
  return target_dir


def _is_complete(target_dir):
  return (target_dir / INIT_FILENAME).is_file() and (target_dir / MEDIA_FILENAME).is_file()


def _cache_dir_for(cache_root, source_path):
  stat = os.stat(source_path)
  key = hashlib.md5(f"{source_path}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()
  return cache_root / key


def create_blueprint(footage_paths, remux_executor=None, cache_root=None):
  """remux_executor: the Galaxy's one-worker remux executor; without it full quality answers 503."""
  blueprint = Blueprint("route_playback", __name__, url_prefix="/route-playback")
  cache_root = Path(cache_root) if cache_root is not None else Path(utilities.VIDEO_CACHE_PATH) / "route-hls"
  remux_futures = {}
  remux_lock = threading.Lock()

  def forget_future(key, future):
    with remux_lock:
      if remux_futures.get(key) is future:
        remux_futures.pop(key, None)

  def remuxed_dir(source_path):
    """Cache dir with init.mp4 + media.m4s, or None if it is not ready in time or our backlog is full."""
    target_dir = _cache_dir_for(cache_root, source_path)
    if _is_complete(target_dir):
      # Cache hit: answered here, without queueing behind other remuxes. Touch for the oldest-first prune.
      try:
        os.utime(target_dir)
        return target_dir
      except FileNotFoundError:
        pass  # pruned just now: remux again below

    key = str(target_dir)
    created = False
    with remux_lock:
      future = remux_futures.get(key)
      if future is None:
        # Seeking abandons requests but not their jobs, and the executor is shared with /video. Cap our backlog;
        # the 503 is retried by hls.js once the queue drains.
        if len(remux_futures) >= MAX_QUEUED_REMUXES:
          return None
        future = remux_executor.submit(_remux_to_fmp4, source_path, target_dir, cache_root)
        remux_futures[key] = future
        created = True
    # Outside the lock: a callback on an already finished future runs right here and takes the lock.
    if created:
      future.add_done_callback(lambda completed: forget_future(key, completed))

    try:
      return future.result(timeout=REMUX_WAIT_SECONDS)
    except FutureTimeoutError:
      return None

  def playlist_response(body):
    response = Response(body, mimetype="application/vnd.apple.mpegurl")
    response.headers["Cache-Control"] = "no-store"
    return response

  @blueprint.route("/<route_name>/qcamera.m3u8", methods=["GET"])
  def qcamera_playlist(route_name):
    if not utilities.ROUTE_RE.fullmatch(route_name or ""):
      return {"error": "Invalid route name"}, 400

    segments = _playable_segments(route_name, footage_paths)
    if not segments:
      return {"error": "No playable video for this route"}, 404

    return playlist_response(build_playlist(segments, _last_segment_seconds(segments[-1][1])))

  @blueprint.route("/<route_name>/<camera>.m3u8", methods=["GET"])
  def camera_playlist(route_name, camera):
    if not utilities.ROUTE_RE.fullmatch(route_name or ""):
      return {"error": "Invalid route name"}, 400
    if camera not in CAMERA_FILENAMES:
      return {"error": "Invalid camera"}, 400

    segments = _playable_segments(route_name, footage_paths, CAMERA_FILENAMES[camera])
    if not segments:
      return {"error": "No playable video for this route"}, 404

    # Listing never remuxes. The last segment's length comes from its qcamera.ts when there is one.
    last_qcamera = os.path.join(os.path.dirname(segments[-1][1]), QCAMERA_FILENAME)
    last_seconds = _last_segment_seconds(last_qcamera) if os.path.isfile(last_qcamera) else SEGMENT_SECONDS
    return playlist_response(build_playlist(segments, last_seconds, camera=camera))

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

  @blueprint.route("/segment/<segment>/<camera>/<part>", methods=["GET"])
  def camera_segment(segment, camera, part):
    if not utilities.SEGMENT_RE.fullmatch(segment or ""):
      return {"error": "Invalid segment name"}, 400
    if camera not in CAMERA_FILENAMES or part not in (INIT_FILENAME, MEDIA_FILENAME):
      return {"error": "Invalid camera or part"}, 400
    if remux_executor is None:
      return {"error": "Full-quality playback is unavailable"}, 503

    for footage_path in footage_paths:
      source_path = os.path.join(footage_path, segment, CAMERA_FILENAMES[camera])
      if not os.path.isfile(source_path):
        continue
      if os.path.exists(_segment_lock_path(source_path)):
        return {"error": "Segment is still recording"}, 409
      try:
        target_dir = remuxed_dir(source_path)
      except (OSError, ValueError) as error:
        return {"error": str(error)}, 409
      if target_dir is None:
        return {"error": "Video is still being prepared"}, 503
      try:
        return send_file(target_dir / part, mimetype="video/mp4" if part == INIT_FILENAME else "video/iso.segment", conditional=True)
      except FileNotFoundError:
        # Pruned between the remux and this read; the player retries and the next request remuxes again.
        return {"error": "Video is still being prepared"}, 503
    return {"error": "Video not found"}, 404

  return blueprint


def register(app, footage_paths, remux_executor=None):
  app.register_blueprint(create_blueprint(footage_paths, remux_executor=remux_executor))
