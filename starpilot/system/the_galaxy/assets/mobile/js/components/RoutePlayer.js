// Whole-route player: plays a route as one HLS stream with a single seekable timeline. Low quality is the road
// camera's qcamera.ts; full quality is the raw HEVC of any camera, remuxed per segment to fMP4 on the device.
// Uses the vendored hls.js (loaded on first use) wherever Media Source Extensions exist, so every browser gets the
// same playback and error handling; the browser's native HLS player is only the fallback (e.g. older iPhones).

const HLS_MODULE_URL = "/assets/vendor/hls.js/hls.light-1.7.3.min.js"
const HLS_MIME = "application/vnd.apple.mpegurl"
const HEVC_MIME = 'video/mp4; codecs="hvc1.1.6.L150.B0"'

let hlsModule = null

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
  props: {
    route: { type: String, required: true },
    camera: { type: String, default: "forward" },
    quality: { type: String, default: "low" },
  },
  emits: ["error", "fallback-low"],
  computed: {
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
  },
  mounted() {
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
  template: `<video ref="video" class="gx-video" controls muted playsinline preload="metadata" @error="onVideoError"></video>`,
}
