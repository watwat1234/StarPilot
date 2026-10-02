import os
import shutil
import struct
import subprocess
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from flask import Flask

from test_dashboard_stats import FakeParams, MODULE_DIR, _install_server_import_stubs

_install_server_import_stubs()

from openpilot.starpilot.system.the_galaxy import route_playback


ROUTE_NAME = "0000006a--9f0a7bdf9c"
PLAYLIST_URL = f"/route-playback/{ROUTE_NAME}/qcamera.m3u8"


def _make_segment(root, segment_num, route_name=ROUTE_NAME, payload=b"ts", locked=False):
  segment = root / f"{route_name}--{segment_num}"
  segment.mkdir(parents=True)
  (segment / "qcamera.ts").write_bytes(payload)
  if locked:
    (segment / "qcamera.ts.lock").touch()
  return segment


def _fake_ffprobe(monkeypatch, outcome):
  calls = []
  def run(cmd, **kwargs):
    calls.append((cmd, kwargs))
    if isinstance(outcome, BaseException):
      raise outcome
    return SimpleNamespace(stdout=f"{outcome()}\n")
  monkeypatch.setattr(route_playback.subprocess, "run", run)
  return calls


@pytest.fixture
def last_duration(monkeypatch):
  durations = {"seconds": 23.5}
  durations["calls"] = _fake_ffprobe(monkeypatch, lambda: durations["seconds"])
  return durations


def _client(*roots):
  app = Flask(f"route_playback_{time.monotonic_ns()}")
  route_playback.register(app, footage_paths=[str(root) + "/" for root in roots])
  return app.test_client()


def _entries(body):
  lines = body.splitlines()
  return [line for line in lines if line and not line.startswith("#")], lines


def test_playlist_lists_every_segment_in_numeric_order(tmp_path, last_duration):
  for num in (0, 1, 2, 10):
    _make_segment(tmp_path, num)

  response = _client(tmp_path).get(PLAYLIST_URL)

  assert response.status_code == 200
  assert response.mimetype == "application/vnd.apple.mpegurl"
  assert response.headers["Cache-Control"] == "no-store"
  body = response.get_data(as_text=True)
  uris, lines = _entries(body)
  assert lines[:5] == [
    "#EXTM3U",
    "#EXT-X-VERSION:3",
    "#EXT-X-PLAYLIST-TYPE:VOD",
    "#EXT-X-TARGETDURATION:60",
    "#EXT-X-MEDIA-SEQUENCE:0",
  ]
  assert uris == [f"../segment/{ROUTE_NAME}--{num}/qcamera.ts" for num in (0, 1, 2, 10)]
  assert lines.count("#EXT-X-DISCONTINUITY") == 4
  assert [line for line in lines if line.startswith("#EXTINF")] == ["#EXTINF:60.000,"] * 3 + ["#EXTINF:23.500,"]
  assert lines[-1] == "#EXT-X-ENDLIST"


def test_playlist_skips_segments_still_recording_and_missing_qcamera(tmp_path, last_duration):
  _make_segment(tmp_path, 0)
  (tmp_path / f"{ROUTE_NAME}--1").mkdir()
  _make_segment(tmp_path, 2, locked=True)

  uris, _ = _entries(_client(tmp_path).get(PLAYLIST_URL).get_data(as_text=True))

  assert uris == [f"../segment/{ROUTE_NAME}--0/qcamera.ts"]


def test_playlist_target_duration_covers_a_long_last_segment(tmp_path, last_duration):
  _make_segment(tmp_path, 0)
  last_duration["seconds"] = 60.4

  _, lines = _entries(_client(tmp_path).get(PLAYLIST_URL).get_data(as_text=True))

  assert "#EXT-X-TARGETDURATION:61" in lines


@pytest.mark.parametrize("failure", [
  FileNotFoundError("ffprobe"),
  subprocess.TimeoutExpired("ffprobe", route_playback.FFPROBE_TIMEOUT_SECONDS),
  subprocess.CalledProcessError(1, "ffprobe"),
])
def test_playlist_falls_back_to_a_full_minute_when_ffprobe_fails(tmp_path, monkeypatch, failure):
  _make_segment(tmp_path, 0)
  _fake_ffprobe(monkeypatch, failure)

  _, lines = _entries(_client(tmp_path).get(PLAYLIST_URL).get_data(as_text=True))

  assert "#EXTINF:60.000," in lines


