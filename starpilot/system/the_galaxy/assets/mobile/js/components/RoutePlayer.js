// Whole-route player: plays a route as one HLS stream with a single seekable timeline. Low quality is the road
// camera's qcamera.ts; full quality is the raw HEVC of any camera, remuxed per segment to fMP4 on the device.
// Uses the vendored hls.js (loaded on first use) wherever Media Source Extensions exist, so every browser gets the
// same playback and error handling; the browser's native HLS player is only the fallback (e.g. older iPhones).
// The route timeline (engagement and thumbnails) sits under the video and seeks it; a control bar (±10 s, time of day
// and segment, speed, mute, fullscreen) replaces the browser's controls, like connect's player.

import { RouteTimeline, routeClock } from "./RouteTimeline.js"

const HLS_MODULE_URL = "/assets/vendor/hls.js/hls.light-1.7.3.min.js"
const HLS_MIME = "application/vnd.apple.mpegurl"
const HEVC_MIME = 'video/mp4; codecs="hvc1.1.6.L150.B0"'

const SPEEDS = [0.1, 0.25, 0.5, 1, 2, 4, 8]
const SKIP_SECONDS = 10

let hlsModule = null

function installStyle() {
  if (document.getElementById("gx-route-player-style")) return
  const style = document.createElement("style")
  style.id = "gx-route-player-style"
  style.textContent = `
    .gx-route-player:focus {outline:none}
    .gx-route-player .gx-video {cursor:pointer}
    .gx-route-controls {display:flex;align-items:center;gap:2px;margin-top:8px}
    .gx-route-controls button,.gx-route-controls select {display:inline-flex;align-items:center;justify-content:center;gap:1px;min-width:40px;height:40px;padding:0 8px;border:0;border-radius:20px;background:transparent;color:inherit;font:inherit;font-size:.9rem;cursor:pointer;appearance:none;-webkit-appearance:none}
    .gx-route-controls button:hover,.gx-route-controls select:hover {background:var(--surface-container-highest,#363d48)}
    .gx-route-controls button .bi {font-size:1.15rem}
    .gx-route-controls small {font-size:.7rem}
    .gx-route-controls select {text-align:center;text-align-last:center}
    .gx-route-controls select option {background:var(--surface-container-high,#2b313b)}
    .gx-route-controls__time {flex:1;min-width:0;margin:0 6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-variant-numeric:tabular-nums}
    .gx-route-player:fullscreen {display:flex;flex-direction:column;justify-content:center;padding:16px;background:#000;color:#fff}
    .gx-route-player:-webkit-full-screen {display:flex;flex-direction:column;justify-content:center;padding:16px;background:#000;color:#fff}
    .gx-route-player:fullscreen .gx-video {flex:1;min-height:0;max-height:none}
    .gx-route-player:-webkit-full-screen .gx-video {flex:1;min-height:0;max-height:none}
    .gx-scrim--bottomsheet.gx-route-player-scrim {align-items:center;padding:12px}
    .gx-route-player-scrim .gx-sheet.gx-route-player-sheet {max-height:calc(100dvh - 24px);border-radius:var(--radius-xl)}
    @media (max-width:767px) {
      .gx-route-controls button,.gx-route-controls select {min-width:32px;padding:0 4px}
      .gx-route-controls__time {margin:0 2px;font-size:.8rem}
    }
    @media (max-width:399px) {
      .gx-route-controls {gap:0}
      .gx-route-controls small {display:none}
      .gx-route-controls select {width:40px;padding:0}
      .gx-route-controls__time {margin:0}
    }
    @media (min-width:768px) and (min-height:600px) {
      .gx-scrim--bottomsheet.gx-route-player-scrim {padding:3dvh 3vw}
      .gx-route-player-scrim .gx-sheet.gx-route-player-sheet {width:min(1280px,94vw);max-width:none;max-height:94dvh}
      .gx-route-player-sheet .gx-video {max-height:calc(94dvh - 300px)}
    }
  `
  document.head.appendChild(style)
}

function editable(target) {
  return target instanceof Element && !!target.closest("input, select, textarea, [contenteditable]")
}

function loadHls() {
  hlsModule ||= import(HLS_MODULE_URL).then((module) => module.default).catch((error) => {
    hlsModule = null
    throw error
  })
  return hlsModule
}

export function routePlaylistUrl(route, camera = "forward", quality = "low") {
  const playlist = camera === "forward" && quality === "low" ? "qcamera" : camera
  return `/route-playback/${encodeURIComponent(route)}/${encodeURIComponent(playlist)}.m3u8`
}

