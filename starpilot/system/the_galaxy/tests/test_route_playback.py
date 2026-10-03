import hashlib
import json
import os
import shutil
import struct
import subprocess
import time
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from test_dashboard_stats import FakeParams, MODULE_DIR, _install_server_import_stubs

_install_server_import_stubs()

from openpilot.starpilot.system.the_galaxy import route_playback, route_timeline


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

  # Every camera uses the whole-route player; the road camera toggles low/full quality; segments are the error fallback.
  assert 'import { RoutePlayer } from "../components/RoutePlayer.js"' in recordings
  assert ('<RoutePlayer v-if="usingRoutePlayer" :route="playerRoute.name" :camera="selectedCamera" :quality="quality" '
          '@error="onRoutePlayerError" @fallback-low="onRoutePlayerFallbackLow" />') in recordings
  assert "usingRoutePlayer() {\n      return this.wholeRoute\n    }" in recordings
  # The quality options sit right after the forward chip, both shown, the active one marked.
  assert ('<span v-if="c===\'forward\' && usingRoutePlayer && selectedCamera===\'forward\'" class="gx-video-quality" '
          'role="group"') in recordings
  assert ':aria-pressed="String(quality===q)"' in recordings and '@click="quality = q"' in recordings
  assert 'v-if="!usingRoutePlayer" type="button" class="gx-chip gx-video-whole-route"' in recordings
  assert 'v-if="!usingRoutePlayer" class="gx-video-segment-controls"' in recordings
  assert "if (this.usingRoutePlayer || !this.segments[this.current]) return" in recordings
  assert 'this.wholeRoute = true\n        this.quality = "low"' in recordings
  assert 'playWholeRoute() {\n      // Back from the single-segment fallback: restart at the quality that plays everywhere.\n      this.quality = "low"' in recordings

  # Playlist URL: qcamera for the road camera on low, else the camera's fMP4 playlist.
  assert 'const playlist = camera === "forward" && quality === "low" ? "qcamera" : camera' in player
  assert "`/route-playback/${encodeURIComponent(route)}/${encodeURIComponent(playlist)}.m3u8`" in player
  assert 'const HLS_MODULE_URL = "/assets/vendor/hls.js/hls.light-1.7.3.min.js"' in player
  assert (vendor / "hls.light-1.7.3.min.js").is_file() and (vendor / "LICENSE").is_file()
  assert "import(HLS_MODULE_URL)" in player

  # Full quality needs HEVC decode: checked before loading; road camera falls back to low, others error.
  assert "HEVC_MIME = 'video/mp4; codecs=\"hvc1.1.6.L150.B0\"'" in player
  hevc_check = player.index("if (full && !mseHevc")
  assert hevc_check < player.index("await loadHls()")
  assert player.index('this.$emit("fallback-low")') > hevc_check

  # hls.js is preferred wherever MSE exists (and can decode the stream); native HLS is only the fallback.
  assert "if (mediaSource && (!full || mseHevc)) {" in player
  assert player.index("await loadHls()") < player.index("if (!video.canPlayType(HLS_MIME)) {")

  # Camera/quality switches keep the position; the resume handler is tied to its attach.
  assert "camera() {\n      this.attach(true)" in player and "quality() {\n      this.attach(true)" in player
  assert "const resumeAt = keepTime ? this._resumeAt || this.$refs.video?.currentTime || 0 : 0" in player
  assert player.index("this._resumeAt = resumeAt") < player.index('this.$emit("fallback-low")')
  assert "this._resumeAt = resumeAt\n" in player  # stays pending until the new source loads
  assert "if (this._native) video.currentTime = resumeAt\n          this._resumeAt = 0" in player
  assert "new Hls({ startPosition: resumeAt > 0 ? resumeAt : -1 })" in player

  # A road-camera full-quality decode failure during playback drops to low quality, not to segments.
  assert 'if (data.type === Hls.ErrorTypes.MEDIA_ERROR && this.fallBackToLow()) return' in player
  assert "if (this._native && !this.fallBackToLow())" in player
  assert 'if (this.camera !== "forward" || !this.fullQuality) return false' in player

  # The hls.js instance is torn down on close/camera switch (unmount) and before re-attaching.
  assert "beforeUnmount() {\n    this.detach()" in player
  assert "this._hls?.destroy()" in player
  assert player.index("const resumeAt") < player.index("this.detach()\n      const video")


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


def test_a_cache_dir_missing_a_part_is_remuxed_again(tmp_path, monkeypatch):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  _make_camera_segment(footage, 0)
  calls = _fake_ffmpeg(monkeypatch)
  client = _full_client(footage, cache_root, FakeExecutor())
  url = f"/route-playback/segment/{ROUTE_NAME}--0/forward/media.m4s"
  client.get(url).close()
  [entry] = list(cache_root.iterdir())
  (entry / "media.m4s").unlink()

  with client.get(url) as response:
    assert response.status_code == 200
    assert response.data == FAKE_MEDIA
  assert len(calls) == 2


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


def test_backlog_on_the_shared_executor_is_capped(tmp_path, monkeypatch):
  footage = tmp_path / "footage"
  for num in (0, 1, 2):
    _make_camera_segment(footage, num)
  _fake_ffmpeg(monkeypatch)
  monkeypatch.setattr(route_playback, "REMUX_WAIT_SECONDS", 0.01)
  executor = FakeExecutor(hold=True)
  client = _full_client(footage, tmp_path / "cache", executor)
  url = f"/route-playback/segment/{ROUTE_NAME}--{{}}/forward/media.m4s"

  # A scrub over three minutes: two jobs queue, the third is turned away without submitting.
  assert [client.get(url.format(num)).status_code for num in (0, 1, 2)] == [503] * 3
  assert len(executor.submitted) == route_playback.MAX_QUEUED_REMUXES == 2

  fn, args, future = executor.submitted[0]
  future.set_result(fn(*args))
  assert client.get(url.format(2)).status_code == 503
  assert len(executor.submitted) == 3


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


