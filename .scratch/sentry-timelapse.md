# Sentry timelapse: MP4 built from retained event images

Companion to `sentry-history-pagination.md` (history refresh fix, pagination, date filter, bulk delete). This covers the timelapse feature added on top. Branch flow: developed on `sentry-history-pagination`, cherry-picked to `wat-ioniq-tuning` and `wat-bolt-tuning`, all three pushed to `origin` and `github`.
All paths are under `starpilot/system/the_galaxy/`.

## Status
- Confirmed by the user on the device: the Sentry page (pagination etc.) is useful, and the timelapse feature works ("this is pretty great") after the ffmpeg fix below.
- Verified on the dev box only: the encode helpers, run against synthetic images with both the system ffmpeg and the repo's bundled ffmpeg. The Flask route itself and the JS were never run here (no `node`, no `pytest`, the module needs the full device environment). `py_compile` passes.
- Not tested: encode time on the device CPU, very large histories (600 frames), the GIF path (removed, see below).

## What it does
`GET /api/sentry/timelapse` builds one MP4 from the retained Sentry event images and returns it as a download. The mobile Sentry view has a "Make timelapse" button (with Camera and Pacing pickers) in the history panel. It honors the active date filter, so the label becomes "Timelapse of range".

### Endpoint (`the_galaxy.py`)
- Params: `since` / `until` (same as the events list), `camera=wide|driver|both` (default `wide`; `both` puts them side by side), `timing=gap|even` (default `gap`), `fps` (1-30, default 4, only used by `even`).
- Offroad only (409 otherwise). 400 for bad params, 404 if no event in range has the requested images, 409 if a timelapse is already being made (single non-blocking lock), 500 on encode failure.
- Response headers: `X-Timelapse-Frames`, `X-Timelapse-Seconds`; `Content-Disposition: attachment`. The whole MP4 is built in a temp dir, read into memory and returned; nothing is cached on disk.
- Frame selection: events with a parseable `detectedAt`, not `test-*`, having every requested image. Oldest first. Sampled evenly down to 600 frames.
- Helpers: `_sentry_timelapse_sources`, `_sentry_timelapse_gaps`, `_sentry_timelapse_durations`, `_format_sentry_gap`, `_sentry_timelapse_timeline`, `_render_sentry_timelapse_frame`, `_encode_sentry_timelapse`, `_sentry_timelapse_font`. Constants are the `_SENTRY_TIMELAPSE_*` block just above `_capture_sentry_test_images`.

### What is drawn on each frame (Pillow, not ffmpeg)
- Frames are scaled to 640px wide per camera (1280 for `both`).
- Colored dot + kind label next to the timestamp: WARNING amber, ALARM red, SELFIE blue, anything else gray "EVENT". Alarm frames also get an 8px red border, with the badge inset inside it.
- Second line: "next event in 4h 50m", or "last event" on the final frame. It is the gap to the *next* event because that is how long the frame is held.
- Timeline bar along the bottom: every event at its real-time position in the range, kind-colored, not-yet-shown ones dimmed, the current one a white marker.
- Drawn as shapes plus text, never a Unicode glyph, so it does not depend on the device font having the symbol. Font is DejaVu Sans Bold if present, else Pillow's default.
- Selfie events only store `driver.jpg`, so they appear only in Driver timelapses. `power_off` events have no photos and never appear.

### Pacing
- `gap`: frame time = `0.15 + 0.25 * log10(1 + gap_minutes)`, clamped to 1.5s. Roughly 0.22s for 1 min, 0.6s for 1 h, 0.9s for a day, 1.15s for a week. Real proportional timing is useless (a 10s burst vs a 3-week lull is a factor of 100,000). Last frame holds 1.0s.
- `even`: 1/fps per frame.
- Either way the total is scaled down if it exceeds 60s (floor 0.04s per frame). A 700-event test set sampled to 600 frames and measured 60.0s.

### Mobile UI (`assets/mobile/js/views/Sentry.js`, `api.js`)
- Camera picker (Road/Driver/Both), Pacing picker (By time gaps / Even), and the button. Shown when history is open and non-empty.
- `api.getSentryTimelapse({since, until, camera, timing})` fetches a blob and returns `{blob, frames, seconds}`. The view downloads it as `sentry-timelapse.mp4` through an object URL. Fetch-as-blob rather than a plain link, so errors come back as JSON and there is no auth-header problem.
- The desktop `sentry.js` has **no** timelapse button (and no date filter or bulk delete). Deprecated UI, deliberately skipped.

## The ffmpeg gotcha (why the first deployed version failed)
The device runs the repo's bundled ffmpeg (`.venv/lib/python3.12/site-packages/ffmpeg/install/bin/ffmpeg`, resolved by `utilities._resolve_ffmpeg_binary`). It is built with `--disable-everything` and an explicit whitelist:
- Filters: `blend, vflip, format, scale, aformat, anull, aresample, null`. **No `fps` filter.**
- Encoders: `libx264, aac, ffvhuff, rawvideo, png, mjpeg` plus hardware wrappers. Demuxers include `image2` and `concat`.

