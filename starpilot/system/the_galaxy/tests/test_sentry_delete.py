import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from test_dashboard_stats import FakeParams, MODULE_DIR
from test_sentry_push_and_routing import the_galaxy


def _iso(hour: int) -> str:
  return datetime(2026, 9, 1, hour, 0, 0, tzinfo=timezone.utc).isoformat()


class SentryEnv:
  def __init__(self, roots: list[Path], client):
    self.roots = roots
    self.client = client

  def add_event(self, event_id: str, hour: int, roots: tuple[int, ...] = (0,)) -> None:
    """Records an event newer than any recorded before it; its image directory exists in each of `roots`."""
    image_paths = []
    for root_index in roots:
      directory = self.roots[root_index] / event_id
      directory.mkdir(parents=True, exist_ok=True)
      image = directory / "wide.jpg"
      image.write_bytes(b"jpeg")
      image_paths.append(str(image.resolve()))
    the_galaxy._record_sentry_event({
      "eventId": event_id,
      "kind": "warning",
      "detectedAt": _iso(hour),
      "imagePaths": image_paths,
      "message": event_id,
    })

  def set_last_event(self, event_id: str) -> None:
    event = next(event for event in the_galaxy._sentry_event_catalog() if event["eventId"] == event_id)
    the_galaxy.params.put("SentryModeLastEvent", json.dumps(event))

  def catalog_ids(self) -> list[str]:
    return [event["eventId"] for event in the_galaxy._load_sentry_event_catalog_unlocked()]

  def last_event_id(self):
    raw = the_galaxy.params.get("SentryModeLastEvent", encoding="utf-8")
    return json.loads(raw)["eventId"] if raw else None


@pytest.fixture
def env(monkeypatch, tmp_path):
  assert the_galaxy._import_galaxy_web_symbols()
  monkeypatch.setattr(the_galaxy, "params", FakeParams({"IsOffroad": True}))
  monkeypatch.setattr(the_galaxy, "_get_galaxy_dir", lambda: tmp_path)
  roots = [(tmp_path / "root0").resolve(), (tmp_path / "root1").resolve()]
  for root in roots:
    root.mkdir()
  monkeypatch.setattr(the_galaxy, "_sentry_event_roots", lambda: tuple(roots))

  app = the_galaxy.Flask(
    f"test_galaxy_delete_{time.monotonic_ns()}",
    template_folder=str(MODULE_DIR / "templates"),
    static_folder=str(MODULE_DIR / "assets"),
  )
  the_galaxy.setup(app)
  return SentryEnv(roots, app.test_client())


def test_delete_single_removes_storage_and_catalog(env):
  env.add_event("a", 1)
  env.add_event("b", 2, roots=(0, 1))

  response = env.client.delete("/api/sentry/events/b")

  assert response.status_code == 200
  assert env.catalog_ids() == ["a"]
  assert not (env.roots[0] / "b").exists() and not (env.roots[1] / "b").exists()
  assert (env.roots[0] / "a").exists()


def test_delete_single_unknown_event_is_404(env):
  env.add_event("a", 1)
  assert env.client.delete("/api/sentry/events/nope").status_code == 404


def test_delete_single_resyncs_last_event_to_newest_survivor(env):
  env.add_event("a", 1)
  env.add_event("b", 2)
  env.add_event("c", 3)
  env.set_last_event("c")

  assert env.client.delete("/api/sentry/events/c").status_code == 200
  assert env.last_event_id() == "b"

  assert env.client.delete("/api/sentry/events/a").status_code == 200
  assert env.last_event_id() == "b"  # a was not the last event, so the param is untouched


def test_delete_single_removes_last_event_param_when_nothing_is_left(env):
  env.add_event("a", 1)
  env.set_last_event("a")

  assert env.client.delete("/api/sentry/events/a").status_code == 200
  assert env.last_event_id() is None


