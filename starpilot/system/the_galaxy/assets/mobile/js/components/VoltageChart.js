// Time-series voltage chart: one shared y axis (volts), optional min-max band, shaded spans
// (e.g. time driving), a dashed reference line and a crosshair tooltip. Lines break across gaps
// longer than gapS instead of interpolating over time the device was off.

const PAD = { l: 44, r: 16, t: 14, b: 28 }
const DAY = 86400
const END_LABEL_MIN_W = 640   // narrower charts rely on the legend alone
const END_LABEL_GUTTER = 120  // right gutter the end labels sit in, clear of the data

const fmtV = (v) => (v == null ? "—" : `${Number(v).toFixed(2)} V`)
const fmtTime = (t, span) => new Date(t * 1000).toLocaleString("en-US", span > 2 * DAY
  ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
  : { weekday: "short", hour: "numeric", minute: "2-digit" })

function niceStep(range) {
  if (range <= 1.2) return 0.2
  if (range <= 3) return 0.5
  return 1
}

function segments(points, gapS) {
  const out = []
  let current = []
  for (const p of points) {
    if (current.length && gapS && p.t - current[current.length - 1].t > gapS) {
      out.push(current)
      current = []
    }
    current.push(p)
  }
  if (current.length) out.push(current)
  return out
}