# Stage 3: route timeline.


def _state(enabled, active, alert="normal"):
  return SimpleNamespace(enabled=enabled, active=active, alertStatus=alert)


def _events(start_ns, *timed):
  # initData carries the route start; the sentinel after it is the segment's t=0.
  return [(1, "initData", None), (start_ns, "sentinel", None), *[(start_ns + int(t * 1e9), w, m) for t, w, m in timed]]


def test_summarize_starts_at_the_sentinel_and_splits_spans_on_state_and_alert_changes():
  timeline, thumbnail, late = route_timeline.summarize(_events(
    2433_000_000_000,
    (-1.5, "selfdriveState", _state(False, False)),
    (0.5, "selfdriveState", _state(False, False)),
    (4.8, "thumbnail", SimpleNamespace(thumbnail=b"\xff\xd8first")),
    (10.0, "selfdriveState", _state(True, True)),
    (20.0, "selfdriveState", _state(True, True)),
    (30.0, "selfdriveState", _state(True, False)),
    (40.0, "selfdriveState", _state(True, True, "userPrompt")),
    (45.0, "selfdriveState", _state(True, True, "critical")),
    (50.0, "selfdriveState", _state(True, True)),
    (59.9, "selfdriveState", _state(True, True)),
    (59.95, "thumbnail", SimpleNamespace(thumbnail=b"\xff\xd8second")),
  ))

  assert timeline == {"thumbnailAt": 4.8, "spans": [
    [0.0, 10.0, "disengaged", 0],
    [10.0, 30.0, "engaged", 0],
    [30.0, 40.0, "overriding", 0],
    [40.0, 45.0, "engaged", 1],
    [45.0, 50.0, "engaged", 2],
    [50.0, 59.9, "engaged", 0],
  ]}
  assert thumbnail == b"\xff\xd8first"
  # The next minute's, written just before the rotation.
  assert late == (59.95, b"\xff\xd8second")


@pytest.mark.parametrize("events", [[], [(1, "initData", None)], _events(5, (1.0, "carState", None))])
def test_summarize_without_state_or_thumbnail_is_empty(events):
  assert route_timeline.summarize(events) == ({"spans": [], "thumbnailAt": None}, None, None)


@pytest.mark.parametrize(("seconds", "own", "late"), [(0.3, b"t", None), (29.9, b"t", None), (30.0, None, (30.0, b"t")), (59.97, None, (59.97, b"t"))])
def test_summarize_takes_a_thumbnail_30_s_or_more_in_as_the_next_minutes(seconds, own, late):
  timeline, thumbnail, late_thumbnail = route_timeline.summarize(_events(5, (seconds, "thumbnail", SimpleNamespace(thumbnail=b"t"))))
  assert (thumbnail, late_thumbnail) == (own, late)
  assert timeline["thumbnailAt"] == (seconds if own else None)


def _make_log_segment(root, segment_num, qlog="qlog.zst", locked=False):
  segment = root / f"{ROUTE_NAME}--{segment_num}"
  segment.mkdir(parents=True, exist_ok=True)
  (segment / qlog).write_bytes(b"qlog")
  if locked:
    (segment / "rlog.lock").touch()
  return segment


def _thumbnail_events(*thumbnails):
  return _events(5, *[(seconds, "thumbnail", SimpleNamespace(thumbnail=data)) for seconds, data in thumbnails])


def _parse_with(tmp_path, monkeypatch, qlogs, segment_num):
  """route_timeline.parse of segment_num with fake qlogs {segment num: events, or an exception to raise}."""
  def read_events(qlog_path):
    events = qlogs[int(Path(qlog_path).parent.name.rsplit("--", 1)[1])]
    if isinstance(events, Exception):
      raise events
    return events
  reads = []
  monkeypatch.setattr(route_timeline, "read_events", lambda qlog_path: reads.append(Path(qlog_path).parent.name) or read_events(qlog_path))
  for num in qlogs:
    _make_log_segment(tmp_path / "footage", num)
  out_dir = tmp_path / "out"
  out_dir.mkdir()
  route_timeline.parse(str(tmp_path / "footage" / f"{ROUTE_NAME}--{segment_num}" / "qlog.zst"), str(out_dir))
  thumbnail = out_dir / "thumbnail.jpg"
  timeline = json.loads((out_dir / "timeline.json").read_text())
  return timeline["thumbnailAt"], thumbnail.read_bytes() if thumbnail.exists() else None, reads


def test_a_minute_without_its_thumbnail_takes_it_from_the_end_of_the_previous_qlog(tmp_path, monkeypatch):
  qlogs = {11: _thumbnail_events((0.3, b"eleven"), (59.97, b"twelve")), 12: _thumbnail_events()}
  assert _parse_with(tmp_path, monkeypatch, qlogs, 12) == (0.0, b"twelve", [f"{ROUTE_NAME}--12", f"{ROUTE_NAME}--11"])


def test_a_minute_with_its_own_thumbnail_keeps_it_and_reads_no_other_qlog(tmp_path, monkeypatch):
  qlogs = {12: _thumbnail_events((4.8, b"own")), 13: _thumbnail_events((4.8, b"thirteen"), (59.9, b"late"))}
  assert _parse_with(tmp_path, monkeypatch, qlogs, 13) == (4.8, b"thirteen", [f"{ROUTE_NAME}--13"])


@pytest.mark.parametrize("qlogs", [
  {0: _thumbnail_events()},  # minute 0: no previous qlog to look in
  {4: _thumbnail_events()},  # the previous segment is gone
  {3: ValueError("corrupt"), 4: _thumbnail_events()},
  {3: _thumbnail_events((0.3, b"own only")), 4: _thumbnail_events()},
])
def test_a_minute_without_a_thumbnail_anywhere_has_none(tmp_path, monkeypatch, qlogs):
  assert _parse_with(tmp_path, monkeypatch, qlogs, max(qlogs))[:2] == (None, None)