def test_delete_all_requires_all_flag(env):
  env.add_event("a", 1)
  assert env.client.delete("/api/sentry/events").status_code == 400
  assert env.catalog_ids() == ["a"]


def test_delete_all_clears_everything_and_the_last_event_param(env):
  env.add_event("a", 1)
  env.add_event("b", 2, roots=(0, 1))
  env.set_last_event("b")

  response = env.client.delete("/api/sentry/events?all=1")

  assert response.get_json() == {"deleted": 2, "failed": 0}
  assert env.catalog_ids() == []
  assert env.last_event_id() is None
  assert not (env.roots[1] / "b").exists()


def test_delete_range_resyncs_last_event_to_newest_survivor(env):
  env.add_event("old", 1)
  env.add_event("mid", 5)
  env.add_event("new", 9)
  env.set_last_event("new")

  # Delete the newest event only: since is inclusive, so [08:00, ...) matches "new".
  response = env.client.delete("/api/sentry/events", query_string={"since": _iso(8)})

  assert response.get_json() == {"deleted": 1, "failed": 0}
  assert env.catalog_ids() == ["mid", "old"]
  assert env.last_event_id() == "mid"


def test_delete_range_leaves_last_event_param_when_it_survives(env):
  env.add_event("old", 1)
  env.add_event("new", 9)
  env.set_last_event("new")

  env.client.delete("/api/sentry/events", query_string={"until": _iso(5)})

  assert env.catalog_ids() == ["new"]
  assert env.last_event_id() == "new"


def _fail_rmtree_under(monkeypatch, failing_root: Path):
  real_rmtree = the_galaxy.shutil.rmtree

  def rmtree(path, *args, **kwargs):
    if failing_root in Path(path).parents:
      raise OSError("simulated rmtree failure")
    return real_rmtree(path, *args, **kwargs)

  monkeypatch.setattr(the_galaxy.shutil, "rmtree", rmtree)


def test_bulk_delete_keeps_event_and_logs_when_a_later_root_fails(env, monkeypatch):
  env.add_event("a", 1)
  env.add_event("b", 2, roots=(0, 1))
  _fail_rmtree_under(monkeypatch, env.roots[1])
  logged = []
  monkeypatch.setattr(the_galaxy.cloudlog, "exception", lambda message, *args, **kwargs: logged.append(message))

  response = env.client.delete("/api/sentry/events?all=1")

  assert response.get_json() == {"deleted": 1, "failed": 1}
  assert env.catalog_ids() == ["b"]  # still listed: its root-1 storage still exists, so a retry can finish it
  assert (env.roots[1] / "b").exists()
  assert not (env.roots[0] / "b").exists()  # the root that succeeded was still cleaned
  assert len(logged) == 1 and "root1" in logged[0]


def test_single_delete_returns_500_when_storage_removal_fails(env, monkeypatch):
  env.add_event("b", 2, roots=(0, 1))
  _fail_rmtree_under(monkeypatch, env.roots[1])
  monkeypatch.setattr(the_galaxy.cloudlog, "exception", lambda *args, **kwargs: None)

  response = env.client.delete("/api/sentry/events/b")

  assert response.status_code == 500
  assert "error" in response.get_json()
  assert env.catalog_ids() == ["b"]


def test_list_filters_by_range_with_exclusive_until(env):
  env.add_event("old", 1)
  env.add_event("mid", 5)
  env.add_event("new", 9)

  response = env.client.get("/api/sentry/events", query_string={"since": _iso(5), "until": _iso(9)})

  assert [event["eventId"] for event in response.get_json()["events"]] == ["mid"]


def test_list_rejects_an_invalid_timestamp(env):
  response = env.client.get("/api/sentry/events", query_string={"since": "yesterday"})

  assert response.status_code == 400
  assert "since" in response.get_json()["error"]


def test_stored_event_images_are_cacheable(env):
  env.add_event("a", 1)

  with env.client.get("/api/sentry/images/a/wide.jpg") as response:
    assert response.status_code == 200
    assert response.cache_control.max_age == the_galaxy._SENTRY_IMAGE_CACHE_SECONDS