def test_ffprobe_runs_once_on_the_last_segment_with_a_timeout(tmp_path, last_duration):
  for num in (0, 1):
    _make_segment(tmp_path, num)

  _client(tmp_path).get(PLAYLIST_URL)

  [(cmd, kwargs)] = last_duration["calls"]
  assert cmd[-1].endswith(f"{ROUTE_NAME}--1/qcamera.ts")
  assert kwargs["timeout"] == route_playback.FFPROBE_TIMEOUT_SECONDS


def test_playlist_uses_the_first_footage_path_with_playable_segments(tmp_path, last_duration):
  empty, filled = tmp_path / "hd", tmp_path / "raw"
  empty.mkdir()
  _make_segment(filled, 3)

  uris, _ = _entries(_client(empty, filled).get(PLAYLIST_URL).get_data(as_text=True))

  assert uris == [f"../segment/{ROUTE_NAME}--3/qcamera.ts"]


def test_playlist_rejects_bad_names_and_unknown_routes(tmp_path, last_duration):
  client = _client(tmp_path)

  assert client.get("/route-playback/not-a-route/qcamera.m3u8").status_code == 400
  assert client.get(f"/route-playback/{ROUTE_NAME}--0/qcamera.m3u8").status_code == 400
  assert client.get(PLAYLIST_URL).status_code == 404


def test_segment_is_served_as_mpeg_ts_with_range(tmp_path):
  _make_segment(tmp_path, 0, payload=bytes(range(100)))
  client = _client(tmp_path)

  with client.get(f"/route-playback/segment/{ROUTE_NAME}--0/qcamera.ts") as response:
    assert response.status_code == 200
    assert response.mimetype == "video/mp2t"
    assert response.data == bytes(range(100))

  with client.get(f"/route-playback/segment/{ROUTE_NAME}--0/qcamera.ts", headers={"Range": "bytes=10-19"}) as partial:
    assert partial.status_code == 206
    assert partial.data == bytes(range(10, 20))


def test_segment_rejects_bad_names_locked_and_missing_files(tmp_path):
  _make_segment(tmp_path, 1, locked=True)
  client = _client(tmp_path)

  assert client.get(f"/route-playback/segment/{ROUTE_NAME}/qcamera.ts").status_code == 400
  assert client.get("/route-playback/segment/..%2F..%2Fetc/qcamera.ts").status_code in (400, 404)
  assert client.get(f"/route-playback/segment/{ROUTE_NAME}--1/qcamera.ts").status_code == 409
  assert client.get(f"/route-playback/segment/{ROUTE_NAME}--2/qcamera.ts").status_code == 404


def test_galaxy_setup_registers_route_playback(monkeypatch, tmp_path, last_duration):
  import importlib.util
  import sys

  spec = importlib.util.spec_from_file_location("route_playback_server", MODULE_DIR / "the_galaxy.py")
  the_galaxy = importlib.util.module_from_spec(spec)
  sys.modules["route_playback_server"] = the_galaxy
  spec.loader.exec_module(the_galaxy)
  assert the_galaxy._import_galaxy_web_symbols()
  monkeypatch.setattr(the_galaxy, "FOOTAGE_PATHS", [str(tmp_path) + "/"])
  monkeypatch.setattr(the_galaxy, "params", FakeParams())
  _make_segment(tmp_path, 0)

  app = the_galaxy.Flask(
    f"route_playback_setup_{time.monotonic_ns()}",
    template_folder=str(MODULE_DIR / "templates"),
    static_folder=str(MODULE_DIR / "assets"),
  )
  the_galaxy.setup(app)

  assert "route_playback" in app.blueprints
  response = app.test_client().get(PLAYLIST_URL)
  assert response.status_code == 200
  assert f"../segment/{ROUTE_NAME}--0/qcamera.ts" in response.get_data(as_text=True)