The first version used `-vf fps=30` and failed on the device with `No option name near '30'` (exit 234). It was only tested with the system ffmpeg, which has `fps`. The dev box's system ffmpeg is NOT representative; always test against the bundled binary.

Attempted and rejected: the concat demuxer with per-file `duration` plus `-r 30`. On this build the last entry's duration is mishandled (a 2.2s list rendered as 3.17s, or 1.37s without the repeated last entry), and `-t` behaved oddly.

Final approach (`_encode_sentry_timelapse`): render each frame once, then repeat it as **hard links** (`os.link`, falling back to `shutil.copyfile`) to fill its duration at 30fps, and feed ffmpeg a plain `image2` sequence with `-framerate 30`. No filters. Cumulative rounding keeps the total exact; every frame gets at least one output frame. Results with the bundled ffmpeg: 5.37s vs 5.4s expected on the gap test set (33ms quantization), exactly 1.0s for a single event, exactly 60.0s for the capped set.

Also added: on ffmpeg failure the message includes ffmpeg's last stderr line and the full tail goes to swaglog (`cloudlog.error`). The original failure only logged the bare `RuntimeError`, which is why it needed this investigation. On a device, look in `/data/tmux_logs` (Galaxy stdout/stderr) or swaglog (`Paths.swaglog_root()`, probably `/data/log`, unverified).

## Commits
| Change | `sentry-history-pagination` | `wat-ioniq-tuning` | `wat-bolt-tuning` |
|---|---|---|---|
| Timelapse endpoint + mobile button | `bdce69a59` | `4f27f0a07` | `3f237a013` |
| Kind badge + alarm border | `8e4be1c0a` | `7e479b8b5` | `b9f145a5f` |
| Gap pacing, gap label, timeline bar, MP4 only | `76051085f` | `66c7bb190` | `63dc545a5` |
| Report ffmpeg's error on failure | `1f664711f` | `734318cde` | `ef038f298` |
| Fix for the minimal ffmpeg build | `501509cd3` | `356b40b81` | `3298964e9` |

Worktrees: `starpilot-wat-bolt-analysis` = `sentry-history-pagination`, `starpilot-wat-ioniq-merge` = `wat-ioniq-tuning`, `starpilot-wat-bolt-merge` = `wat-bolt-tuning`.

### Why cherry-pick and not merge
`sentry-history-pagination` also carries unrelated Bolt investigation commits (notes, `tools/tuning` replay scripts) and, more importantly, panda firmware reverts (`9690383be` reverts the CAN/SBU stop-mode wake and GPIOC11 bootkick experiments, `64f88a635` restores stock `panda_*_ignition_only.bin` binaries). A full merge into `wat-ioniq-tuning` was tried, then dropped (reset to `418f87440`, nothing had been pushed) because it changes flashed firmware. The five Sentry commits were cherry-picked instead. `wat-bolt-tuning` had already merged the branch, so only the newer commits were cherry-picked there. `git cherry -v <target> <source>` shows which commits are patch-equivalent already.

## Deploying and retrying
Pull the branch on the device (`wat-ioniq-tuning` at `356b40b81` or later) and **restart Galaxy**, since the route is backend code. Hard-reload the browser for the JS (module imports have no version query strings).

## Test checklist
1. Open Sentry, View history, Make timelapse with each Camera (Road, Driver, Both) and each Pacing. It downloads, plays, and the snackbar shows frame count and seconds.
2. With a date range set, the button says "Timelapse of range" and only those events appear.
3. Bursts of events should flash by (~0.2s each); a multi-day lull should linger (~1s). Timeline bar marker should move left to right.
4. Alarm frames have the red border and ALARM label; selfies (Driver camera only) show SELFIE.
5. Empty range: 404 message. Start the car (onroad): 409 message. Tap the button twice quickly: second gets "already being made".
6. Time a large history (hundreds of events). Encode runs inside the request on the device CPU, so watch for proxy timeouts.
7. Check nothing is left behind: the work dir is a `TemporaryDirectory`, so `/tmp/sentry-timelapse-*` should not accumulate.

## Known gaps and follow-ups
- No unit tests for any of the timelapse code, and the JS has never been syntax-checked here.
- GIF output was built, then dropped at the user's request. Only MP4 exists. There is no `format` param.
- The MP4 is held fully in memory and there is no on-disk cache, so repeated requests re-encode.
- Encode time on the device for a 600-frame history is unmeasured. If it is too slow, options are a lower frame cap, smaller frame width, `-preset ultrafast`, or moving to a background job with polling.
- `_SENTRY_TIMELAPSE_MAX_SECONDS` (60s) and the 600-frame cap are guesses, not tuned on real data.
- Frame timing is quantized to the 30fps output grid (33ms).
- The gap label and bar are baked into the pixels; there is no way to turn them off.
- Desktop UI has no timelapse (or date filter / bulk delete).
- The `fps` param only matters for `timing=even` and is not exposed in the UI.
