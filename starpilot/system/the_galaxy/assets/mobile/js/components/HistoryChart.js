// Time-series chart for the device history: one y axis in one unit, optional min-max band, shaded spans
// (e.g. time driving), dashed reference lines and a crosshair tooltip. Lines break across gaps longer than gapS
// instead of interpolating over time the device was off. Different units go on separate charts, never two axes.

const PAD = { l: 44, r: 16, t: 14, b: 28 }
const DAY = 86400
const END_LABEL_MIN_W = 640   // narrower charts rely on the legend alone
const END_LABEL_GUTTER = 120  // right gutter the end labels sit in, clear of the data
const REF_LABEL_MIN_GAP = 14  // reference lines closer than this get their labels on opposite sides

// decimals: values; tickDecimals: axis and reference labels; pad: added around the data; minRange: the smallest
// y span shown; fallback: the axis with no data; min/max: hard limits of the unit
export const UNITS = {
  V: { suffix: " V", decimals: 2, tickDecimals: 1, pad: 0.1, minRange: 0.6, fallback: [11.5, 13] },
  "°C": { suffix: " °C", decimals: 1, tickDecimals: 0, pad: 2, minRange: 10, fallback: [20, 80] },
  "%": { suffix: "%", decimals: 0, tickDecimals: 0, pad: 5, minRange: 20, fallback: [0, 100], min: 0, max: 100 },
  h: { suffix: " h", decimals: 1, tickDecimals: 1, pad: 0, minRange: 1, fallback: [0, 1], min: 0 },
}
const STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 25, 50, 100]

export function fmtValue(v, unit = "V", decimals = null) {
  if (v == null) return "—"
  const u = UNITS[unit]
  return `${Number(v).toFixed(decimals ?? u.decimals)}${u.suffix}`
}

const fmtTime = (t, span) => new Date(t * 1000).toLocaleString("en-US", span > 2 * DAY
  ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
  : { weekday: "short", hour: "numeric", minute: "2-digit" })

// at most ~6 ticks
const niceStep = (range) => STEPS.find((s) => range / s <= 6) ?? STEPS[STEPS.length - 1]

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