export const RoutePlayer = {
  name: "RoutePlayer",
  components: { RouteTimeline },
  props: {
    route: { type: String, required: true },
    camera: { type: String, default: "forward" },
    quality: { type: String, default: "low" },
  },
  emits: ["error", "fallback-low"],
  data: () => ({ time: 0, duration: 0, segments: [], startedAt: null, paused: true, muted: true, rate: 1, speeds: SPEEDS }),
  computed: {
    clockText() {
      return routeClock(this.time, this.segments, this.startedAt)
    },
    total() {
      const last = this.segments.at(-1)
      return last ? last.start + last.duration : Number.isFinite(this.duration) ? this.duration : 0
    },
    playlistUrl() {
      return routePlaylistUrl(this.route, this.camera, this.quality)
    },
    fullQuality() {
      return !this.playlistUrl.endsWith("/qcamera.m3u8")
    },
  },
  watch: {
    route() {
      this.attach()
    },
    camera() {
      this.attach(true)
    },
    quality() {
      this.attach(true)
    },
    rate: "applyRate",
  },
  mounted() {
    installStyle()
    this.attach()
  },
  beforeUnmount() {
    this.detach()
  },
  methods: {
    async attach(keepTime = false) {
      // Camera/quality switches continue at the same point of the route, also across a fallback that never played.
      const resumeAt = keepTime ? this._resumeAt || this.$refs.video?.currentTime || 0 : 0
      // Pending until the new source reaches it, so a second quick switch keeps the same point.
      this._resumeAt = resumeAt
      this.detach()
      const video = this.$refs.video
      const url = this.playlistUrl
      const full = this.fullQuality
      const token = (this._token = (this._token || 0) + 1)

      const mediaSource = window.ManagedMediaSource || window.MediaSource
      const mseHevc = !!mediaSource?.isTypeSupported?.(HEVC_MIME)
      if (full && !mseHevc && !(video.canPlayType(HLS_MIME) && video.canPlayType(HEVC_MIME))) {
        // Full quality is HEVC. Without decode support the road camera drops to low quality; other cameras error.
        if (this.camera === "forward") this.$emit("fallback-low")
        else this.$emit("error", "This browser cannot play full-quality video.")
        return
      }
      if (resumeAt > 0) {
        video.addEventListener("loadedmetadata", () => {
          if (token !== this._token) return
          // hls.js already starts there (startPosition); native HLS needs the seek.
          if (this._native) video.currentTime = resumeAt
          this._resumeAt = 0
        }, { once: true })
      }

      let Hls = null
      if (mediaSource && (!full || mseHevc)) {
        try {
          const loaded = await loadHls()
          if (loaded.isSupported()) Hls = loaded
        } catch (error) {
          // Falls back to native playback below.
        }
        // Closed or switched route while hls.js was loading.
        if (token !== this._token || !this.$refs.video) return
      }

      if (!Hls) {
        if (!video.canPlayType(HLS_MIME)) {
          this.$emit("error", "This browser cannot play route video.")
          return
        }
        this._native = true
        video.src = url
        video.play().catch(() => {})
        return
      }

      // Start loading at the resume point instead of fetching (and remuxing) segment 0 first.
      const hls = new Hls({ startPosition: resumeAt > 0 ? resumeAt : -1 })
      this._hls = hls
      let recoveredMedia = false
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal || hls !== this._hls) return
        if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !recoveredMedia) {
          recoveredMedia = true
          hls.recoverMediaError()
          return
        }
        if (data.type === Hls.ErrorTypes.MEDIA_ERROR && this.fallBackToLow()) return
        this.detach()
        this.$emit("error", data.response?.code === 404 ? "No playable video for this route." : "Could not play this route.")
      })
      hls.loadSource(url)
      hls.attachMedia(video)
      video.play().catch(() => {})
    },
    seek(seconds) {
      this.time = seconds
      if (this._resumeAt) {
        // Mid-switch the new source isn't there yet (and would start at the old point): restart it here.
        this._resumeAt = seconds
        this.attach(true)
      } else {
        this.$refs.video.currentTime = seconds
      }
    },
    onTimelineLoaded({ segments, startedAt }) {
      this.segments = segments
      this.startedAt = startedAt
    },
    applyRate() {
      // load() (every source change) resets playbackRate to the default, so both are set; loadedmetadata reapplies.
      const video = this.$refs.video
      if (!video) return
      video.defaultPlaybackRate = this.rate
      video.playbackRate = this.rate
    },
    stepRate(step) {
      const index = SPEEDS.indexOf(this.rate) + step
      this.rate = SPEEDS[Math.min(Math.max(index, 0), SPEEDS.length - 1)]
    },
    skip(seconds) {
      const target = Math.max(this.time + seconds, 0)
      this.seek(this.total ? Math.min(target, this.total) : target)
    },
    togglePlay() {
      const video = this.$refs.video
      if (video.paused) video.play().catch(() => {})
      else video.pause()
    },
    toggleMute() {
      this.$refs.video.muted = !this.$refs.video.muted
    },
    toggleFullscreen() {
      const wrapper = this.$refs.wrapper
      if (document.fullscreenElement || document.webkitFullscreenElement) {
        (document.exitFullscreen || document.webkitExitFullscreen).call(document)?.catch?.(() => {})
      } else if (wrapper.requestFullscreen) {
        wrapper.requestFullscreen().catch(() => {})
      } else if (wrapper.webkitRequestFullscreen) {
        wrapper.webkitRequestFullscreen()
      } else {
        // iPhone: only the video itself goes fullscreen, with the system's controls.
        try {
          this.$refs.video.webkitEnterFullscreen?.()
        } catch (error) {
          // Not before the video has metadata.
        }
      }
    },
    onKey(event) {
      if (event.ctrlKey || event.metaKey || event.altKey || editable(event.target)) return
      // Space on a button (only reachable by keyboard, see onBarMouseDown) presses that button.
      if (event.key === " " && event.target instanceof HTMLButtonElement) return
      const actions = {
        " ": () => this.togglePlay(), k: () => this.togglePlay(), j: () => this.skip(-SKIP_SECONDS), l: () => this.skip(SKIP_SECONDS),
        "<": () => this.stepRate(-1), ">": () => this.stepRate(1), m: () => this.toggleMute(), f: () => this.toggleFullscreen(),
      }
      const action = actions[event.key.length === 1 ? event.key.toLowerCase() : event.key]
      if (!action) return
      event.preventDefault()
      action()
    },
    onBarMouseDown(event) {
      // A clicked button doesn't take focus, so space still plays/pauses instead of pressing it again.
      if (!event.target.closest("button")) return
      event.preventDefault()
      this.$refs.wrapper.focus({ preventScroll: true })
    },
    onTimeUpdate() {
      // During a switch the emptied video reports 0; the timeline keeps showing the resume point.
      if (!this._resumeAt) this.time = this.$refs.video.currentTime
    },
    onVideoError() {
      // hls.js reports its own errors; this covers the native fallback.
      if (this._native && !this.fallBackToLow()) this.$emit("error", "Could not play this route.")
    },
    fallBackToLow() {
      // Road camera full quality that fails to decode: drop to low quality at the same point, not to segments
      // (the single-segment player serves the same HEVC).
      if (this.camera !== "forward" || !this.fullQuality) return false
      this._resumeAt = this._resumeAt || this.$refs.video?.currentTime || 0
      this.detach()
      this.$emit("fallback-low")
      return true
    },
    detach() {
      this._token = (this._token || 0) + 1
      this._native = false
      this._hls?.destroy()
      this._hls = null
      const video = this.$refs.video
      if (video) {
        video.pause()
        video.removeAttribute("src")
        video.load()
      }
    },
  },
  template: `
    <div ref="wrapper" class="gx-route-player" tabindex="-1" @keydown="onKey">
      <video ref="video" class="gx-video" muted playsinline preload="metadata" @click="togglePlay" @error="onVideoError" @timeupdate="onTimeUpdate"
        @loadedmetadata="applyRate" @durationchange="duration = $refs.video.duration" @ratechange="rate = $refs.video.playbackRate" @play="paused = false" @pause="paused = true" @volumechange="muted = $refs.video.muted"></video>
      <div class="gx-route-controls" @mousedown="onBarMouseDown">
        <button type="button" aria-label="Back 10 seconds" title="Back 10 s (J)" @click="skip(-10)"><i class="bi bi-arrow-counterclockwise" aria-hidden="true"></i><small>10</small></button>
        <button type="button" :aria-label="paused ? 'Play' : 'Pause'" :title="(paused ? 'Play' : 'Pause') + ' (space)'" @click="togglePlay"><i class="bi" :class="paused ? 'bi-play-fill' : 'bi-pause-fill'" aria-hidden="true"></i></button>
        <button type="button" aria-label="Forward 10 seconds" title="Forward 10 s (L)" @click="skip(10)"><i class="bi bi-arrow-clockwise" aria-hidden="true"></i><small>10</small></button>
        <span class="gx-route-controls__time" aria-live="off">{{ clockText }}</span>
        <select v-model.number="rate" aria-label="Playback speed" title="Playback speed (< >)">
          <option v-for="s in speeds" :key="s" :value="s">{{ s }}×</option>
        </select>
        <button type="button" :aria-label="muted ? 'Unmute' : 'Mute'" :title="(muted ? 'Unmute' : 'Mute') + ' (M)'" @click="toggleMute"><i class="bi" :class="muted ? 'bi-volume-mute-fill' : 'bi-volume-up-fill'" aria-hidden="true"></i></button>
        <button type="button" aria-label="Fullscreen" title="Fullscreen (F)" @click="toggleFullscreen"><i class="bi bi-fullscreen" aria-hidden="true"></i></button>
      </div>
      <RouteTimeline :route="route" :camera="camera" :quality="quality" :time="time" @seek="seek" @loaded="onTimelineLoaded" />
    </div>`,
}
