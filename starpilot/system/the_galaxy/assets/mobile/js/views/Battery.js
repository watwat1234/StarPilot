import { api, showSnackbar } from "../api.js"
import { GalaxyTabs } from "../components/GalaxyTabs.js"
import { VoltageChart } from "../components/VoltageChart.js"

const DAY = 86400
const TABS = { trend: "Trend", detail: "Detail" }
const TREND_RANGES = [30, 90, 365]
const DETAIL_RANGES = [1, 7, 30]
const MIN_DRIVE_S = 300
const PARK_ROWS = 50
const SAMPLE_ROWS = 200

const END_REASONS = {
  ignition: "Drove",
  offroad_timeout: "Shutdown timer",
  low_voltage: "Low-voltage cutoff",
  battery_capacity_exhausted: "Capacity limit",
  forced_power_down: "Power-down",
  unknown: "Power lost",
}

// Rough 12V bands; volts only, no state-of-charge claims (EV DC-DC charging skews those tables).
export function batteryStatus(voltage, onroad) {
  if (voltage == null) return null
  if (onroad) {
    if (voltage >= 13.2) return { level: "good", icon: "bi-lightning-charge-fill", label: "Charging" }
    if (voltage >= 12.4) return { level: "warning", icon: "bi-exclamation-triangle-fill", label: "Not charging" }
    return { level: "critical", icon: "bi-x-octagon-fill", label: "Low" }
  }
  if (voltage >= 12.4) return { level: "good", icon: "bi-check-circle-fill", label: "Normal" }
  if (voltage >= 12.0) return { level: "warning", icon: "bi-exclamation-triangle-fill", label: "Low" }
  return { level: "critical", icon: "bi-x-octagon-fill", label: "Very low" }
}