export const VoltageChart = {
  name: "VoltageChart",
  props: {
    // [{ key, label, color (CSS var), points: [{ t, v, lo?, hi?, note? }], band: bool }]
    series: { type: Array, required: true },
    t0: { type: Number, required: true },
    t1: { type: Number, required: true },
    cutoff: { type: Number, default: null },
    cutoffLabel: { type: String, default: "Shutdown cutoff" },
    spans: { type: Array, default: () => [] },      // [{ t0, t1 }]
    spanLabel: { type: String, default: "" },
    gapS: { type: Number, default: 0 },
    height: { type: Number, default: 220 },
    ariaLabel: { type: String, default: "Voltage over time" },
    // reserve the end-label gutter even without end labels, so stacked charts share an x axis
    gutter: { type: Boolean, default: false },
  },
  data() { return { width: 600, hover: null } },
  mounted() {
    this.observer = new ResizeObserver(([entry]) => { this.width = Math.max(260, Math.round(entry.contentRect.width)) })
    this.observer.observe(this.$el)
  },
  beforeUnmount() { this.observer?.disconnect() },
  computed: {
    endLabels() { return this.series.length > 1 && this.width >= END_LABEL_MIN_W },
    padR() { return this.endLabels || (this.gutter && this.width >= END_LABEL_MIN_W) ? END_LABEL_GUTTER : PAD.r },
    plotR() { return this.width - this.padR },
    plotW() { return this.plotR - PAD.l },
    plotH() { return this.height - PAD.t - PAD.b },
    empty() { return !this.series.some((s) => s.points.length) },
    // the tick step is picked once, from the data range; recomputing it from the rounded domain could pick another
    yAxis() {
      const values = []
      for (const s of this.series) for (const p of s.points) values.push(p.lo ?? p.v, p.hi ?? p.v)
      if (this.cutoff != null) values.push(this.cutoff)
      const finite = values.filter((v) => Number.isFinite(v))
      if (!finite.length) return { lo: 11.5, hi: 13, step: 0.5 }
      let lo = Math.min(...finite) - 0.1
      let hi = Math.max(...finite) + 0.1
      if (hi - lo < 0.6) { const mid = (hi + lo) / 2; lo = mid - 0.3; hi = mid + 0.3 }
      const step = niceStep(hi - lo)
      const round = (v) => Math.round(v * 100) / 100
      return { lo: round(Math.floor(lo / step) * step), hi: round(Math.ceil(hi / step) * step), step }
    },
    yDomain() { return [this.yAxis.lo, this.yAxis.hi] },
    yTicks() {
      const { lo, hi, step } = this.yAxis
      const ticks = []
      for (let v = lo; v <= hi + 1e-9; v += step) ticks.push(Math.round(v * 100) / 100)
      return ticks.map((v) => ({ v, y: this.y(v), label: v.toFixed(1) }))
    },
    xTicks() {
      const span = this.t1 - this.t0
      const ticks = []
      if (span <= 2 * DAY) {
        const stepH = span <= 12 * 3600 ? 2 : span <= DAY ? 4 : 8
        const d = new Date(this.t0 * 1000); d.setMinutes(0, 0, 0)
        d.setHours(Math.ceil(d.getHours() / stepH) * stepH)
        for (let t = d.getTime() / 1000; t <= this.t1; t += stepH * 3600) {
          ticks.push({ t, label: new Date(t * 1000).toLocaleTimeString("en-US", { hour: "numeric" }) })
        }
      } else {
        const stepD = Math.max(1, Math.ceil(span / DAY / Math.max(3, Math.floor(this.plotW / 80))))
        const d = new Date(this.t0 * 1000); d.setHours(24, 0, 0, 0)
        for (let t = d.getTime() / 1000; t <= this.t1; t += stepD * DAY) {
          ticks.push({ t, label: new Date(t * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" }) })
        }
      }
      // the hourly start is t0's hour rounded, which can fall before t0 and over the y labels
      return ticks.filter((tick) => tick.t >= this.t0).map((tick) => ({ ...tick, x: this.x(tick.t) }))
    },
    drawn() {
      const drawn = this.series.map((s) => {
        const segs = segments(s.points, this.gapS)
        const line = segs.map((seg) => seg.map((p, i) => `${i ? "L" : "M"}${this.x(p.t).toFixed(1)},${this.y(p.v).toFixed(1)}`).join("")).join("")
        const band = s.band ? segs.map((seg) => {
          const top = seg.map((p, i) => `${i ? "L" : "M"}${this.x(p.t).toFixed(1)},${this.y(p.hi).toFixed(1)}`).join("")
          const bottom = seg.slice().reverse().map((p) => `L${this.x(p.t).toFixed(1)},${this.y(p.lo).toFixed(1)}`).join("")
          return `${top}${bottom}Z`
        }).join("") : ""
        const last = s.points[s.points.length - 1]
        // a lone point between gaps has no line to sit on; mark it so it isn't lost
        const lone = segs.filter((seg) => seg.length === 1).map(([p]) => p)
        return {
          ...s,
          line,
          bandPath: band,
          dotsXY: lone.map((p) => ({ x: this.x(p.t), y: this.y(p.v) })),
          endLabel: last && this.endLabels ? { x: this.plotR + 10, y: this.y(last.v) + 4 } : null,
        }
      })
      // keep end labels at least a line apart, in their series' vertical order
      const labelled = drawn.filter((s) => s.endLabel).sort((a, b) => a.endLabel.y - b.endLabel.y)
      for (let i = 1; i < labelled.length; i++) {
        const prev = labelled[i - 1].endLabel
        labelled[i].endLabel.y = Math.max(labelled[i].endLabel.y, prev.y + 14)
      }
      return drawn
    },
    spanRects() {
      // drop spans outside the range before the 1 px minimum, or they draw as slivers at the edge
      return this.spans.filter((s) => s.t1 > this.t0 && s.t0 < this.t1).map((s) => {
        const x0 = this.x(Math.max(s.t0, this.t0))
        const x1 = this.x(Math.min(s.t1, this.t1))
        return { x: x0, w: Math.max(1, x1 - x0) }
      })
    },
    cutoffY() { return this.cutoff == null ? null : this.y(this.cutoff) },
    tooltip() {
      if (!this.hover) return null
      const span = this.t1 - this.t0
      const left = Math.min(this.hover.x + 12, this.width - 180)
      return { left: Math.max(4, left), title: fmtTime(this.hover.t, span), rows: this.hover.rows }
    },
  },
  methods: {
    x(t) { return PAD.l + ((t - this.t0) / Math.max(1, this.t1 - this.t0)) * this.plotW },
    y(v) { const [lo, hi] = this.yDomain; return PAD.t + (1 - (v - lo) / (hi - lo)) * this.plotH },
    onMove(event) {
      const rect = event.currentTarget.getBoundingClientRect()
      const px = event.clientX - rect.left
      const t = this.t0 + ((px - PAD.l) / this.plotW) * (this.t1 - this.t0)
      let best = null
      for (const s of this.series) {
        for (const p of s.points) {
          const d = Math.abs(p.t - t)
          if (!best || d < best.d) best = { d, t: p.t }
        }
      }
      if (!best || Math.abs(this.x(best.t) - px) > 40) { this.hover = null; return }
      const rows = []
      for (const s of this.series) {
        const p = s.points.find((q) => q.t === best.t)
        if (!p) continue
        rows.push({ key: s.key, label: s.label, color: s.color, value: fmtV(p.v), range: s.band ? `${fmtV(p.lo)} – ${fmtV(p.hi)}` : "", note: p.note || "" })
      }
      this.hover = { x: this.x(best.t), t: best.t, rows }
    },
  },
  template: `
    <div class="gx-vchart">
      <div v-if="series.length > 1 || spanLabel" class="gx-vchart__legend">
        <span v-for="s in series.length > 1 ? series : []" :key="s.key"><i :style="{ background: s.color }"></i>{{ s.label }}</span>
        <span v-if="spanLabel"><i class="gx-vchart__span-swatch"></i>{{ spanLabel }}</span>
      </div>
      <div class="gx-vchart__plot">
        <svg :width="width" :height="height" role="img" :aria-label="ariaLabel" @pointermove="onMove" @pointerleave="hover = null">
          <rect v-for="(r, i) in spanRects" :key="'s' + i" :x="r.x" :y="${PAD.t}" :width="r.w" :height="plotH" class="gx-vchart__span" />
          <g class="gx-vchart__grid">
            <line v-for="tick in yTicks" :key="'y' + tick.v" :x1="${PAD.l}" :x2="plotR" :y1="tick.y" :y2="tick.y" />
          </g>
          <g class="gx-vchart__axis">
            <text v-for="tick in yTicks" :key="'yl' + tick.v" :x="${PAD.l - 6}" :y="tick.y + 4" text-anchor="end">{{ tick.label }}</text>
            <text v-for="tick in xTicks" :key="'xl' + tick.t" :x="tick.x" :y="height - 8" text-anchor="middle">{{ tick.label }}</text>
          </g>
          <g v-if="cutoffY != null" class="gx-vchart__ref">
            <line :x1="${PAD.l}" :x2="plotR" :y1="cutoffY" :y2="cutoffY" />
            <text :x="plotR" :y="cutoffY - 5" text-anchor="end">{{ cutoffLabel }} {{ cutoff.toFixed(1) }} V</text>
          </g>
          <g v-for="s in drawn" :key="s.key" :style="{ color: s.color }">
            <path v-if="s.bandPath" :d="s.bandPath" class="gx-vchart__band" />
            <path v-if="s.line" :d="s.line" class="gx-vchart__line" />
            <circle v-for="(d, i) in s.dotsXY" :key="i" :cx="d.x" :cy="d.y" r="4" class="gx-vchart__dot" />
          </g>
          <text v-for="s in drawn.filter((d) => d.endLabel)" :key="'e' + s.key" :x="s.endLabel.x" :y="s.endLabel.y" class="gx-vchart__end">{{ s.label }}</text>
          <line v-if="hover" :x1="hover.x" :x2="hover.x" :y1="${PAD.t}" :y2="${PAD.t} + plotH" class="gx-vchart__crosshair" />
        </svg>
        <div v-if="empty" class="gx-vchart__empty">No data in this range yet.</div>
        <div v-if="tooltip" class="gx-vchart__tip" :style="{ left: tooltip.left + 'px' }" role="status">
          <strong>{{ tooltip.title }}</strong>
          <div v-for="row in tooltip.rows" :key="row.key">
            <i :style="{ background: row.color }"></i>{{ row.label }} <b>{{ row.value }}</b>
            <small v-if="row.range">range {{ row.range }}</small>
            <small v-if="row.note">{{ row.note }}</small>
          </div>
        </div>
      </div>
    </div>
  `,
}
