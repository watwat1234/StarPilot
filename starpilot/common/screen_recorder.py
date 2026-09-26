from datetime import datetime
from pathlib import Path

# Matches the default-name check in the_galaxy/utilities.py:process_screen_recording
FILENAME_FORMAT = "%B_%d_%Y-%I-%M%p"


def lock_path(video_path: Path) -> Path:
  # Galaxy's list endpoint skips recordings that have a "<file>.mp4.lock" sibling
  return video_path.with_name(video_path.name + ".lock")


def _default_directory() -> Path:
  from openpilot.starpilot.common.starpilot_variables import SCREEN_RECORDINGS_PATH
  return SCREEN_RECORDINGS_PATH


def new_recording_path(directory: Path | None = None, now: datetime | None = None) -> Path:
  directory = directory or _default_directory()
  directory.mkdir(parents=True, exist_ok=True)
  stem = (now or datetime.now()).strftime(FILENAME_FORMAT)
  path = directory / f"{stem}.mp4"
  suffix = 2
  while path.exists() or lock_path(path).exists():
    path = directory / f"{stem}_{suffix}.mp4"
    suffix += 1
  return path


class ScreenRecording:
  """Owns the file path and .lock marker for one recording; the encoder itself lives in gui_app."""

  def __init__(self, gui_app, directory: Path | None = None):
    self._gui_app = gui_app
    self._directory = directory  # None -> SCREEN_RECORDINGS_PATH
    self._path: Path | None = None
    self._started_at = 0.0

  @property
  def is_recording(self) -> bool:
    return self._gui_app.is_recording

  @property
  def started_at(self) -> float:
    return self._started_at

  def start(self, now_monotonic: float) -> bool:
    if self.is_recording:
      return False
    path = new_recording_path(self._directory)
    lock = lock_path(path)
    lock.touch()
    if not self._gui_app.start_recording(str(path)):
      lock.unlink(missing_ok=True)
      return False
    self._path = path
    self._started_at = now_monotonic
    return True

  def stop(self) -> None:
    frames = self._gui_app.stop_recording()
    if self._path is not None:
      if not frames:
        self._path.unlink(missing_ok=True)  # ffmpeg leaves an unplayable stub when nothing was encoded
      lock_path(self._path).unlink(missing_ok=True)
      self._path = None

  def toggle(self, now_monotonic: float) -> None:
    if self.is_recording:
      self.stop()
    else:
      self.start(now_monotonic)
