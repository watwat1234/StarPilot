// Route timeline under the whole-route video: an engagement bar and one road camera thumbnail per minute, both from
// each segment's qlog (parsed on the device, never started onroad). Drag or click along it to seek; arrows step 10 s.
// A mouse hovering over it shows the time there.

const BUSY_RETRY_MS = 3000
const ONROAD_RETRY_MS = 60000
const KEY_STEP_SECONDS = 10
const SEGMENT_SECONDS = 60
const STATE_COLORS = { engaged: "#178644", overriding: "#919b95", disengaged: "#173349" }
const ALERT_COLORS = [null, "#da6f25", "#c92231"]

function installStyle() {
  if (document.getElementById("gx-route-timeline-style")) return
  const style = document.createElement("style")
  style.id = "gx-route-timeline-style"
  style.textContent = `
    .gx-route-timeline {padding-top:12px;user-select:none;-webkit-user-select:none}
    .gx-route-timeline__track {position:relative;cursor:pointer;touch-action:pan-y;border-radius:6px;outline-offset:3px}
    .gx-route-timeline__track:focus:not(:focus-visible) {outline:none}
    .gx-route-timeline__bar,.gx-route-timeline__film {position:relative;overflow:hidden;background:var(--surface-container-high,#2b313b)}
    .gx-route-timeline__bar {height:12px;border-radius:6px 6px 0 0}
    .gx-route-timeline__film {height:32px;border-radius:0 0 6px 6px}
    .gx-route-timeline__bar span,.gx-route-timeline__film span {position:absolute;top:0;bottom:0}
    .gx-route-timeline__film span {box-sizing:border-box;border-left:1px solid #0008;overflow:hidden}
    .gx-route-timeline__film img {display:block;width:100%;height:100%;object-fit:cover;pointer-events:none}
    .gx-route-timeline__playhead,.gx-route-timeline__hover {position:absolute;z-index:2;top:-3px;bottom:-3px;width:2px;margin-left:-1px;background:#fff;box-shadow:0 0 3px #000;pointer-events:none}
    .gx-route-timeline__hover {z-index:3;background:#fff9;box-shadow:none}
    .gx-route-timeline__label {position:absolute;bottom:100%;left:0;margin-bottom:4px;padding:1px 6px;border-radius:4px;background:#000c;color:#fff;font-size:.75rem;white-space:nowrap;font-variant-numeric:tabular-nums}
    .gx-route-timeline__note {margin-top:6px;font-size:.8rem;color:var(--on-surface-variant,#aab)}
  `
  document.head.appendChild(style)
}

export function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds))
  const minutes = Math.floor(s / 60) % 60
  const rest = String(s % 60).padStart(2, "0")
  return s >= 3600 ? `${Math.floor(s / 3600)}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`
}