def test_recordings_plays_the_whole_route_through_the_hls_route_player():
  mobile = MODULE_DIR / "assets/mobile/js"
  recordings = (mobile / "views/Recordings.js").read_text(encoding="utf-8")
  player = (mobile / "components/RoutePlayer.js").read_text(encoding="utf-8")
  vendor = MODULE_DIR / "assets/vendor/hls.js"

  # Road camera defaults to the whole-route player; other cameras and the full-quality toggle keep segments.
  assert 'import { RoutePlayer } from "../components/RoutePlayer.js"' in recordings
  assert '<RoutePlayer v-if="usingRoutePlayer" :route="playerRoute.name" @error="onRoutePlayerError" />' in recordings
  assert 'return this.selectedCamera === "forward" && this.wholeRoute' in recordings
  assert 'v-if="!usingRoutePlayer" class="gx-video-segment-controls"' in recordings
  assert "if (this.usingRoutePlayer || !this.segments[this.current]) return" in recordings

  # Playlist URL is built from the route name; hls.js comes from the local vendor copy, loaded only when needed.
  assert "`/route-playback/${encodeURIComponent(route)}/qcamera.m3u8`" in player
  assert 'const HLS_MODULE_URL = "/assets/vendor/hls.js/hls.light-1.7.3.min.js"' in player
  assert (vendor / "hls.light-1.7.3.min.js").is_file() and (vendor / "LICENSE").is_file()
  assert "import(HLS_MODULE_URL)" in player

  # hls.js is preferred wherever MSE exists; native HLS is only the fallback.
  assert player.index("window.MediaSource || window.ManagedMediaSource") < player.index("video.canPlayType(HLS_MIME)")

  # The hls.js instance is torn down on close/camera switch (unmount) and before re-attaching.
  assert "beforeUnmount() {\n    this.detach()" in player
  assert "this._hls?.destroy()" in player
  assert "async attach() {\n      this.detach()" in player


# --- Stage 2: full-quality fMP4 HLS over the raw .hevc cameras ---

FORWARD_URL = f"/route-playback/{ROUTE_NAME}/forward.m3u8"


def _box(kind, payload=b""):
  return struct.pack(">I4s", 8 + len(payload), kind) + payload


FAKE_INIT = _box(b"ftyp", b"isom") + _box(b"moov", b"trak")
FAKE_MEDIA = _box(b"moof", b"frag") + _box(b"mdat", b"x" * 32)


def _make_camera_segment(root, segment_num, camera_file="fcamera.hevc", locked=False, qcamera=False):
  segment = root / f"{ROUTE_NAME}--{segment_num}"
  segment.mkdir(parents=True, exist_ok=True)
  (segment / camera_file).write_bytes(b"raw hevc")
  if locked:
    (segment / f"{camera_file}.lock").touch()
  if qcamera:
    (segment / "qcamera.ts").write_bytes(b"ts")
  return segment


class FakeExecutor:
  """Runs jobs synchronously, or leaves them pending when `hold` is set."""
  def __init__(self, hold=False):
    self.hold = hold
    self.submitted = []

  def submit(self, fn, *args):
    future = Future()
    self.submitted.append((fn, args, future))
    if not self.hold:
      try:
        future.set_result(fn(*args))
      except Exception as error:
        future.set_exception(error)
    return future


def _fake_ffmpeg(monkeypatch, output=FAKE_INIT + FAKE_MEDIA, failure=None):
  calls = []
  def run(cmd, **kwargs):
    calls.append((cmd, kwargs))
    if failure is not None:
      raise failure
    with open(cmd[-1], "wb") as file:
      file.write(output)
    return SimpleNamespace(stdout="")
  monkeypatch.setattr(route_playback.subprocess, "run", run)
  return calls


def _full_client(footage, cache_root, executor):
  app = Flask(f"route_playback_full_{time.monotonic_ns()}")
  app.register_blueprint(route_playback.create_blueprint([str(footage) + "/"], remux_executor=executor, cache_root=cache_root))
  return app.test_client()