const fmtV = (v) => (v == null ? "—" : `${Number(v).toFixed(2)} V`)
const fmtDate = (t) => new Date(t * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h >= 24 ? `${Math.floor(h / 24)}d ${h % 24}h` : h ? `${h}h ${m}m` : `${m}m`
}
function fmtAgo(t, now) {
  const s = Math.max(0, now - t)
  if (s < 90) return "just now"
  if (s < 5400) return `${Math.round(s / 60)} min ago`
  if (s < 2 * DAY) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / DAY)} days ago`
}

export const Battery = {
  name: "Battery",
  components: { GalaxyTabs, VoltageChart },
  data() {
    return { TABS, TREND_RANGES, DETAIL_RANGES, tab: "trend", trendDays: 90, detailDays: 7, data: null, loading: false, error: "", showTable: false, parkLimit: PARK_ROWS, sampleLimit: SAMPLE_ROWS }
  },
  computed: {
    days() { return this.tab === "trend" ? this.trendDays : this.detailDays },
    now() { return this.data?.now || Date.now() / 1000 },
    t0() { return this.now - this.days * DAY },
    live() { return this.data?.live || null },
    hero() {
      const live = this.live
      if (!live) return null
      const onroad = live.live ? Boolean(this.data?.onroad) : Boolean(live.onroad)
      return {
        value: fmtV(live.voltage),
        status: batteryStatus(live.voltage, onroad),
        note: live.live ? (onroad ? "Live · driving" : "Live · parked") : `Last recorded ${fmtAgo(live.updatedAt, this.now)} · device reading unavailable`,
      }
    },
    sessions() { return this.data?.sessions || [] },
    parks() {
      return this.sessions.filter((s) => s.kind === "park").map((s) => {
        const duration = s.end_ts - s.start_ts
        const fromBoot = s.start_flag === "ign_off_at_boot"
        let dropRate = null
        if (s.v_at_1h != null && s.v_end != null && duration >= 2 * 3600) dropRate = (s.v_at_1h - s.v_end) / ((duration - 3600) / 3600)
        return { ...s, duration, fromBoot, dropRate }
      })
    },
    healthParks() { return this.parks.filter((p) => !p.fromBoot && p.start_ts >= this.t0) },
    trendSeries() {
      const series = (key, label, color, field) => ({
        key, label, color,
        points: this.healthParks.filter((p) => p[field] != null).map((p) => ({ t: p.start_ts, v: p[field], note: `parked ${fmtDuration(p.duration)}` })),
      })
      return [
        series("h1", "1 h after parking", "var(--vc-1)", "v_at_1h"),
        series("h3", "3 h after parking", "var(--vc-2)", "v_at_3h"),
      ]
    },
    driveSeries() {
      const points = this.sessions
        .filter((s) => s.kind === "drive" && s.v_mean != null && s.end_ts - s.start_ts >= MIN_DRIVE_S && s.start_ts >= this.t0)
        .map((s) => ({ t: s.start_ts, v: s.v_mean, note: `drive ${fmtDuration(s.end_ts - s.start_ts)} · min ${fmtV(s.v_min)}` }))
      return [{ key: "drive", label: "Average while driving", color: "var(--vc-3)", points }]
    },
    detailSeries() {
      const points = (this.data?.samples || []).filter((s) => s.ts_end >= this.t0)
        .map((s) => ({ t: (s.ts_start + s.ts_end) / 2, v: s.v_mean, lo: s.v_min, hi: s.v_max, note: s.onroad ? "driving" : "parked" }))
      return [{ key: "v", label: "12V battery", color: "var(--vc-1)", band: true, points }]
    },
    drivingSpans() {
      const spans = []
      for (const s of this.data?.samples || []) {
        if (!s.onroad || s.ts_end < this.t0) continue
        const last = spans[spans.length - 1]
        if (last && s.ts_start - last.t1 < 60) last.t1 = s.ts_end
        else spans.push({ t0: s.ts_start, t1: s.ts_end })
      }
      return spans
    },
    rangeParks() { return this.parks.filter((p) => p.end_ts >= this.t0) },
    parkRows() {
      return this.rangeParks.slice().reverse().slice(0, this.parkLimit).map((p) => ({
        key: p.id,
        start: fmtDate(p.start_ts),
        duration: fmtDuration(p.duration),
        h1: fmtV(p.v_at_1h), h3: fmtV(p.v_at_3h), h6: fmtV(p.v_at_6h),
        end: fmtV(p.v_end),
        drop: p.dropRate == null ? "—" : `${(p.dropRate * 1000).toFixed(0)} mV/h`,
        reason: p.end_reason == null ? "In progress" : (END_REASONS[p.end_reason] || p.end_reason),
        note: p.fromBoot ? "timed from device start" : "",
      }))
    },
    rangeSamples() { return (this.data?.samples || []).filter((s) => s.ts_end >= this.t0) },
    sampleRows() {
      return this.rangeSamples.slice().reverse().slice(0, this.sampleLimit).map((s) => ({
        key: s.ts_start, time: fmtDate(s.ts_start), mean: fmtV(s.v_mean), min: fmtV(s.v_min), max: fmtV(s.v_max), state: s.onroad ? "Driving" : "Parked",
      }))
    },
    gapS() { return (this.data?.sampleIntervalS || 600) * 1.6 },
  },
  watch: {
    tab() { this.load() },
    trendDays() { this.parkLimit = PARK_ROWS; this.load() },
    detailDays() { this.sampleLimit = SAMPLE_ROWS; this.load() },
  },
  methods: {
    async load() {
      // a quick tab or range switch can leave an older request still running: only the latest one counts
      const request = ++this.requestId
      this.loading = true
      this.error = ""
      try {
        const data = await api.getBatteryHistory(this.days, this.tab === "detail")
        if (request === this.requestId) this.data = data
      } catch (err) {
        if (request !== this.requestId) return
        this.error = err?.message || String(err)
        if (this.data) showSnackbar("Couldn't refresh battery history.", "error")
      } finally {
        if (request === this.requestId) this.loading = false
      }
    },
  },
  created() { this.requestId = 0 },
  mounted() { this.load() },
  template: `
    <div class="gx-battery">
      <div class="dh-hero">
        <div class="dh-hero__info">
          <h1 class="dh-title">12V Battery</h1>
          <p class="dh-sub">Voltage at the car's OBD port, as the comma device reads it.</p>
        </div>
        <button type="button" class="gx-btn gx-btn--tonal" :disabled="loading" @click="load"><i class="bi bi-arrow-clockwise"></i> Refresh</button>
      </div>

      <section class="gx-card gx-battery__now">
        <template v-if="hero">
          <strong class="gx-battery__value">{{ hero.value }}</strong>
          <span v-if="hero.status" class="gx-battery__status" :class="'is-' + hero.status.level"><i class="bi" :class="hero.status.icon"></i>{{ hero.status.label }}</span>
          <small>{{ hero.note }}</small>
        </template>
        <span v-else class="dh-muted">No reading yet. The device records once the panda reports a voltage.</span>
      </section>

      <div v-if="error && !data" class="gx-alert gx-alert--warn"><i class="bi bi-exclamation-triangle-fill gx-alert__icon"></i><div class="gx-alert__body"><strong>Couldn't load battery history</strong><span>{{ error }}</span></div></div>

      <GalaxyTabs :items="TABS" :active="tab" @select="tab = $event" />

      <template v-if="tab === 'trend'">
        <div class="gx-battery__ranges" role="group" aria-label="Time range">
          <button v-for="d in TREND_RANGES" :key="d" type="button" class="gx-tab" :class="{ active: trendDays === d }" :aria-pressed="trendDays === d" @click="trendDays = d">{{ d === 365 ? '1 year' : d + ' days' }}</button>
        </div>

        <section class="gx-card gx-battery__chart">
          <h3>Parked voltage</h3>
          <p class="gx-note">One point per park, measured 1 and 3 hours after the car was turned off (with the device's own draw). A steady downward drift over months points at an aging battery.</p>
          <VoltageChart :series="trendSeries" :t0="t0" :t1="now" :cutoff="data?.cutoffV ?? null" aria-label="Voltage 1 and 3 hours after parking, per park" />
        </section>

        <section class="gx-card gx-battery__chart">
          <h3>While driving</h3>
          <p class="gx-note">Average voltage per drive. A drop here points at charging (DC-DC converter) rather than the battery.</p>
          <VoltageChart :series="driveSeries" :t0="t0" :t1="now" :height="180" gutter aria-label="Average voltage per drive" />
        </section>

        <section class="gx-card gx-battery__table">
          <div class="gx-battery__table-head">
            <h3>Parks</h3>
            <button type="button" class="gx-btn gx-btn--tonal" :aria-expanded="showTable" @click="showTable = !showTable">{{ showTable ? 'Hide table' : 'Show table' }}</button>
          </div>
          <div v-if="showTable" class="gx-battery__scroll" tabindex="0" aria-label="Park history table">
            <table>
              <thead><tr><th>Parked</th><th>Length</th><th>After 1 h</th><th>After 3 h</th><th>After 6 h</th><th>Last</th><th>Drop</th><th>Ended</th></tr></thead>
              <tbody>
                <tr v-for="row in parkRows" :key="row.key">
                  <td>{{ row.start }}<small v-if="row.note"> ({{ row.note }})</small></td><td>{{ row.duration }}</td><td>{{ row.h1 }}</td><td>{{ row.h3 }}</td><td>{{ row.h6 }}</td><td>{{ row.end }}</td><td>{{ row.drop }}</td><td>{{ row.reason }}</td>
                </tr>
                <tr v-if="!parkRows.length"><td colspan="8" class="gx-empty">No parks recorded in this range.</td></tr>
              </tbody>
            </table>
          </div>
          <div v-if="showTable && rangeParks.length > parkRows.length" class="gx-battery__more">
            <small>Showing the newest {{ parkRows.length }} of {{ rangeParks.length }} parks</small>
            <button type="button" class="gx-btn gx-btn--text" @click="parkLimit += ${PARK_ROWS}">Show more</button>
          </div>
        </section>
      </template>

      <template v-else>
        <div class="gx-battery__ranges" role="group" aria-label="Time range">
          <button v-for="d in DETAIL_RANGES" :key="d" type="button" class="gx-tab" :class="{ active: detailDays === d }" :aria-pressed="detailDays === d" @click="detailDays = d">{{ d === 1 ? '24 hours' : d + ' days' }}</button>
        </div>

        <section class="gx-card gx-battery__chart">
          <h3>Voltage</h3>
          <p class="gx-note">10-minute averages with the min–max range. Breaks are times the device was off. Kept for {{ Math.round(data?.sampleRetentionDays || 30) }} days.</p>
          <VoltageChart :series="detailSeries" :t0="t0" :t1="now" :cutoff="data?.cutoffV ?? null" :spans="drivingSpans" span-label="Driving" :gap-s="gapS" :height="240" aria-label="12V battery voltage, 10-minute averages" />
        </section>

        <section class="gx-card gx-battery__table">
          <div class="gx-battery__table-head">
            <h3>Samples</h3>
            <button type="button" class="gx-btn gx-btn--tonal" :aria-expanded="showTable" @click="showTable = !showTable">{{ showTable ? 'Hide table' : 'Show table' }}</button>
          </div>
          <div v-if="showTable" class="gx-battery__scroll" tabindex="0" aria-label="Voltage samples table">
            <table>
              <thead><tr><th>Time</th><th>Average</th><th>Min</th><th>Max</th><th>State</th></tr></thead>
              <tbody>
                <tr v-for="row in sampleRows" :key="row.key"><td>{{ row.time }}</td><td>{{ row.mean }}</td><td>{{ row.min }}</td><td>{{ row.max }}</td><td>{{ row.state }}</td></tr>
                <tr v-if="!sampleRows.length"><td colspan="5" class="gx-empty">No samples in this range.</td></tr>
              </tbody>
            </table>
          </div>
          <div v-if="showTable && rangeSamples.length > sampleRows.length" class="gx-battery__more">
            <small>Showing the newest {{ sampleRows.length }} of {{ rangeSamples.length }} samples</small>
            <button type="button" class="gx-btn gx-btn--text" @click="sampleLimit += ${SAMPLE_ROWS}">Show more</button>
          </div>
        </section>
      </template>
    </div>
  `,
}