// "16:59:34 – 12": time of day (24 h) and segment number at `seconds` into the playlist. Segment numbers count
// minutes from the route start, so gaps (aged-out segments) still give the right time. Without a start time the
// time is since the route start.
export function routeClock(seconds, segments, startedAt) {
  const segment = segments.findLast((s) => s.start <= seconds) || segments[0]
  if (!segment) return clock(seconds)
  const number = Number(segment.segment.split("--").pop())
  const sinceStart = number * SEGMENT_SECONDS + Math.max(0, seconds - segment.start)
  const time = startedAt == null ? clock(sinceStart)
    : new Date((startedAt + sinceStart) * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" })
  return `${time} – ${number}`
}

const segmentUrl = (segment, part) => `/route-playback/segment/${encodeURIComponent(segment)}/${part}`

export const RouteTimeline = {
  name: "RouteTimeline",
  props: {
    route: { type: String, required: true },
    camera: { type: String, default: "forward" },
    quality: { type: String, default: "low" },
    time: { type: Number, default: 0 },
  },
  emits: ["seek", "loaded"],
  data: () => ({ segments: [], startedAt: null, info: {}, onroad: false, dragTime: null, hoverTime: null }),
  computed: {
    total() {
      const last = this.segments.at(-1)
      return last ? last.start + last.duration : 0
    },
    shownTime() {
      return Math.min(this.dragTime ?? this.time, this.total)
    },
    fraction() {
      return this.total ? this.shownTime / this.total : 0
    },
    hoverFraction() {
      return this.total ? Math.min(this.hoverTime, this.total) / this.total : 0
    },
    bars() {
      const bars = []
      for (const { segment, start, duration } of this.segments) {
        for (const [from, to, state, alert] of this.info[segment]?.spans || []) {
          bars.push({ key: `${segment}/${from}`, color: ALERT_COLORS[alert] || STATE_COLORS[state], style: this.place(start + from, Math.min(to, duration) - from) })
        }
      }
      return bars
    },
    tiles() {
      return this.segments.map(({ segment, start, duration }) => ({
        key: segment,
        style: this.place(start, duration),
        src: this.info[segment]?.thumbnailAt != null ? segmentUrl(segment, "thumbnail.jpg") : null,
      }))
    },
  },
  watch: {
    route() {
      this.reset()
      this.load()
    },
    camera: "load",
    quality: "load",
  },
  mounted() {
    installStyle()
    this.reset()
    this.load()
  },
  beforeUnmount() {
    this._token++
  },
  methods: {
    label(seconds) {
      return routeClock(seconds, this.segments, this.startedAt)
    },
    place(start, duration) {
      return { left: `${(start / this.total) * 100}%`, width: `${(Math.max(0, duration) / this.total) * 100}%` }
    },
    reset() {
      // Stops the fetch loop of the previous route; segment data is per segment, so it survives camera/quality changes.
      this._token = (this._token || 0) + 1
      this.segments = []
      this.startedAt = null
      this.info = {}
      this.onroad = false
      this.dragTime = null
      this.hoverTime = null
    },
    async load() {
      // Only the segment list (offsets match the playlist that is playing) depends on camera and quality.
      const token = this._token
      const list = (this._list = (this._list || 0) + 1)
      const query = new URLSearchParams({ camera: this.camera, quality: this.quality })
      let segments = [], startedAt = null
      try {
        const response = await fetch(`/route-playback/${encodeURIComponent(this.route)}/timeline.json?${query}`)
        if (response.ok) ({ segments, startedAt = null } = await response.json())
      } catch (error) {
        // No timeline; the video still plays.
      }
      if (token !== this._token || list !== this._list) return
      this.segments = segments
      this.startedAt = startedAt
      this.$emit("loaded", { segments, startedAt })
      // Minutes the device already parsed come with the list; only the rest are fetched one by one.
      for (const { segment, timeline } of segments) {
        if (timeline && !this.info[segment]) this.info[segment] = timeline
      }
      this.fetchSegments(token)
    },
    async fetchSegments(token) {
      // One request at a time (each may parse a qlog on the device), from the minute in view outward.
      if (this._fetching === token) return
      this._fetching = token
      try {
        while (token === this._token) {
          const pending = this.segments.map((s, index) => [s, index]).filter(([s]) => !(s.segment in this.info))
          if (!pending.length) return
          const playing = this.segments.findIndex((s) => s.start + s.duration > this.time)
          const near = (index) => Math.abs(index - (playing < 0 ? this.segments.length - 1 : playing))
          const [{ segment }] = pending.reduce((best, entry) => (near(entry[1]) < near(best[1]) ? entry : best))
          let response = null, body = null
          try {
            response = await fetch(segmentUrl(segment, "timeline.json"))
            body = await response.json()
          } catch (error) {
            // No response (connection dropped): retried below. A bad body is shown as an empty minute.
          }
          if (token !== this._token) return
          if (!response || response.status === 503) {
            // Uncached while driving: check again now and then until offroad. Busy or unreachable: shortly.
            this.onroad = body?.reason === "onroad"
            await new Promise((resolve) => setTimeout(resolve, this.onroad ? ONROAD_RETRY_MS : BUSY_RETRY_MS))
            continue
          }
          this.onroad = false
          this.info[segment] = response.ok ? body : null
        }
      } finally {
        if (this._fetching === token) this._fetching = null
      }
    },
    timeAt(event) {
      const rect = this.$refs.track.getBoundingClientRect()
      return Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1) * this.total
    },
    onPointerDown(event) {
      if (!this.total || event.button > 0 || this.dragTime !== null) return
      // Only the pointer that started the drag moves it (a second finger is ignored).
      this._pointer = event.pointerId
      this.$refs.track.setPointerCapture?.(event.pointerId)
      this.dragTime = this.timeAt(event)
    },
    onPointerMove(event) {
      if (this.dragTime !== null && event.pointerId === this._pointer) this.dragTime = this.timeAt(event)
      // Touch has no hover: a finger only shows the time while dragging.
      else if (this.dragTime === null && event.pointerType === "mouse" && this.total) this.hoverTime = this.timeAt(event)
    },
    onPointerUp(event) {
      if (this.dragTime === null || event.pointerId !== this._pointer) return
      this.dragTime = null
      this.hoverTime = event.pointerType === "mouse" ? this.timeAt(event) : null
      this.$emit("seek", this.timeAt(event))
    },
    onPointerCancel(event) {
      if (event.pointerId === this._pointer) this.dragTime = null
    },
    onKey(event) {
      const step = { ArrowLeft: -KEY_STEP_SECONDS, ArrowDown: -KEY_STEP_SECONDS, ArrowRight: KEY_STEP_SECONDS, ArrowUp: KEY_STEP_SECONDS }[event.key]
      const target = event.key === "Home" ? 0 : event.key === "End" ? this.total : step ? this.time + step : null
      if (target === null || !this.total) return
      event.preventDefault()
      this.$emit("seek", Math.min(Math.max(target, 0), this.total))
    },
  },
  template: `
    <div v-if="total" class="gx-route-timeline">
      <div ref="track" class="gx-route-timeline__track" role="slider" tabindex="0" aria-label="Route position"
        aria-valuemin="0" :aria-valuemax="Math.round(total)" :aria-valuenow="Math.round(shownTime)" :aria-valuetext="label(shownTime)"
        @pointerdown="onPointerDown" @pointermove="onPointerMove" @pointerup="onPointerUp" @pointercancel="onPointerCancel"
        @pointerleave="hoverTime = null" @keydown="onKey">
        <div class="gx-route-timeline__bar"><span v-for="b in bars" :key="b.key" :style="{ ...b.style, background: b.color }"></span></div>
        <div class="gx-route-timeline__film"><span v-for="t in tiles" :key="t.key" :style="t.style"><img v-if="t.src" :src="t.src" alt="" draggable="false"></span></div>
        <div class="gx-route-timeline__playhead" :style="{ left: fraction * 100 + '%' }">
          <span v-if="dragTime !== null" class="gx-route-timeline__label" :style="{ transform: 'translateX(-' + fraction * 100 + '%)' }">{{ label(dragTime) }}</span>
        </div>
        <div v-if="hoverTime !== null && dragTime === null" class="gx-route-timeline__hover" :style="{ left: hoverFraction * 100 + '%' }">
          <span class="gx-route-timeline__label" :style="{ transform: 'translateX(-' + hoverFraction * 100 + '%)' }">{{ label(hoverTime) }}</span>
        </div>
      </div>
      <div v-if="onroad" class="gx-route-timeline__note">Timeline fills in after the drive</div>
    </div>`,
}