def test_camera_playlist_maps_an_init_part_per_segment(tmp_path, last_duration):
  footage = tmp_path / "footage"
  for num in (0, 1, 4):
    _make_camera_segment(footage, num, qcamera=True)

  response = _full_client(footage, tmp_path / "cache", FakeExecutor()).get(FORWARD_URL)

  assert response.status_code == 200
  assert response.mimetype == "application/vnd.apple.mpegurl"
  assert response.headers["Cache-Control"] == "no-store"
  uris, lines = _entries(response.get_data(as_text=True))
  assert lines[:2] == ["#EXTM3U", "#EXT-X-VERSION:7"]
  assert uris == [f"../segment/{ROUTE_NAME}--{num}/forward/media.m4s" for num in (0, 1, 4)]
  assert [line for line in lines if line.startswith("#EXT-X-MAP")] == [
    f'#EXT-X-MAP:URI="../segment/{ROUTE_NAME}--{num}/forward/init.mp4"' for num in (0, 1, 4)
  ]
  assert lines.count("#EXT-X-DISCONTINUITY") == 3
  assert [line for line in lines if line.startswith("#EXTINF")] == ["#EXTINF:60.000,"] * 2 + ["#EXTINF:23.500,"]
  assert lines[-1] == "#EXT-X-ENDLIST"
  # Listing a route never remuxes anything; the only subprocess is the last segment's qcamera ffprobe.
  [(cmd, _)] = last_duration["calls"]
  assert cmd[-1].endswith(f"{ROUTE_NAME}--4/qcamera.ts")


def test_camera_playlist_skips_locked_and_missing_files_and_uses_60s_without_qcamera(tmp_path, last_duration):
  footage = tmp_path / "footage"
  _make_camera_segment(footage, 0, camera_file="ecamera.hevc")
  _make_camera_segment(footage, 1, camera_file="fcamera.hevc")
  _make_camera_segment(footage, 2, camera_file="ecamera.hevc", locked=True)

  body = _full_client(footage, tmp_path / "cache", FakeExecutor()).get(f"/route-playback/{ROUTE_NAME}/wide.m3u8").get_data(as_text=True)

  uris, lines = _entries(body)
  assert uris == [f"../segment/{ROUTE_NAME}--0/wide/media.m4s"]
  assert "#EXTINF:60.000," in lines
  assert last_duration["calls"] == []


def test_camera_playlist_rejects_bad_cameras_and_keeps_the_qcamera_playlist(tmp_path, last_duration):
  footage = tmp_path / "footage"
  _make_camera_segment(footage, 0, qcamera=True)
  client = _full_client(footage, tmp_path / "cache", FakeExecutor())

  assert client.get(f"/route-playback/{ROUTE_NAME}/rear.m3u8").status_code == 400
  assert client.get(f"/route-playback/{ROUTE_NAME}/driver.m3u8").status_code == 404
  qcamera = client.get(PLAYLIST_URL).get_data(as_text=True)
  assert "#EXT-X-VERSION:3" in qcamera and "qcamera.ts" in qcamera and "EXT-X-MAP" not in qcamera


def test_media_parts_are_remuxed_once_split_and_cached(tmp_path, monkeypatch):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  _make_camera_segment(footage, 0, camera_file="dcamera.hevc")
  calls = _fake_ffmpeg(monkeypatch)
  executor = FakeExecutor()
  client = _full_client(footage, cache_root, executor)
  base = f"/route-playback/segment/{ROUTE_NAME}--0/driver"

  with client.get(f"{base}/init.mp4") as init:
    assert init.status_code == 200
    assert init.mimetype == "video/mp4"
    assert init.data == FAKE_INIT
  with client.get(f"{base}/media.m4s", headers={"Range": "bytes=0-7"}) as media:
    assert media.status_code == 206
    assert media.mimetype == "video/iso.segment"
    assert media.data == FAKE_MEDIA[:8]

  # One stream-copy run on the passed-in executor; the second part was a cache hit.
  assert len(calls) == 1 and len(executor.submitted) == 1
  cmd, kwargs = calls[0]
  assert cmd[0] == route_playback.utilities.FFMPEG_BIN
  assert cmd[cmd.index("-i") + 1].endswith(f"{ROUTE_NAME}--0/dcamera.hevc")
  assert cmd[cmd.index("-c") + 1] == "copy" and "libx264" not in cmd
  assert cmd[cmd.index("-tag:v") + 1] == "hvc1"
  assert cmd[cmd.index("-f") + 1] == "mp4"
  assert cmd[cmd.index("-movflags") + 1] == "frag_keyframe+empty_moov+default_base_moof"
  assert kwargs["timeout"] == route_playback.utilities.VIDEO_REMUX_TIMEOUT_SECONDS
  # Only the finished dir is left: no temp dir, no joined file.
  [entry] = list(cache_root.iterdir())
  assert sorted(path.name for path in entry.iterdir()) == ["init.mp4", "media.m4s"]


