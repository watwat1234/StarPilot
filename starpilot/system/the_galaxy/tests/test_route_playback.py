import time

import pytest
from flask import Flask

from test_dashboard_stats import FakeParams, MODULE_DIR, _install_server_import_stubs

_install_server_import_stubs()

from openpilot.starpilot.system.the_galaxy import route_playback  # noqa: E402


ROUTE_NAME = "0000006a--9f0a7bdf9c"
PLAYLIST_URL = f"/route-playback/{ROUTE_NAME}/qcamera.m3u8"


def _make_segment(root, segment_num, route_name=ROUTE_NAME, payload=b"ts", locked=False):
  segment = root / f"{route_name}--{segment_num}"
  segment.mkdir(parents=True)
  (segment / "qcamera.ts").write_bytes(payload)
  if locked:
    (segment / "qcamera.ts.lock").touch()
  return segment


@pytest.fixture
def last_duration(monkeypatch):
  durations = {"seconds": 23.5}
  monkeypatch.setattr(route_playback.utilities, "get_video_duration", lambda path: durations["seconds"])
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


def test_playlist_falls_back_to_a_full_minute_without_ffprobe(tmp_path, monkeypatch):
  _make_segment(tmp_path, 0)
  def missing_ffprobe(path):
    raise FileNotFoundError("ffprobe")
  monkeypatch.setattr(route_playback.utilities, "get_video_duration", missing_ffprobe)

  _, lines = _entries(_client(tmp_path).get(PLAYLIST_URL).get_data(as_text=True))

  assert "#EXTINF:60.000," in lines


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