export const HistoryChart = {
  name: "HistoryChart",
  props: {
    // [{ key, label, color (CSS var), points: [{ t, v, lo?, hi?, note? }], band: bool, rangeLabel? }]
    series: { type: Array, required: true },
    t0: { type: Number, required: true },
    t1: { type: Number, required: true },
    unit: { type: String, default: "V" },
    refLines: { type: Array, default: () => [] },   // [{ value, label }]
    spans: { type: Array, default: () => [] },      // [{ t0, t1 }]
    spanLabel: { type: String, default: "" },
    gapS: { type: Number, default: 0 },
    height: { type: Number, default: 220 },
    ariaLabel: { type: String, default: "History over time" },
    // reserve the end-label gutter even without end labels, so stacked charts share an x axis
    gutter: { type: Boolean, default: false },
    emptyText: { type: String, default: "No data in this range yet." },
  },
  data() { return { width: 600, hover: null } },
  mounted() {
    this.observer = new ResizeObserver(([entry]) => { this.width = Math.max(260, Math.round(entry.contentRect.width)) })
    this.observer.observe(this.$el)
  },
  beforeUnmount() { this.observer?.disconnect() },
  computed: {
    u() { return UNITS[this.unit] || UNITS.V },
    endLabels() { return this.series.length > 1 && this.width >= END_LABEL_MIN_W },
    padR() { return this.endLabels || (this.gutter && this.width >= END_LABEL_MIN_W) ? END_LABEL_GUTTER : PAD.r },
    plotR() { return this.width - this.padR },
    plotW() { return this.plotR - PAD.l },
    plotH() { return this.height - PAD.t - PAD.b },
    refs() { return this.refLines.filter((r) => Number.isFinite(r?.value)) },
    empty() { return !this.series.some((s) => s.points.length) },
    // the tick step is picked once, from the data range; recomputing it from the rounded domain could pick another
    yAxis() {
      const u = this.u
      const values = []
      for (const s of this.series) for (const p of s.points) values.push(p.lo ?? p.v, p.hi ?? p.v)
      for (const r of this.refs) values.push(r.value)
      const finite = values.filter((v) => Number.isFinite(v))
      let [lo, hi] = u.fallback
      if (finite.length) {
        lo = Math.min(...finite) - u.pad
        hi = Math.max(...finite) + u.pad
        if (hi - lo < u.minRange) { const mid = (hi + lo) / 2; lo = mid - u.minRange / 2; hi = mid + u.minRange / 2 }
      }
      if (u.min != null && lo < u.min) { hi += u.min - lo; lo = u.min }
      if (u.max != null && hi > u.max) { lo = Math.max(u.min ?? -Infinity, lo - (hi - u.max)); hi = u.max }
      const step = niceStep(hi - lo)
      const round = (v) => Math.round(v * 100) / 100
      return { lo: round(Math.floor(lo / step) * step), hi: round(Math.ceil(hi / step) * step), step }
    },
    yDomain() { return [this.yAxis.lo, this.yAxis.hi] },
    yTicks() {
      const { lo, hi, step } = this.yAxis
      const decimals = step < 1 ? Math.max(this.u.tickDecimals, 1) : this.u.tickDecimals
      const ticks = []
      for (let v = lo; v <= hi + 1e-9; v += step) ticks.push(Math.round(v * 100) / 100)
      return ticks.map((v) => ({ v, y: this.y(v), label: v.toFixed(decimals) }))
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
    refsDrawn() {
      // labels sit above their line; a line too close below the previous one gets its label underneath instead
      const drawn = this.refs.map((r) => ({ ...r, y: this.y(r.value), text: `${r.label} ${fmtValue(r.value, this.unit, this.u.tickDecimals)}` }))
        .sort((a, b) => a.y - b.y)
      return drawn.map((r, i) => ({ ...r, labelY: i && r.y - drawn[i - 1].y < REF_LABEL_MIN_GAP ? r.y + 14 : r.y - 5 }))
    },
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
        const range = s.band ? `${s.rangeLabel || "range"} ${fmtValue(p.lo, this.unit)} – ${fmtValue(p.hi, this.unit)}` : ""
        rows.push({ key: s.key, label: s.label, color: s.color, value: fmtValue(p.v, this.unit), range, note: p.note || "" })
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
          <g class="gx-vchart__ref">
            <line v-for="r in refsDrawn" :key="'r' + r.value" :x1="${PAD.l}" :x2="plotR" :y1="r.y" :y2="r.y" />
          </g>
          <g v-for="s in drawn" :key="s.key" :style="{ color: s.color }">
            <path v-if="s.bandPath" :d="s.bandPath" class="gx-vchart__band" />
            <path v-if="s.line" :d="s.line" class="gx-vchart__line" />
            <circle v-for="(d, i) in s.dotsXY" :key="i" :cx="d.x" :cy="d.y" r="4" class="gx-vchart__dot" />
          </g>
          <!-- labels over the data, on their surface-coloured halo, so a line crossing one can't hide it -->
          <g class="gx-vchart__ref">
            <text v-for="r in refsDrawn" :key="'rl' + r.value" :x="plotR" :y="r.labelY" text-anchor="end">{{ r.text }}</text>
          </g>
          <text v-for="s in drawn.filter((d) => d.endLabel)" :key="'e' + s.key" :x="s.endLabel.x" :y="s.endLabel.y" class="gx-vchart__end">{{ s.label }}</text>
          <line v-if="hover" :x1="hover.x" :x2="hover.x" :y1="${PAD.t}" :y2="${PAD.t} + plotH" class="gx-vchart__crosshair" />
        </svg>
        <div v-if="empty" class="gx-vchart__empty">{{ emptyText }}</div>
        <div v-if="tooltip" class="gx-vchart__tip" :style="{ left: tooltip.left + 'px' }" role="status">
          <strong>{{ tooltip.title }}</strong>
          <div v-for="row in tooltip.rows" :key="row.key">
            <i :style="{ background: row.color }"></i>{{ row.label }} <b>{{ row.value }}</b>
            <small v-if="row.range">{{ row.range }}</small>
            <small v-if="row.note">{{ row.note }}</small>
          </div>
        </div>
      </div>
    </div>
  `,
}