def test_concurrent_requests_share_one_remux_and_time_out_with_503(tmp_path, monkeypatch):
  footage = tmp_path / "footage"
  _make_camera_segment(footage, 0)
  _fake_ffmpeg(monkeypatch)
  monkeypatch.setattr(route_playback, "REMUX_WAIT_SECONDS", 0.01)
  executor = FakeExecutor(hold=True)
  client = _full_client(footage, tmp_path / "cache", executor)
  url = f"/route-playback/segment/{ROUTE_NAME}--0/forward/media.m4s"

  assert client.get(url).status_code == 503
  assert client.get(url).status_code == 503
  assert len(executor.submitted) == 1

  fn, args, future = executor.submitted[0]
  future.set_result(fn(*args))
  with client.get(url) as response:
    assert response.status_code == 200
    assert response.data == FAKE_MEDIA


def test_ffmpeg_failure_is_an_error_and_leaves_no_partial_cache(tmp_path, monkeypatch):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  _make_camera_segment(footage, 0)
  _fake_ffmpeg(monkeypatch, failure=subprocess.CalledProcessError(1, "ffmpeg"))
  client = _full_client(footage, cache_root, FakeExecutor())

  assert client.get(f"/route-playback/segment/{ROUTE_NAME}--0/forward/init.mp4").status_code == 409
  assert list(cache_root.iterdir()) == []


def test_output_without_moov_is_an_error(tmp_path, monkeypatch):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  _make_camera_segment(footage, 0)
  _fake_ffmpeg(monkeypatch, output=FAKE_MEDIA)
  client = _full_client(footage, cache_root, FakeExecutor())

  assert client.get(f"/route-playback/segment/{ROUTE_NAME}--0/forward/init.mp4").status_code == 409
  assert list(cache_root.iterdir()) == []


def test_media_parts_reject_bad_requests_locked_missing_and_no_executor(tmp_path, monkeypatch):
  footage = tmp_path / "footage"
  _make_camera_segment(footage, 0, locked=True)
  calls = _fake_ffmpeg(monkeypatch)
  client = _full_client(footage, tmp_path / "cache", FakeExecutor())
  base = f"/route-playback/segment/{ROUTE_NAME}"

  assert client.get(f"{base}/forward/init.mp4").status_code == 400
  assert client.get(f"{base}--0/rear/init.mp4").status_code == 400
  assert client.get(f"{base}--0/forward/joined.mp4").status_code == 400
  assert client.get(f"{base}--0/forward/init.mp4").status_code == 409
  assert client.get(f"{base}--0/wide/init.mp4").status_code == 404
  assert calls == []

  unwired = Flask(f"route_playback_unwired_{time.monotonic_ns()}")
  route_playback.register(unwired, footage_paths=[str(footage) + "/"])
  assert unwired.test_client().get(f"{base}--0/forward/init.mp4").status_code == 503


def _cache_entry(cache_root, name, size, mtime):
  entry = cache_root / name
  entry.mkdir(parents=True)
  (entry / "media.m4s").write_bytes(b"x" * size)
  os.utime(entry, (mtime, mtime))
  return entry


def test_prune_evicts_oldest_dirs_spares_the_new_one_and_clears_stale_temp(tmp_path, monkeypatch):
  monkeypatch.setattr(route_playback, "HLS_CACHE_MAX_BYTES", 250)
  cache_root = tmp_path / "cache"
  oldest = _cache_entry(cache_root, "a", 100, 1000)
  middle = _cache_entry(cache_root, "b", 100, 2000)
  newest = _cache_entry(cache_root, "c", 100, 3000)
  keep = _cache_entry(cache_root, "keep", 100, 500)
  stale = cache_root / ".tmp-crashed"
  stale.mkdir()

  route_playback._prune_cache(cache_root, keep_path=keep)

  assert not oldest.exists() and not stale.exists()
  assert middle.exists() and newest.exists() and keep.exists()