@pytest.mark.parametrize("qlogs", [
  {0: _thumbnail_events((59.01, b"late"))},  # a route's first minute: its only thumbnail is the next minute's
  {3: _thumbnail_events((0.3, b"own only")), 4: _thumbnail_events((59.01, b"late"))},
])
def test_a_minute_with_none_of_its_own_anywhere_falls_back_to_the_next_minutes_from_its_end(tmp_path, monkeypatch, qlogs):
  assert _parse_with(tmp_path, monkeypatch, qlogs, max(qlogs))[:2] == (59.01, b"late")


def test_the_timeline_cache_key_carries_the_parser_version_and_the_video_key_does_not(tmp_path):
  source = tmp_path / "qlog.zst"
  source.write_bytes(b"qlog")
  stat = source.stat()
  old_key = hashlib.md5(f"{source}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()
  assert route_playback._cache_dir_for(tmp_path, source).name == old_key
  assert route_playback._timeline_dir_for(tmp_path, source).name not in (old_key, "")
  assert route_timeline.CACHE_VERSION == 2


def _fake_parser(monkeypatch, thumbnail=b"\xff\xd8jpeg", failure=None, write=True, then_fail=None):
  """write: True, False, or the indices of the (qlog, out dir) pairs that get a timeline. then_fail: raised after writing."""
  calls = []
  def run(cmd, **kwargs):
    calls.append((cmd, kwargs))
    if failure is not None:
      raise failure
    for index, out_dir in enumerate(cmd[7::2]):
      if write is True or (write and index in write):
        if thumbnail is not None:
          with open(os.path.join(out_dir, "thumbnail.jpg"), "wb") as file:
            file.write(thumbnail)
        with open(os.path.join(out_dir, "timeline.json"), "w") as file:
          file.write('{"spans":[[0,60,"engaged",0]],"thumbnailAt":5}')
      elif thumbnail is not None:
        # A pair cut off mid-way: thumbnail written, timeline not.
        with open(os.path.join(out_dir, "thumbnail.jpg"), "wb") as file:
          file.write(thumbnail)
    if then_fail is not None:
      raise then_fail
    return SimpleNamespace(stdout="")
  monkeypatch.setattr(route_playback.subprocess, "run", run)
  return calls


@pytest.fixture
def offroad(monkeypatch):
  params = FakeParams({"IsOnroad": False})
  monkeypatch.setattr(route_playback.utilities, "params", params)
  return params


def _timeline_client(footage, cache_root, executor):
  app = Flask(f"route_timeline_{time.monotonic_ns()}")
  app.register_blueprint(route_playback.create_blueprint(
    [str(footage) + "/"], parse_executor=executor, timeline_cache_root=cache_root))
  return app.test_client()


def _segment_url(num, part="timeline.json"):
  return f"/route-playback/segment/{ROUTE_NAME}--{num}/{part}"


def _get(client, url):
  # Read and close, so send_file's handle doesn't leak.
  with client.get(url) as response:
    response.get_data()
  return response


@pytest.mark.parametrize(("query", "files"), [
  ("", ("qcamera.ts",)),
  ("?camera=forward&quality=full", ("fcamera.hevc", "qcamera.ts")),
  ("?camera=wide&quality=low", ("ecamera.hevc", "qcamera.ts")),
])
def test_route_timeline_offsets_match_the_playlist(tmp_path, last_duration, monkeypatch, query, files):
  footage = tmp_path / "footage"
  for num in (0, 1, 3):
    segment = footage / f"{ROUTE_NAME}--{num}"
    segment.mkdir(parents=True)
    for name in files:
      (segment / name).write_bytes(b"x")
  playlist = {"": "qcamera", "?camera=forward&quality=full": "forward", "?camera=wide&quality=low": "wide"}[query]
  monkeypatch.setattr(route_playback.utilities, "get_route_start_time_for_route", lambda *args: None)
  client = _full_client(footage, tmp_path / "cache", FakeExecutor())

  response = client.get(f"/route-playback/{ROUTE_NAME}/timeline.json{query}")

  assert response.status_code == 200
  extinf = [float(line[8:-1]) for line in client.get(f"/route-playback/{ROUTE_NAME}/{playlist}.m3u8").get_data(as_text=True).splitlines()
            if line.startswith("#EXTINF:")]
  assert response.get_json() == {"segments": [
    {"segment": f"{ROUTE_NAME}--0", "start": 0.0, "duration": 60.0},
    {"segment": f"{ROUTE_NAME}--1", "start": 60.0, "duration": 60.0},
    {"segment": f"{ROUTE_NAME}--3", "start": 120.0, "duration": 23.5},
  ], "startedAt": None}
  assert extinf == [60.0, 60.0, 23.5]


@pytest.mark.parametrize(("started", "expected"), [
  (datetime(2026, 10, 1, 14, 8, 36, 500000), datetime(2026, 10, 1, 14, 8, 36, 500000).timestamp()),
  (None, None),
])
def test_route_timeline_carries_the_route_start_as_epoch_seconds(tmp_path, last_duration, monkeypatch, started, expected):
  for num in (1, 4):
    _make_segment(tmp_path, num)
  calls = []
  monkeypatch.setattr(route_playback.utilities, "get_route_start_time_for_route", lambda *args: calls.append(args) or started)

  body = _client(tmp_path).get(f"/route-playback/{ROUTE_NAME}/timeline.json").get_json()

  assert body["startedAt"] == expected
  assert calls == [(ROUTE_NAME, str(tmp_path), [1, 4])]


def test_route_timeline_rejects_bad_requests(tmp_path, last_duration):
  _make_segment(tmp_path, 0)
  client = _client(tmp_path)

  assert client.get("/route-playback/nope/timeline.json").status_code == 400
  assert client.get(f"/route-playback/{ROUTE_NAME}/timeline.json?camera=rear").status_code == 400
  assert client.get(f"/route-playback/{ROUTE_NAME}/timeline.json?quality=best").status_code == 400
  assert client.get(f"/route-playback/{ROUTE_NAME}/timeline.json?camera=wide").status_code == 404
  assert client.get("/route-playback/0000006a--0000000000/timeline.json").status_code == 404


def test_segment_timeline_is_parsed_once_niced_then_cached(tmp_path, monkeypatch, offroad):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  _make_log_segment(footage, 0)
  calls = _fake_parser(monkeypatch)
  client = _timeline_client(footage, cache_root, FakeExecutor())

  response = _get(client, _segment_url(0))
  assert response.status_code == 200
  assert response.get_json() == {"spans": [[0, 60, "engaged", 0]], "thumbnailAt": 5}
  with _get(client, _segment_url(0, "thumbnail.jpg")) as thumbnail:
    assert thumbnail.status_code == 200
    assert thumbnail.mimetype == "image/jpeg"
    assert thumbnail.data == b"\xff\xd8jpeg"
    assert thumbnail.cache_control.max_age == 86400

  assert len(calls) == 1
  cmd, kwargs = calls[0]
  assert cmd[:4] == ["nice", "-n", "19", route_playback.sys.executable]
  assert cmd[4:7] == ["-m", "openpilot.starpilot.system.the_galaxy.route_timeline", str(footage / f"{ROUTE_NAME}--0" / "qlog.zst")]
  assert kwargs["timeout"] == route_playback.TIMELINE_PARSE_TIMEOUT_SECONDS
  # The exit status isn't checked: each pair's timeline.json is.
  assert "check" not in kwargs
  assert str(route_playback.REPO_ROOT) in kwargs["env"]["PYTHONPATH"]
  [entry] = list(cache_root.iterdir())
  assert sorted(path.name for path in entry.iterdir()) == ["thumbnail.jpg", "timeline.json"]


def test_segment_timeline_rejects_bad_names_missing_logs_and_recording_segments(tmp_path, monkeypatch, offroad):
  footage = tmp_path / "footage"
  _make_log_segment(footage, 0, locked=True)
  _make_log_segment(footage, 1, qlog="qlog.bz2")
  (footage / f"{ROUTE_NAME}--2").mkdir()
  calls = _fake_parser(monkeypatch)
  client = _timeline_client(footage, tmp_path / "cache", FakeExecutor())

  assert client.get(f"/route-playback/segment/{ROUTE_NAME}/timeline.json").status_code == 400
  assert _get(client, _segment_url(0, "rlog.zst")).status_code == 404
  assert _get(client, _segment_url(0)).status_code == 409
  assert _get(client, _segment_url(2)).status_code == 404
  assert calls == []
  assert _get(client, _segment_url(1)).status_code == 200
  assert calls[0][0][6].endswith("qlog.bz2")


def test_onroad_serves_only_the_cache(tmp_path, monkeypatch, offroad):
  footage = tmp_path / "footage"
  _make_log_segment(footage, 0)
  calls = _fake_parser(monkeypatch)
  client = _timeline_client(footage, tmp_path / "cache", FakeExecutor())
  assert _get(client, _segment_url(0)).status_code == 200
  _make_log_segment(footage, 1)

  offroad.put("IsOnroad", True)

  assert _get(client, _segment_url(0)).status_code == 200
  response = _get(client, _segment_url(1))
  assert response.status_code == 503
  assert response.get_json()["reason"] == "onroad"
  assert len(calls) == 1


def test_a_parse_queued_offroad_does_not_run_once_onroad(tmp_path, monkeypatch, offroad):
  footage = tmp_path / "footage"
  _make_log_segment(footage, 0)
  calls = _fake_parser(monkeypatch)
  monkeypatch.setattr(route_playback, "TIMELINE_WAIT_SECONDS", 0.01)
  executor = FakeExecutor(hold=True)
  client = _timeline_client(footage, tmp_path / "cache", executor)
  assert _get(client, _segment_url(0)).status_code == 503

  offroad.put("IsOnroad", True)
  fn, args, future = executor.submitted[0]
  with pytest.raises(route_playback.Onroad):
    fn(*args)
  assert calls == []


def test_concurrent_requests_share_one_parse_and_the_backlog_is_capped(tmp_path, monkeypatch, offroad):
  footage = tmp_path / "footage"
  for num in range(6):
    _make_log_segment(footage, num)
  _fake_parser(monkeypatch)
  monkeypatch.setattr(route_playback, "TIMELINE_WAIT_SECONDS", 0.01)
  executor = FakeExecutor(hold=True)
  client = _timeline_client(footage, tmp_path / "cache", executor)

  assert _get(client, _segment_url(0)).status_code == 503
  assert _get(client, _segment_url(0)).status_code == 503
  assert len(executor.submitted) == 1
  assert [_get(client, _segment_url(num)).status_code for num in range(1, 6)] == [503] * 5
  assert len(executor.submitted) == route_playback.MAX_QUEUED_PARSES == 4

  fn, args, future = executor.submitted[0]
  future.set_result(fn(*args))
  assert _get(client, _segment_url(0)).status_code == 200


@pytest.mark.parametrize("failure", [subprocess.CalledProcessError(1, "python"), subprocess.TimeoutExpired("python", 30), None])
def test_a_failed_parse_is_an_error_and_leaves_no_partial_cache(tmp_path, monkeypatch, offroad, failure):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  _make_log_segment(footage, 0)
  _fake_parser(monkeypatch, failure=failure, write=False)
  client = _timeline_client(footage, cache_root, FakeExecutor())

  assert _get(client, _segment_url(0)).status_code == 409
  assert list(cache_root.iterdir()) == []


def test_a_segment_without_a_thumbnail_answers_404_for_it(tmp_path, monkeypatch, offroad):
  footage = tmp_path / "footage"
  _make_log_segment(footage, 0)
  _fake_parser(monkeypatch, thumbnail=None)
  client = _timeline_client(footage, tmp_path / "cache", FakeExecutor())

  assert _get(client, _segment_url(0, "thumbnail.jpg")).status_code == 404
  assert _get(client, _segment_url(0)).status_code == 200


def test_timeline_cache_is_pruned_to_its_own_budget(tmp_path, monkeypatch, offroad):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  _make_log_segment(footage, 0)
  _fake_parser(monkeypatch)
  monkeypatch.setattr(route_playback, "TIMELINE_CACHE_MAX_BYTES", 150)
  old = _cache_entry(cache_root, "old", 100, 1000)
  recent = _cache_entry(cache_root, "recent", 100, 2000)

  assert _get(_timeline_client(footage, cache_root, FakeExecutor()), _segment_url(0)).status_code == 200

  assert not old.exists() and recent.exists()
  assert len(list(cache_root.iterdir())) == 2


def _cache_timeline(cache_root, segment, body='{"spans":[],"thumbnailAt":null}'):
  target_dir = route_playback._timeline_dir_for(cache_root, segment / "qlog.zst")
  target_dir.mkdir(parents=True)
  (target_dir / "timeline.json").write_text(body)
  return target_dir


def _batch_segments(calls):
  return [os.path.basename(os.path.dirname(qlog)) for qlog in calls[0][0][6::2]]


def test_neighbours_go_outward_earlier_first_like_the_client():
  assert route_playback._neighbours(["a", "b", "c", "d", "e"], "b") == ["a", "c", "d", "e"]
  assert route_playback._neighbours(["a", "b", "c"], "c") == ["b", "a"]
  assert route_playback._neighbours(["a"], "a") == []


def test_a_parse_batches_the_nearest_uncached_finished_segments_of_the_route(tmp_path, monkeypatch, offroad):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  segments = {num: _make_log_segment(footage, num) for num in (0, 1, 2, 3, 4, 7, 8)}
  _cache_timeline(cache_root, segments[3])
  _make_log_segment(footage, 5, locked=True)
  (footage / f"{ROUTE_NAME}--6").mkdir()  # no qlog
  other_route = footage / "0000006b--0123456789--4"
  other_route.mkdir()
  (other_route / "qlog.zst").write_bytes(b"qlog")
  calls = _fake_parser(monkeypatch)
  client = _timeline_client(footage, cache_root, FakeExecutor())

  assert _get(client, _segment_url(4)).status_code == 200

  # Order 3 (cached), 5 (recording), 2, 6 (no qlog), 1, 7, 0, 8 -> capped at 5 pairs in one subprocess.
  assert len(calls) == 1
  assert _batch_segments(calls) == [f"{ROUTE_NAME}--{num}" for num in (4, 2, 1, 7, 0)]
  assert len(calls[0][0][6:]) == 2 * route_playback.TIMELINE_BATCH_SIZE
  assert all(_get(client, _segment_url(num)).status_code == 200 for num in (0, 1, 2, 7))
  assert len(calls) == 1
  assert _get(client, _segment_url(8)).status_code == 200
  assert _batch_segments(calls[1:]) == [f"{ROUTE_NAME}--8"]


def test_a_queued_batch_skips_neighbours_parsed_meanwhile(tmp_path, monkeypatch, offroad):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  segments = {num: _make_log_segment(footage, num) for num in range(3)}
  calls = _fake_parser(monkeypatch)
  monkeypatch.setattr(route_playback, "TIMELINE_WAIT_SECONDS", 0.01)
  executor = FakeExecutor(hold=True)
  assert _get(_timeline_client(footage, cache_root, executor), _segment_url(1)).status_code == 503

  _cache_timeline(cache_root, segments[2])
  fn, args, future = executor.submitted[0]
  fn(*args)

  assert _batch_segments(calls) == [f"{ROUTE_NAME}--1", f"{ROUTE_NAME}--0"]


@pytest.mark.parametrize("then_fail", [None, subprocess.TimeoutExpired("python", 10)])
def test_a_batch_keeps_the_pairs_that_finished(tmp_path, monkeypatch, offroad, then_fail):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  segments = {num: _make_log_segment(footage, num) for num in range(3)}
  _fake_parser(monkeypatch, write={0, 2}, then_fail=then_fail)
  client = _timeline_client(footage, cache_root, FakeExecutor())

  # Batch 1, 0, 2: the requested one and segment 2 finished, segment 0 did not.
  assert _get(client, _segment_url(1)).status_code == 200

  kept = {path.name for path in cache_root.iterdir()}
  assert kept == {route_playback._timeline_dir_for(cache_root, segments[num] / "qlog.zst").name for num in (1, 2)}


def test_a_neighbour_that_cannot_be_renamed_does_not_fail_the_request(tmp_path, monkeypatch, offroad):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  segments = {num: _make_log_segment(footage, num) for num in range(3)}
  _fake_parser(monkeypatch)
  blocked = route_playback._timeline_dir_for(cache_root, segments[0] / "qlog.zst")
  real_rename = route_playback.os.rename
  def rename(source, target):
    if Path(target) == blocked:
      raise OSError("rename failed")
    real_rename(source, target)
  monkeypatch.setattr(route_playback.os, "rename", rename)
  client = _timeline_client(footage, cache_root, FakeExecutor())

  # Batch 1, 0, 2: segment 0's rename fails; 1 is served and 2 is still kept.
  assert _get(client, _segment_url(1)).status_code == 200

  kept = {path.name for path in cache_root.iterdir()}
  assert kept == {route_playback._timeline_dir_for(cache_root, segments[num] / "qlog.zst").name for num in (1, 2)}


def test_a_temp_dir_failure_partway_leaves_no_temp_dirs(tmp_path, monkeypatch, offroad):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  for num in range(3):
    _make_log_segment(footage, num)
  calls = _fake_parser(monkeypatch)
  real_mkdtemp = route_playback.tempfile.mkdtemp
  made = []
  def mkdtemp(**kwargs):
    if len(made) == 2:
      raise OSError("no space")
    made.append(real_mkdtemp(**kwargs))
    return made[-1]
  monkeypatch.setattr(route_playback.tempfile, "mkdtemp", mkdtemp)
  client = _timeline_client(footage, cache_root, FakeExecutor())

  assert _get(client, _segment_url(1)).status_code == 409

  assert calls == [] and len(made) == 2
  assert list(cache_root.iterdir()) == []


def test_a_failed_requested_pair_is_an_error_and_leaves_no_cache_for_it(tmp_path, monkeypatch, offroad):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  segments = {num: _make_log_segment(footage, num) for num in range(2)}
  _fake_parser(monkeypatch, write={1})
  client = _timeline_client(footage, cache_root, FakeExecutor())

  assert _get(client, _segment_url(0)).status_code == 409

  assert [path.name for path in cache_root.iterdir()] == [route_playback._timeline_dir_for(cache_root, segments[1] / "qlog.zst").name]


def test_route_timeline_carries_the_cached_minutes_only(tmp_path, last_duration, monkeypatch, offroad):
  footage, cache_root = tmp_path / "footage", tmp_path / "cache"
  for num in range(4):
    segment = _make_segment(footage, num)
    (segment / "qlog.zst").write_bytes(b"qlog")
  segments = {num: footage / f"{ROUTE_NAME}--{num}" for num in range(4)}
  cached = _cache_timeline(cache_root, segments[0], '{"spans":[[0,60,"engaged",0]],"thumbnailAt":5}')
  os.utime(cached, (1000, 1000))
  _cache_timeline(cache_root, segments[1], "{not json")
  _cache_timeline(cache_root, segments[2])
  (segments[2] / "rlog.lock").touch()
  app = Flask(f"route_timeline_{time.monotonic_ns()}")
  app.register_blueprint(route_playback.create_blueprint(
    [str(footage) + "/"], parse_executor=FakeExecutor(), timeline_cache_root=cache_root))
  client = app.test_client()
  offroad.put("IsOnroad", True)
  executor_calls = len(last_duration["calls"])

  body = client.get(f"/route-playback/{ROUTE_NAME}/timeline.json").get_json()

  # Cached: carried and touched. Unreadable, recording, uncached: no key (the client fetches those). Never parses.
  assert [entry.get("timeline") for entry in body["segments"]] == [
    {"spans": [[0, 60, "engaged", 0]], "thumbnailAt": 5}, None, None, None]
  assert ["timeline" in entry for entry in body["segments"]] == [True, False, False, False]
  assert cached.stat().st_mtime > 1000
  assert len(last_duration["calls"]) == executor_calls + 1  # the last segment's ffprobe only


SYNTHETIC_QLOG = """
import sys
import zstandard
from cereal import log

def event(seconds, which):
  message = log.Event.new_message()
  message.logMonoTime = int(seconds * 1e9)
  return message, message.init(which)

messages = [event(32.0, "initData")[0], event(2400.0, "sentinel")[0]]
for tenth in range(600):
  message, state = event(2400.0 + tenth / 10, "selfdriveState")
  state.enabled = tenth >= 100
  state.active = tenth >= 100 and not 300 <= tenth < 350
  state.alertStatus = "critical" if 500 <= tenth < 520 else "normal"
  messages.append(message)
  # Thumbnails at the given tenths of a second, each tagged with its tenth.
  if str(tenth) in sys.argv[3].split(","):
    message, thumbnail = event(2400.0 + tenth / 10, "thumbnail")
    thumbnail.frameId = 48000 + tenth
    thumbnail.thumbnail = b"\\xff\\xd8synthetic" + str(tenth).encode()
    messages.append(message)
raw = b"".join(m.to_bytes() for m in messages)
with open(sys.argv[1], "wb") as file:
  file.write(zstandard.ZstdCompressor().compress(raw[:len(raw) - int(sys.argv[2])]))
"""


def _write_synthetic_qlog(segment, cut=0, thumbnails="50"):
  env = route_playback.utilities._dashboard_worker_env(route_playback.REPO_ROOT)
  subprocess.run([route_playback.sys.executable, "-c", SYNTHETIC_QLOG, str(segment / "qlog.zst"), str(cut), thumbnails],
                 cwd=route_playback.REPO_ROOT, env=env, check=True, timeout=60)


# Cut 7 bytes: power lost mid-message. The partial last message is dropped and the rest is kept.
@pytest.mark.parametrize(("cut", "last_end"), [(0, 59.9), (7, 59.8)])
def test_real_parser_subprocess_on_a_synthetic_qlog(tmp_path, monkeypatch, offroad, cut, last_end):
  footage = tmp_path / "footage"
  for num in (40, 41):
    _write_synthetic_qlog(_make_log_segment(footage, num), cut)
  runs = []
  real_run = route_playback.subprocess.run
  monkeypatch.setattr(route_playback.subprocess, "run", lambda cmd, **kwargs: runs.append(cmd) or real_run(cmd, **kwargs))
  client = _timeline_client(footage, tmp_path / "cache", FakeExecutor())

  response = _get(client, _segment_url(40))

  assert response.status_code == 200, response.get_data(as_text=True)
  # One interpreter parsed both minutes; the neighbour is now cached.
  assert len(runs) == 1 and len(runs[0]) == 10
  assert _get(client, _segment_url(41)).get_json() == response.get_json()
  assert len(runs) == 1
  assert response.get_json() == {"thumbnailAt": 5.0, "spans": [
    [0.0, 10.0, "disengaged", 0],
    [10.0, 30.0, "engaged", 0],
    [30.0, 35.0, "overriding", 0],
    [35.0, 50.0, "engaged", 0],
    [50.0, 52.0, "engaged", 2],
    [52.0, last_end, "engaged", 0],
  ]}
  with _get(client, _segment_url(40, "thumbnail.jpg")) as thumbnail:
    assert thumbnail.data == b"\xff\xd8synthetic50"


def test_real_parser_subprocess_takes_a_spilled_thumbnail_from_the_previous_qlog(tmp_path, monkeypatch, offroad):
  footage = tmp_path / "footage"
  _write_synthetic_qlog(_make_log_segment(footage, 40), thumbnails="3,599")
  _write_synthetic_qlog(_make_log_segment(footage, 41), thumbnails="")
  client = _timeline_client(footage, tmp_path / "cache", FakeExecutor())

  assert _get(client, _segment_url(41)).get_json()["thumbnailAt"] == 0.0
  with _get(client, _segment_url(41, "thumbnail.jpg")) as thumbnail:
    assert thumbnail.data == b"\xff\xd8synthetic599"
  assert _get(client, _segment_url(40)).get_json()["thumbnailAt"] == 0.3
  with _get(client, _segment_url(40, "thumbnail.jpg")) as thumbnail:
    assert thumbnail.data == b"\xff\xd8synthetic3"


def test_route_player_shows_the_timeline_and_seeks_from_it():
  mobile = MODULE_DIR / "assets/mobile/js/components"
  player = (mobile / "RoutePlayer.js").read_text(encoding="utf-8")
  timeline = (mobile / "RouteTimeline.js").read_text(encoding="utf-8")

  # Under the video, fed the playing time; its seeks set the video's position.
  assert 'import { RouteTimeline, routeClock } from "./RouteTimeline.js"' in player
  assert '@timeupdate="onTimeUpdate"' in player
  assert "if (!this._resumeAt) this.time = this.$refs.video.currentTime" in player
  assert '<RouteTimeline :route="route" :camera="camera" :quality="quality" :time="time" @seek="seek" @loaded="onTimelineLoaded" />' in player
  assert player.index("<video ref=\"video\"") < player.index("<RouteTimeline")
  assert "this.$refs.video.currentTime = seconds" in player
  # A seek during a camera/quality switch restarts the switch at that point instead of being overwritten.
  assert "this._resumeAt = seconds\n        this.attach(true)" in player

  # Endpoints match the backend.
  assert "`/route-playback/${encodeURIComponent(this.route)}/timeline.json?${query}`" in timeline
  assert "`/route-playback/segment/${encodeURIComponent(segment)}/${part}`" in timeline
  assert 'segmentUrl(segment, "timeline.json")' in timeline
  assert 'thumbnailAt != null ? segmentUrl(segment, "thumbnail.jpg") : null' in timeline
  # Minutes already parsed come with the segment list; fetchSegments skips minutes in info.
  assert "this.segments = segments\n" in timeline
  assert "for (const { segment, timeline } of segments) {\n        if (timeline && !this.info[segment]) this.info[segment] = timeline" in timeline
  assert timeline.index("this.info[segment] = timeline") < timeline.index("this.fetchSegments(token)\n    },")
  assert ".filter(([s]) => !(s.segment in this.info))" in timeline

  # A route change stops the old fetch loop; camera/quality only reload the segment list.
  assert "route() {\n      this.reset()\n      this.load()" in timeline
  assert 'camera: "load",\n    quality: "load",' in timeline
  assert "if (token !== this._token || list !== this._list) return" in timeline

  # One request at a time; onroad shows the note and checks again slowly; busy or no connection retries soon.
  assert "if (this._fetching === token) return" in timeline
  assert timeline.count("await fetch(") == 2
  assert "if (!response || response.status === 503) {" in timeline
  assert 'this.onroad = body?.reason === "onroad"' in timeline
  assert "setTimeout(resolve, this.onroad ? ONROAD_RETRY_MS : BUSY_RETRY_MS))\n            continue" in timeline
  assert "Timeline fills in after the drive" in timeline

  # Drag shows the time and seeks on release; keys step; vertical scrolling stays with the page.
  assert 'role="slider"' in timeline and "touch-action:pan-y" in timeline
  release = 'this.dragTime = null\n      this.hoverTime = event.pointerType === "mouse" ? this.timeAt(event) : null\n'
  assert release + '      this.$emit("seek", this.timeAt(event))' in timeline
  assert '@pointercancel="onPointerCancel"' in timeline
  # Only the pointer that started the drag moves or ends it.
  assert "if (this.dragTime === null || event.pointerId !== this._pointer) return" in timeline


# --- Stage 4: player UI (layout, own controls, hover time, time of day + segment) ---

TIMELINE_JS = MODULE_DIR / "assets/mobile/js/components/RouteTimeline.js"


def _route_clock(snippet, tz="America/Los_Angeles"):
  node = shutil.which("node")
  if node is None:
    pytest.skip("node is not installed")
  # Imported from its source as a data: URL (it has no imports), so any node reads it as an ES module.
  load = "const { routeClock } = await import('data:text/javascript,' + encodeURIComponent(process.env.SOURCE))"
  harness = f"{load}\nprocess.stdout.write(JSON.stringify({snippet}))"
  result = subprocess.run([node, "--input-type=module"], input=harness, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "TZ": tz, "SOURCE": TIMELINE_JS.read_text(encoding="utf-8")})
  assert result.returncode == 0, result.stderr
  return json.loads(result.stdout)


