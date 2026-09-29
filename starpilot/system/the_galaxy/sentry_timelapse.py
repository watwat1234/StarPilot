#!/usr/bin/env python3
"""Sentry Mode timelapse rendering.

The Galaxy route picks the frames and calls render_in_worker(), which runs this file as a separate
`nice -n 19` process so frame rendering and the ffmpeg encode never compete with driving or Sentry
processes for CPU. The worker only needs the standard library and Pillow.
"""
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

FRAME_WIDTH = 640
MAX_FRAMES = 300
OUTPUT_FPS = 30
MAX_SECONDS = 60.0
MIN_FRAME_SECONDS = 0.04
# Gap-paced frame time: base + scale * log10(1 + gap in minutes), clamped. About 0.22s for a 1 minute gap,
# 0.6s for an hour, 0.9s for a day and 1.15s for a week, so bursts stay readable and lulls still pause.
GAP_BASE_SECONDS = 0.15
GAP_SCALE_SECONDS = 0.25
GAP_MAX_SECONDS = 1.5
LAST_FRAME_SECONDS = 1.0
FILES = {"wide": ("wide.jpg",), "driver": ("driver.jpg",), "both": ("wide.jpg", "driver.jpg")}
# kind -> (badge label, badge color); alarms also get a border so they stand out at a glance.
BADGES = {
  "warning": ("WARNING", (245, 166, 35)),
  "alarm": ("ALARM", (220, 38, 38)),
  "selfie": ("SELFIE", (59, 130, 246)),
}
DEFAULT_BADGE = ("EVENT", (140, 140, 140))
ALARM_BORDER = 8
BAR_HEIGHT = 26
FONTS = (
  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
  "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)

# CPU guards: the whole worker (and the ffmpeg it starts) runs at the lowest priority, x264 is held to one
# encoder thread, and a render that runs past the timeout is killed along with its ffmpeg. ffmpeg 7+ still
# runs its decode/encode/mux stages on their own threads, so a render peaks around 1.5-2 cores (about 3 with
# two encoder threads), all at nice 19 so everything else gets the CPU first.
WORKER_NICE = 19
FFMPEG_THREADS = 1
TIMEOUT_SECONDS = 180

Frame = tuple[datetime, str, list[Path | None]]


def frame_gaps(frames: list[Frame]) -> list[float | None]:
  """Seconds from each frame to the next one; None for the last frame."""
  return [(frames[i + 1][0] - frames[i][0]).total_seconds() for i in range(len(frames) - 1)] + [None]


def frame_durations(gaps: list[float | None], timing: str, fps: int) -> list[float]:
  """How long each frame is shown. 'gap' compresses the real gaps logarithmically, 'even' uses 1/fps.

  Either way the total is scaled down to MAX_SECONDS if it would run longer.
  """
  if timing == "even":
    durations = [1.0 / fps] * len(gaps)
  else:
    durations = []
    for gap in gaps:
      if gap is None:
        durations.append(LAST_FRAME_SECONDS)
        continue
      seconds = GAP_BASE_SECONDS + GAP_SCALE_SECONDS * math.log10(1 + max(gap, 0.0) / 60.0)
      durations.append(min(seconds, GAP_MAX_SECONDS))

  total = sum(durations)
  if total > MAX_SECONDS:
    scale = MAX_SECONDS / total
    durations = [max(duration * scale, MIN_FRAME_SECONDS) for duration in durations]
  return durations


def render_in_worker(frames: list[Frame], durations: list[float], ffmpeg_bin: str, timeout: float = TIMEOUT_SECONDS) -> bytes:
  """Renders and encodes the timelapse in a low-priority worker process and returns the MP4 bytes.

  Raises TimeoutError if the worker runs past `timeout` (it is killed with its ffmpeg), RuntimeError on failure.
  """
  with tempfile.TemporaryDirectory(prefix="sentry-timelapse-job-") as job_dir:
    job_path = Path(job_dir) / "job.json"
    output_path = Path(job_dir) / "timelapse.mp4"
    job_path.write_text(json.dumps({
      "frames": [[detected_at.isoformat(), kind, [str(path) if path else None for path in paths]] for detected_at, kind, paths in frames],
      "durations": durations,
      "ffmpeg": ffmpeg_bin,
      "output": str(output_path),
    }))
    command = ["nice", "-n", str(WORKER_NICE), sys.executable, str(Path(__file__).resolve()), "worker", str(job_path)]
    # Its own session, so a timeout can kill the worker and the ffmpeg it started together.
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    try:
      _, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
      os.killpg(process.pid, signal.SIGKILL)
      process.communicate()
      raise TimeoutError("Timelapse took too long and was stopped.") from error
    if process.returncode != 0 or not output_path.is_file():
      detail = stderr.decode("utf-8", "replace").strip()
      raise RuntimeError(detail.splitlines()[-1] if detail else f"Timelapse worker exited with {process.returncode}.")
    return output_path.read_bytes()


def _format_gap(seconds: float) -> str:
  seconds = int(max(seconds, 0))
  if seconds < 60:
    return f"{seconds}s"
  minutes = seconds // 60
  if minutes < 60:
    return f"{minutes}m"
  hours, minutes = divmod(minutes, 60)
  if hours < 24:
    return f"{hours}h {minutes}m"
  days, hours = divmod(hours, 24)
  return f"{days}d {hours}h"


def _timeline(frames: list[Frame]) -> list[float]:
  """Each frame's position (0..1) along the real time span of the range."""
  start = frames[0][0]
  span = (frames[-1][0] - start).total_seconds()
  if span <= 0:
    return [0.5] * len(frames)
  return [(frame[0] - start).total_seconds() / span for frame in frames]


def _font(size: int):
  from PIL import ImageFont

  for font_path in FONTS:
    try:
      return ImageFont.truetype(font_path, size)
    except OSError:
      continue
  try:
    return ImageFont.load_default(size=size)
  except TypeError:
    return ImageFont.load_default()


def render_frame(
  paths: list[Path | None], detected_at: datetime, kind: str, gap_to_next: float | None,
  index: int, positions: list[float], kinds: list[str], output_path: Path,
) -> None:
  from PIL import Image, ImageDraw

  # Keyed by slot (not appended) so a camera this event is missing (e.g. a driver-only Comma
  # Selfie in "both" mode) leaves its tile position blank instead of shifting the others over.
  tiles = {}
  for slot, path in enumerate(paths):
    if path is None:
      continue
    with Image.open(path) as source:
      image = source.convert("RGB")
    height = max(2, round(image.height * FRAME_WIDTH / image.width) // 2 * 2)
    tiles[slot] = image.resize((FRAME_WIDTH, height))

  frame_height = max((tile.height for tile in tiles.values()), default=FRAME_WIDTH * 3 // 4)
  frame = Image.new("RGB", (FRAME_WIDTH * len(paths), frame_height))
  for slot, tile in tiles.items():
    frame.paste(tile, (slot * FRAME_WIDTH, 0))

  badge_label, badge_color = BADGES.get(kind, DEFAULT_BADGE)
  label = detected_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
  draw = ImageDraw.Draw(frame)
  font = _font(22)
  small_font = _font(17)

  # The overlays are always inset by the border width, so they do not jump when an alarm frame draws its border.
  inset = ALARM_BORDER
  if kind == "alarm":
    draw.rectangle((0, 0, frame.width - 1, frame.height - 1), outline=badge_color, width=ALARM_BORDER)

  # Drawn as shapes plus text, not a Unicode glyph, so it does not depend on the font having the symbol.
  x, y = 10 + inset, 8 + inset
  dot = 14
  badge_width = draw.textlength(badge_label, font=font)
  left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
  line_height = bottom - top
  total = dot + 8 + badge_width + 16 + (right - left)
  gap_text = f"next event in {_format_gap(gap_to_next)}" if gap_to_next is not None else "last event"
  box_bottom = y + line_height + 8 + 22
  draw.rectangle((x - 6, y - 4, x + total + 6, box_bottom), fill=(0, 0, 0))
  draw.ellipse((x, y + (line_height - dot) // 2 + 2, x + dot, y + (line_height - dot) // 2 + 2 + dot), fill=badge_color)
  draw.text((x + dot + 8, y), badge_label, fill=badge_color, font=font)
  draw.text((x + dot + 8 + badge_width + 16, y), label, fill=(255, 255, 255), font=font)
  draw.text((x, y + line_height + 8), gap_text, fill=(170, 170, 170), font=small_font)

  # Timeline bar along the bottom: every event in real-time position, the current one highlighted.
  bar_top = frame.height - inset - BAR_HEIGHT
  bar_left, bar_right = inset + 10, frame.width - inset - 10
  draw.rectangle((inset, bar_top, frame.width - inset, frame.height - inset), fill=(0, 0, 0))
  middle = bar_top + BAR_HEIGHT // 2
  draw.line((bar_left, middle, bar_right, middle), fill=(120, 120, 120), width=3)
  for other_index, position in enumerate(positions):
    tick_x = bar_left + position * (bar_right - bar_left)
    tick_color = BADGES.get(kinds[other_index], DEFAULT_BADGE)[1]
    if other_index > index:
      tick_color = tuple(channel * 2 // 5 for channel in tick_color)
    draw.line((tick_x, bar_top + 4, tick_x, frame.height - inset - 4), fill=tick_color, width=3)
  current_x = bar_left + positions[index] * (bar_right - bar_left)
  draw.rectangle((current_x - 3, bar_top + 2, current_x + 3, frame.height - inset - 2), fill=(255, 255, 255))
  frame.save(output_path, "JPEG", quality=88)


def encode(frames: list[Frame], durations: list[float], ffmpeg_bin: str, output_path: Path) -> None:
  """Renders the frames and encodes them to an MP4 at output_path, each shown for its duration; raises RuntimeError on failure.

  Each rendered frame is repeated (as hard links) to fill its duration at the output frame rate, so ffmpeg only
  sees a plain image sequence. The device's ffmpeg is a minimal build with no fps filter, and the concat
  demuxer's per-file durations were unreliable there.
  """
  gaps = frame_gaps(frames)
  positions = _timeline(frames)
  kinds = [frame[1] for frame in frames]
  with tempfile.TemporaryDirectory(prefix="sentry-timelapse-") as work_dir:
    work = Path(work_dir)
    sequence = work / "sequence"
    sequence.mkdir()
    elapsed = 0.0
    written = 0
    for index, (detected_at, kind, paths) in enumerate(frames):
      rendered = work / f"frame{index:05d}.jpg"
      try:
        render_frame(paths, detected_at, kind, gaps[index], index, positions, kinds, rendered)
      except FileNotFoundError:
        # The event was deleted while the timelapse was encoding; skip its frame.
        print(f"sentry timelapse skipped a deleted frame: {paths}", file=sys.stderr)
        continue
      # Cumulative rounding keeps the total exact; every frame gets at least one output frame.
      elapsed += durations[index]
      end = max(round(elapsed * OUTPUT_FPS), written + 1)
      while written < end:
        target = sequence / f"{written:06d}.jpg"
        try:
          os.link(rendered, target)
        except OSError:
          shutil.copyfile(rendered, target)
        written += 1

    command = [
      ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
      "-framerate", str(OUTPUT_FPS), "-i", str(sequence / "%06d.jpg"),
      "-c:v", "libx264", "-threads", str(FFMPEG_THREADS), "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "26",
      "-movflags", "+faststart", str(output_path),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0 or not output_path.is_file():
      detail = result.stderr.decode("utf-8", "replace").strip()[-400:]
      raise RuntimeError(f"Timelapse encoding failed: {detail.splitlines()[-1] if detail else 'ffmpeg exited with ' + str(result.returncode)}")


def run_worker(job_path: str) -> None:
  job = json.loads(Path(job_path).read_text())
  frames = [
    (datetime.fromisoformat(detected_at), kind, [Path(path) if path else None for path in paths])
    for detected_at, kind, paths in job["frames"]
  ]
  encode(frames, job["durations"], job["ffmpeg"], Path(job["output"]))


def main() -> None:
  if len(sys.argv) == 3 and sys.argv[1] == "worker":
    try:
      run_worker(sys.argv[2])
    except RuntimeError as error:
      print(str(error), file=sys.stderr)
      sys.exit(1)
    return
  print("Usage: sentry_timelapse.py worker <job.json>", file=sys.stderr)
  sys.exit(2)


if __name__ == "__main__":
  main()