def test_cache_hit_touches_the_dir_for_the_prune_order(tmp_path, monkeypatch):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  _make_camera_segment(footage, 0)
  _fake_ffmpeg(monkeypatch)
  client = _full_client(footage, cache_root, FakeExecutor())
  url = f"/route-playback/segment/{ROUTE_NAME}--0/forward/init.mp4"
  client.get(url).close()
  [entry] = list(cache_root.iterdir())
  os.utime(entry, (1000, 1000))

  client.get(url).close()

  assert entry.stat().st_mtime > 1000


def test_upstream_video_cache_prune_leaves_route_hls_alone(tmp_path, monkeypatch):
  utilities = route_playback.utilities
  monkeypatch.setattr(utilities, "VIDEO_CACHE_PATH", tmp_path)
  monkeypatch.setattr(utilities, "VIDEO_CACHE_MAX_BYTES", 0)
  (tmp_path / "upstream.mp4").write_bytes(b"x" * 10)
  ours = tmp_path / "route-hls" / "abc"
  ours.mkdir(parents=True)
  (ours / "init.mp4").write_bytes(b"x" * 10)

  utilities._prune_video_cache()

  assert not (tmp_path / "upstream.mp4").exists()
  assert (ours / "init.mp4").exists()


def test_galaxy_setup_shares_the_upstream_remux_executor(monkeypatch, tmp_path):
  import importlib.util
  import sys

  spec = importlib.util.spec_from_file_location("route_playback_server_executor", MODULE_DIR / "the_galaxy.py")
  the_galaxy = importlib.util.module_from_spec(spec)
  sys.modules["route_playback_server_executor"] = the_galaxy
  spec.loader.exec_module(the_galaxy)
  assert the_galaxy._import_galaxy_web_symbols()
  monkeypatch.setattr(the_galaxy, "FOOTAGE_PATHS", [str(tmp_path) + "/"])
  monkeypatch.setattr(the_galaxy, "params", FakeParams())
  captured = {}
  monkeypatch.setattr(route_playback, "register", lambda app, **kwargs: captured.update(kwargs))

  app = the_galaxy.Flask(
    f"route_playback_executor_{time.monotonic_ns()}",
    template_folder=str(MODULE_DIR / "templates"),
    static_folder=str(MODULE_DIR / "assets"),
  )
  the_galaxy.setup(app)

  assert captured["remux_executor"] is the_galaxy._VIDEO_REMUX_EXECUTOR
  assert captured["footage_paths"] == [str(tmp_path) + "/"]


def _ffmpeg_with_libx265():
  ffmpeg = shutil.which("ffmpeg")
  if not ffmpeg or not shutil.which("ffprobe"):
    return None
  encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
  return ffmpeg if "libx265" in encoders else None


@pytest.mark.skipif(_ffmpeg_with_libx265() is None, reason="needs ffmpeg/ffprobe with libx265")
def test_real_ffmpeg_remux_of_raw_hevc_gives_a_playable_fmp4_pair(tmp_path, monkeypatch):
  monkeypatch.setattr(route_playback.utilities, "FFMPEG_BIN", shutil.which("ffmpeg"))
  source = tmp_path / "fcamera.hevc"
  subprocess.run([
    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=128x96:rate=20", "-t", "2",
    "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error:keyint=20", "-f", "hevc", str(source),
  ], check=True, timeout=60)
  cache_root = tmp_path / "cache"

  target = route_playback._remux_to_fmp4(str(source), cache_root / "seg", cache_root)

  joined = tmp_path / "joined.mp4"
  joined.write_bytes((target / "init.mp4").read_bytes() + (target / "media.m4s").read_bytes())
  probe = subprocess.run([
    "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_tag_string,avg_frame_rate",
    "-of", "default=noprint_wrappers=1", str(joined),
  ], capture_output=True, text=True, check=True, timeout=30).stdout
  assert "codec_tag_string=hvc1" in probe
  assert "avg_frame_rate=20/1" in probe
  assert abs(float(probe.split("duration=")[1].split()[0]) - 2.0) < 0.1
  assert (target / "media.m4s").read_bytes()[4:8] == b"moof"