timelapse = the_galaxy.sentry_timelapse


def test_test_events_are_first_class_timelapse_sources(env):
  env.add_event("test-123-abcd", 1)
  env.add_event("real", 2)
  events = the_galaxy._sentry_event_catalog()

  frames = the_galaxy._sentry_timelapse_sources(events, ("wide.jpg",), timelapse.MAX_FRAMES)

  assert len(frames) == 2


def test_timelapse_skips_a_frame_deleted_mid_encode_and_caps_ffmpeg_threads(env, monkeypatch, tmp_path, capsys):
  env.add_event("a", 1)
  env.add_event("b", 2)
  env.add_event("c", 3)
  frames = the_galaxy._sentry_timelapse_sources(the_galaxy._sentry_event_catalog(), ("wide.jpg",), timelapse.MAX_FRAMES)
  assert len(frames) == 3

  def render(paths, detected_at, kind, gap, index, positions, kinds, output_path):
    if "b" in {path.parent.name for path in paths}:
      raise FileNotFoundError(str(paths[0]))
    output_path.write_bytes(b"frame")

  encoded = []
  commands = []

  def run(command, **kwargs):
    commands.append(command)
    sequence = Path(command[command.index("-i") + 1]).parent
    encoded.extend(sorted(path.name for path in sequence.iterdir()))
    Path(command[-1]).write_bytes(b"mp4")

    class Result:
      returncode = 0
      stderr = b""

    return Result()

  monkeypatch.setattr(timelapse, "render_frame", render)
  monkeypatch.setattr(timelapse.subprocess, "run", run)

  output = tmp_path / "out.mp4"
  timelapse.encode(frames, [1.0, 1.0, 1.0], "ffmpeg", output)

  assert output.read_bytes() == b"mp4"
  assert len(encoded) == 2 * timelapse.OUTPUT_FPS  # frames a and c, one second each; b was skipped
  assert "/b/" in capsys.readouterr().err
  command = commands[0]
  assert command[command.index("-threads") + 1] == str(timelapse.FFMPEG_THREADS)


@pytest.mark.skipif(shutil.which(getattr(the_galaxy.utilities, "FFMPEG_BIN", "ffmpeg")) is None, reason="ffmpeg not installed")
def test_timelapse_real_encode_skips_a_frame_deleted_mid_encode(env, monkeypatch, tmp_path):
  from PIL import Image

  for hour, event_id in enumerate(("a", "b", "c"), start=1):
    env.add_event(event_id, hour)
    Image.new("RGB", (320, 180), (40 * hour, 90, 160)).save(env.roots[0] / event_id / "wide.jpg")
  frames = the_galaxy._sentry_timelapse_sources(the_galaxy._sentry_event_catalog(), ("wide.jpg",), timelapse.MAX_FRAMES)
  assert len(frames) == 3

  real_render = timelapse.render_frame

  def render_with_delete(paths, detected_at, kind, gap, index, positions, kinds, output_path):
    if index == 1:  # the user deletes the middle event just before its frame is rendered
      shutil.rmtree(paths[0].parent)
    return real_render(paths, detected_at, kind, gap, index, positions, kinds, output_path)

  monkeypatch.setattr(timelapse, "render_frame", render_with_delete)

  video = tmp_path / "out.mp4"
  timelapse.encode(frames, [1.0, 1.0, 1.0], the_galaxy.utilities.FFMPEG_BIN, video)

  assert video.read_bytes()[4:8] == b"ftyp"
  probe = subprocess.run(
    [str(Path(the_galaxy.utilities.FFMPEG_BIN).with_name("ffprobe")), "-v", "error", "-select_streams", "v:0",
     "-count_frames", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(video)],
    capture_output=True, text=True,
  )
  assert probe.returncode == 0, probe.stderr
  assert int(probe.stdout.strip()) == 2 * timelapse.OUTPUT_FPS  # frames a and c, one second each


