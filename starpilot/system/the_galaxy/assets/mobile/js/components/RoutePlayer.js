// Whole-route player: plays a route's qcamera.ts segments as one HLS stream with a single seekable timeline.
// Safari/iOS play the playlist natively; other browsers load the vendored hls.js on first use.

const HLS_MODULE_URL = "/assets/vendor/hls.js/hls.light-1.7.3.min.js"
const HLS_MIME = "application/vnd.apple.mpegurl"

let hlsModule = null

function loadHls() {
  hlsModule ||= import(HLS_MODULE_URL).then((module) => module.default).catch((error) => {
    hlsModule = null
    throw error
  })
  return hlsModule
}

export function routePlaylistUrl(route) {
  return `/route-playback/${encodeURIComponent(route)}/qcamera.m3u8`
}

export const RoutePlayer = {
  name: "RoutePlayer",
  props: {
    route: { type: String, required: true },
  },
  emits: ["error"],
  watch: {
    route() {
      this.attach()
    },
  },
  mounted() {
    this.attach()
  },
  beforeUnmount() {
    this.detach()
  },
  methods: {
    async attach() {
      this.detach()
      const video = this.$refs.video
      const url = routePlaylistUrl(this.route)
      const token = (this._token = (this._token || 0) + 1)

      if (video.canPlayType(HLS_MIME)) {
        this._native = true
        video.src = url
        video.play().catch(() => {})
        return
      }

      let Hls
      try {
        Hls = await loadHls()
      } catch (error) {
        if (token === this._token) this.$emit("error", "Could not load the video player.")
        return
      }
      // Closed or switched route while hls.js was loading.
      if (token !== this._token || !this.$refs.video) return
      if (!Hls.isSupported()) {
        this.$emit("error", "This browser cannot play route video.")
        return
      }

      const hls = new Hls()
      this._hls = hls
      let recoveredMedia = false
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal || hls !== this._hls) return
        if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !recoveredMedia) {
          recoveredMedia = true
          hls.recoverMediaError()
          return
        }
        this.detach()
        this.$emit("error", data.response?.code === 404 ? "No playable video for this route." : "Could not play this route.")
      })
      hls.loadSource(url)
      hls.attachMedia(video)
      video.play().catch(() => {})
    },
    onVideoError() {
      // hls.js reports its own errors; this covers the native (Safari/iOS) path.
      if (this._native) this.$emit("error", "Could not play this route.")
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
