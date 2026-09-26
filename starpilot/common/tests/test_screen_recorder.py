from pathlib import Path
from datetime import datetime

from openpilot.starpilot.common.screen_recorder import ScreenRecording, lock_path, new_recording_path


class FakeGuiApp:
  def __init__(self, can_start=True):
    self.is_recording = False
    self.can_start = can_start
    self.paths = []
    self.frames = 5

  def start_recording(self, path):
    if self.is_recording or not self.can_start:
      return False
    self.is_recording = True
    self.paths.append(path)
    return True

  def stop_recording(self):
    self.is_recording = False
    return self.frames


NOW = datetime(2026, 9, 26, 14, 5)


def test_filename_format_and_collision(tmp_path):
  first = new_recording_path(tmp_path, NOW)
  assert first.name == "September_26_2026-02-05PM.mp4"
  first.touch()
  assert new_recording_path(tmp_path, NOW).name == "September_26_2026-02-05PM_2.mp4"


def test_creates_directory(tmp_path):
  target = tmp_path / "a" / "b"
  new_recording_path(target, NOW)
  assert target.is_dir()


def test_lock_created_and_removed(tmp_path):
  app = FakeGuiApp()
  rec = ScreenRecording(app, tmp_path)
  assert rec.start(1.0)
  path = Path(app.paths[0])
  assert lock_path(path).exists()
  rec.stop()
  assert not lock_path(path).exists()
  assert not app.is_recording


def test_double_start_is_noop(tmp_path):
  app = FakeGuiApp()
  rec = ScreenRecording(app, tmp_path)
  assert rec.start(1.0)
  assert not rec.start(2.0)
  assert len(app.paths) == 1


def test_stop_then_start_again(tmp_path):
  app = FakeGuiApp()
  rec = ScreenRecording(app, tmp_path)
  rec.toggle(1.0)
  rec.toggle(2.0)
  rec.toggle(3.0)
  assert app.is_recording
  assert len(app.paths) == 2


def test_failed_start_leaves_no_lock(tmp_path):
  rec = ScreenRecording(FakeGuiApp(can_start=False), tmp_path)
  assert not rec.start(1.0)
  assert list(tmp_path.iterdir()) == []


def test_recording_without_frames_is_deleted(tmp_path):
  app = FakeGuiApp()
  app.frames = 0
  rec = ScreenRecording(app, tmp_path)
  rec.start(1.0)
  path = Path(app.paths[0])
  path.touch()  # stand-in for ffmpeg's empty output
  rec.stop()
  assert list(tmp_path.iterdir()) == []


def test_recording_with_frames_is_kept(tmp_path):
  app = FakeGuiApp()
  rec = ScreenRecording(app, tmp_path)
  rec.start(1.0)
  path = Path(app.paths[0])
  path.touch()
  rec.stop()
  assert list(tmp_path.iterdir()) == [path]