def test_route_clock_shows_the_time_of_day_and_segment_number():
  # 2026-10-01 14:08:36 PDT; segments 0, 1 and (after an aged-out gap) 5 back to back in the playlist.
  segments = json.dumps([{"segment": "r--0", "start": 0, "duration": 60}, {"segment": "r--1", "start": 60, "duration": 60},
                         {"segment": "r--5", "start": 120, "duration": 30}])
  started = datetime(2026, 10, 1, 21, 8, 36, tzinfo=UTC).timestamp()

  assert _route_clock(f"[0, 59.9, 60, 130].map((t) => routeClock(t, {segments}, {started}))") == [
    "14:08:36 – 0", "14:09:35 – 0", "14:09:36 – 1", "14:13:46 – 5"]
  # No start time: time since the route start; no segment list yet: just the time.
  assert _route_clock(f"[routeClock(130, {segments}, null), routeClock(75, [], null)]") == ["5:10 – 5", "1:15"]
  # A drive across midnight wraps to 00:.
  late = datetime(2026, 10, 2, 6, 59, 30, tzinfo=UTC).timestamp()
  assert _route_clock(f"routeClock(45, {segments}, {late})") == "00:00:15 – 0"


def test_route_player_has_its_own_controls_and_a_large_centered_sheet_on_desktop():
  mobile = MODULE_DIR / "assets/mobile/js"
  recordings = (mobile / "views/Recordings.js").read_text(encoding="utf-8")
  player = (mobile / "components/RoutePlayer.js").read_text(encoding="utf-8")
  timeline = TIMELINE_JS.read_text(encoding="utf-8")

  # Only the route player's sheet gets the layout classes; the screen-recording sheet stays a bottom sheet.
  assert recordings.count('scrim-class="gx-route-player-scrim" sheet-class="gx-route-player-sheet"') == 1
  assert 'icon="bi-camera-video" bottomsheet scrim-class="gx-route-player-scrim"' in recordings
  # Centered at every size (phones too); only the large width is desktop-only.
  desktop = player.index("@media (min-width:768px) and (min-height:600px)")
  assert player.index(".gx-scrim--bottomsheet.gx-route-player-scrim {align-items:center") < desktop
  assert ".gx-route-player-scrim .gx-sheet.gx-route-player-sheet {max-height:calc(100dvh - 24px);border-radius:var(--radius-xl)}" in player
  assert player.index(".gx-route-player-scrim .gx-sheet.gx-route-player-sheet {width:min(1280px,94vw);max-width:none") > desktop

  # Native controls stay on the route video (play, volume, buffering, picture in picture); the bar adds what they lack.
  # Their fullscreen button is hidden because ours takes the whole player, and the click toggle would double up with theirs.
  video = player[player.index('<video ref="video"'):player.index("</video>")]
  assert ' controls controlsList="nodownload nofullscreen"' in video and "@click" not in video
  assert 'aria-label="Fullscreen"' in player and "bi-play-fill" not in player and "bi-volume" not in player
  assert '<video v-else ref="player" class="gx-video" controls' in recordings
  assert "const SPEEDS = [0.1, 0.25, 0.5, 1, 2, 4, 8]" in player
  assert 'aria-label="Playback speed"' in player and 'v-model.number="value"' in player
  # The select is its own component bound only to the rate: inside the player's template it was re-patched on every
  # timeupdate (all <option value> rewritten), which made an open speed menu flicker while the video played.
  assert '<speed-select v-model="rate" />' in player
  assert "const SpeedSelect = {" in player and "components: { RouteTimeline, SpeedSelect }" in player
  assert "<select" not in player[player.index("export const RoutePlayer"):]
  # The speed survives source changes: load() resets playbackRate to the default, and metadata reapplies it.
  assert "video.defaultPlaybackRate = this.rate\n      video.playbackRate = this.rate" in player
  assert '@loadedmetadata="applyRate"' in player and 'rate: "applyRate"' in player
  # ±10 s and keys seek through seek(), so a seek mid-switch still restarts the switch.
  assert "this.seek(this.total ? Math.min(target, this.total) : target)" in player
  assert 'aria-label="Back 10 seconds"' in player and 'aria-label="Forward 10 seconds"' in player
  # Keys never fire from inputs or with modifiers; space on a button stays that button's.
  assert 'event.ctrlKey || event.metaKey || event.altKey || editable(event.target)' in player
  assert 'target.closest("input, select, textarea, [contenteditable]")' in player
  assert 'event.key === " " && event.target === this.$refs.video' in player
  # Fullscreen: the whole player, or on iPhone the video with the system's controls.
  assert "wrapper.requestFullscreen().catch(() => {})" in player
  assert "this.$refs.video.webkitEnterFullscreen?.()" in player
  assert ".gx-route-player:-webkit-full-screen .gx-video {flex:1" in player
  # A clicked bar button doesn't keep focus, so space plays/pauses instead of pressing it again.
  assert '<div class="gx-route-controls" @mousedown="onBarMouseDown">' in player
  # Speed changed elsewhere (iPhone's own fullscreen controls) shows in the menu; duration clamps ±10 s without a timeline.
  assert '@ratechange="rate = $refs.video.playbackRate"' in player and '@durationchange="duration = $refs.video.duration"' in player
  assert "{{ clockText }}" in player and "routeClock(this.time, this.segments, this.startedAt)" in player

  # The timeline hands its segment list and start time to the player (no second fetch), and shows the time on hover.
  assert 'this.$emit("loaded", { segments, startedAt })' in timeline
  assert timeline.count("await fetch(") == 2
  assert 'else if (this.dragTime === null && event.pointerType === "mouse" && this.total) this.hoverTime = this.timeAt(event)' in timeline
  assert '@pointerleave="hoverTime = null"' in timeline
  assert '{{ label(hoverTime) }}' in timeline and '{{ label(dragTime) }}' in timeline