@pytest.mark.skipif(shutil.which(getattr(the_galaxy.utilities, "FFMPEG_BIN", "ffmpeg")) is None or shutil.which("nice") is None,
                    reason="ffmpeg or nice not installed")
def test_timelapse_real_worker_renders_at_lowest_priority(env):
  from PIL import Image

  for hour, event_id in enumerate(("a", "b"), start=1):
    env.add_event(event_id, hour)
    Image.new("RGB", (320, 180), (60 * hour, 90, 160)).save(env.roots[0] / event_id / "wide.jpg")
  frames = the_galaxy._sentry_timelapse_sources(the_galaxy._sentry_event_catalog(), ("wide.jpg",), timelapse.MAX_FRAMES)

  data = timelapse.render_in_worker(frames, [0.5, 0.5], the_galaxy.utilities.FFMPEG_BIN)

  assert data[4:8] == b"ftyp"


def test_timelapse_worker_command_is_niced(env, monkeypatch):
  launched = []

  class FakeProcess:
    returncode = 0

    def __init__(self, command, **kwargs):
      launched.append((command, kwargs))
      job = json.loads(Path(command[-1]).read_text())
      Path(job["output"]).write_bytes(b"mp4")
      self.pid = 0

    def communicate(self, timeout=None):
      return b"", b""

  monkeypatch.setattr(timelapse.subprocess, "Popen", FakeProcess)

  assert timelapse.render_in_worker([], [], "ffmpeg") == b"mp4"
  command, kwargs = launched[0]
  assert command[:3] == ["nice", "-n", str(timelapse.WORKER_NICE)]
  assert command[-2] == "worker"
  assert kwargs["start_new_session"] is True


def _process_alive(pid: int) -> bool:
  """True while the process exists and is not a zombie (a killed orphan may linger unreaped in a container)."""
  try:
    state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
  except (FileNotFoundError, IndexError):
    return False
  return state != "Z"


def test_timelapse_timeout_kills_the_worker_and_its_children(monkeypatch, tmp_path):
  real_popen = subprocess.Popen
  child_pid_file = tmp_path / "child.pid"

  def hanging_worker(command, **kwargs):
    # Stands in for a stuck worker that has started ffmpeg: a shell with a child still running.
    return real_popen(["sh", "-c", f"sleep 30 & echo $! > {child_pid_file}; wait"], **kwargs)

  monkeypatch.setattr(timelapse.subprocess, "Popen", hanging_worker)

  with pytest.raises(TimeoutError):
    timelapse.render_in_worker([], [], "ffmpeg", timeout=0.5)

  child_pid = int(child_pid_file.read_text())
  deadline = time.monotonic() + 2
  while _process_alive(child_pid) and time.monotonic() < deadline:
    time.sleep(0.05)
  assert not _process_alive(child_pid)


def test_timelapse_route_returns_503_on_timeout(env, monkeypatch):
  env.add_event("a", 1)
  monkeypatch.setattr(the_galaxy.cloudlog, "error", lambda *args, **kwargs: None, raising=False)

  def timed_out(*args, **kwargs):
    raise TimeoutError("Timelapse took too long and was stopped.")

  monkeypatch.setattr(timelapse, "render_in_worker", timed_out)

  response = env.client.get("/api/sentry/timelapse")

  assert response.status_code == 503
  assert "too long" in response.get_json()["error"]
  assert not the_galaxy._SENTRY_TIMELAPSE_LOCK.locked()


def test_timelapse_route_refuses_a_second_render(env):
  env.add_event("a", 1)
  assert the_galaxy._SENTRY_TIMELAPSE_LOCK.acquire(blocking=False)
  try:
    response = env.client.get("/api/sentry/timelapse")
  finally:
    the_galaxy._SENTRY_TIMELAPSE_LOCK.release()

  assert response.status_code == 409
