import { api, showSnackbar } from "../api.js"
import { GalaxyTabs } from "../components/GalaxyTabs.js"
import { HistoryChart, fmtValue } from "../components/HistoryChart.js"
import { navigate, store } from "../store.js"

const DAY = 86400
const METRICS = { battery: "Battery", thermal: "Temperature" }
const TABS = { trend: "Trend", detail: "Detail" }
const TREND_RANGES = [30, 90, 365]
const DETAIL_RANGES = [1, 7, 30]
const MIN_DRIVE_S = 300
const PARK_ROWS = 50
const SAMPLE_ROWS = 200
const LIVE_RETRY_MS = 2500
// fallbacks until the first response; the server sends the device's values
const THERMAL_DEFAULTS = { dangerC: 85, overheatedC: 100, parkedFanCapPct: 30 }

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

// Parked device temperature against hardwared's limits. hotS is the time hardwared itself held the device over the
// parked limit (offroad 5 min and above dangerC, filtered), when it won't start a drive
export function parkedTempStatus(peakC, hotS, thermal = THERMAL_DEFAULTS) {
  if (peakC == null) return null
  if (peakC >= thermal.overheatedC) return { level: "critical", icon: "bi-x-octagon-fill", label: "Overheated" }
  if (hotS > 0) return { level: "warning", icon: "bi-exclamation-triangle-fill", label: "Too hot to drive" }
  return { level: "good", icon: "bi-check-circle-fill", label: "OK" }
}

const fmtV = (v) => fmtValue(v, "V")
const fmtC = (v) => fmtValue(v, "°C", 0)
const fmtPct = (v) => fmtValue(v, "%")
const fmtDelta = (v) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(0)} °C`)
const fmtDate = (t) => new Date(t * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h >= 24 ? `${Math.floor(h / 24)}d ${h % 24}h` : h ? `${h}h ${m}m` : `${m}m`
}
const fmtHot = (seconds) => (seconds == null ? "—" : seconds < 30 ? "none" : fmtDuration(seconds))
function fmtAgo(t, now) {
  const s = Math.max(0, now - t)
  if (s < 90) return "just now"
  if (s < 5400) return `${Math.round(s / 60)} min ago`
  if (s < 2 * DAY) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / DAY)} days ago`
}
const maxOf = (values) => values.reduce((best, v) => (v != null && (best == null || v > best) ? v : best), null)

export const DeviceHistory = {
  name: "DeviceHistory",
  components: { GalaxyTabs, HistoryChart },
  data() {
    return { METRICS, TABS, TREND_RANGES, DETAIL_RANGES, tab: "trend", trendDays: 90, detailDays: 7, response: null, loading: false, error: "", showTable: false, parkLimit: PARK_ROWS, sampleLimit: SAMPLE_ROWS }
  },
  computed: {
    metric() { return store.params.metric === "thermal" ? "thermal" : "battery" },
    // a response for the other metric has the other sample columns: never render it
    data() { return this.response?.metric === this.metric ? this.response : null },
    days() { return this.tab === "trend" ? this.trendDays : this.detailDays },
    now() { return this.data?.now || Date.now() / 1000 },
    t0() { return this.now - this.days * DAY },
    thermal() { return this.data?.thermal || THERMAL_DEFAULTS },
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
        // the 1/3/6 h readings are timed from when the battery started resting: switch-off or the end of a charge.
        // Unknown when the device booted with the car already parked and at rest
        const hasRest = s.rest_start_ts != null
        let dropRate = null
        if (hasRest && s.v_at_1h != null && s.v_rest_end != null) {
          const hours = (s.rest_end_ts - s.rest_start_ts - 3600) / 3600
          if (hours >= 1) dropRate = (s.v_at_1h - s.v_rest_end) / hours
        }
        return { ...s, duration, fromBoot, hasRest, dropRate }
      })
    },
    drives() {
      return this.sessions.filter((s) => s.kind === "drive" && s.end_ts - s.start_ts >= MIN_DRIVE_S && s.start_ts >= this.t0)
    },
    rangeParks() { return this.parks.filter((p) => p.end_ts >= this.t0) },
    rangeSamples() { return (this.data?.samples || []).filter((s) => s.ts_end >= this.t0) },
    drivingSpans() {
      const spans = []
      for (const s of this.rangeSamples) {
        if (!s.onroad) continue
        const last = spans[spans.length - 1]
        if (last && s.ts_start - last.t1 < 60) last.t1 = s.ts_end
        else spans.push({ t0: s.ts_start, t1: s.ts_end })
      }
      return spans
    },
    gapS() { return (this.data?.sampleIntervalS || 600) * 1.6 },
    cutoffLines() { return this.data?.cutoffV == null ? [] : [{ value: this.data.cutoffV, label: "Shutdown cutoff" }] },
    tempLines() {
      return [
        { value: this.thermal.dangerC, label: "Parked limit" },
        { value: this.thermal.overheatedC, label: "Overheated" },
      ]
    },

    // battery
    healthParks() { return this.parks.filter((p) => p.hasRest && p.start_ts >= this.t0) },
    trendSeries() {
      const note = (p) => `${p.rest_from === "charge" ? "rest from the end of a charge" : "rest from switch-off"} · parked ${fmtDuration(p.duration)}`
      const series = (key, label, color, field) => ({
        key, label, color,
        points: this.healthParks.filter((p) => p[field] != null).map((p) => ({ t: p.start_ts, v: p[field], note: note(p) })),
      })
      return [
        series("h1", "1 h at rest", "var(--vc-1)", "v_at_1h"),
        series("h3", "3 h at rest", "var(--vc-2)", "v_at_3h"),
      ]
    },
    trendEmpty() {
      const unknown = this.rangeParks.filter((p) => p.start_ts >= this.t0 && !p.hasRest).length
      const text = "No park in this range has rested for an hour yet."
      if (!unknown) return text
      const parks = unknown === 1 ? "1 park has" : `${unknown} parks have`
      return `${text} ${parks} no known rest start (the device started with the car already parked, or it was still charging); see the table.`
    },
    driveSeries() {
      const points = this.drives.filter((s) => s.v_mean != null)
        .map((s) => ({ t: s.start_ts, v: s.v_mean, note: `drive ${fmtDuration(s.end_ts - s.start_ts)} · min ${fmtV(s.v_min)}` }))
      return [{ key: "drive", label: "Average while driving", color: "var(--vc-3)", points }]
    },
    detailSeries() {
      const points = this.rangeSamples.map((s) => ({ t: (s.ts_start + s.ts_end) / 2, v: s.v_mean, lo: s.v_min, hi: s.v_max, note: s.onroad ? "driving" : "parked" }))
      return [{ key: "v", label: "12V battery", color: "var(--vc-1)", band: true, points }]
    },
    parkRows() {
      return this.rangeParks.slice().reverse().slice(0, this.parkLimit).map((p) => ({
        key: p.id,
        start: fmtDate(p.start_ts),
        duration: fmtDuration(p.duration),
        h1: fmtV(p.v_at_1h), h3: fmtV(p.v_at_3h), h6: fmtV(p.v_at_6h),
        end: fmtV(p.v_end),
        drop: p.dropRate == null ? "—" : `${(p.dropRate * 1000).toFixed(0)} mV/h`,
        topups: p.topups ?? "—",
        reason: p.end_reason == null ? "In progress" : (END_REASONS[p.end_reason] || p.end_reason),
        note: [
          !p.hasRest ? "rest start unknown" : p.rest_from === "charge" ? "timed from the end of a charge" : "",
          p.rest_closed ? "cut short by a charge" : "",
        ].filter(Boolean).join(", "),
      }))
    },
    sampleRows() {
      return this.rangeSamples.slice().reverse().slice(0, this.sampleLimit).map((s) => ({
        key: s.ts_start, time: fmtDate(s.ts_start), mean: fmtV(s.v_mean), min: fmtV(s.v_min), max: fmtV(s.v_max), state: s.onroad ? "Driving" : "Parked",
      }))
    },

    // temperature
    thermalParks() { return this.rangeParks.filter((p) => p.soc_max != null) },
    thermalSummary() {
      const parks = this.thermalParks
      if (!parks.length) return null
      const peak = maxOf(parks.map((p) => p.soc_max))
      const hot = parks.reduce((sum, p) => sum + (p.s_hot || 0), 0)
      return [
        { label: "Hottest while parked", value: fmtC(peak), status: parkedTempStatus(peak, hot, this.thermal) },
        { label: "Hottest cabin air", value: fmtC(maxOf(parks.map((p) => p.intake_max))), note: "at the device's fan intake" },
        { label: "Over the parked limit", value: fmtHot(hot), note: `${this.thermal.dangerC} °C, over ${parks.length} parks` },
      ]
    },
    parkTempSeries() {
      const note = (p) => [`parked ${fmtDuration(p.duration)}`, p.self_heat_mean != null ? `${fmtDelta(p.self_heat_mean)} over cabin on average` : "", p.fan_pct_mean != null ? `fan ${fmtPct(p.fan_pct_mean)}` : ""].filter(Boolean).join(" · ")
      const series = (key, label, color, field) => ({
        key, label, color,
        points: this.thermalParks.filter((p) => p[field] != null && p.start_ts >= this.t0).map((p) => ({ t: p.start_ts, v: p[field], note: note(p) })),
      })
      return [
        series("soc", "Device peak", "var(--vc-2)", "soc_max"),
        series("intake", "Cabin air peak", "var(--vc-1)", "intake_max"),
      ]
    },
    parkHotSeries() {
      const points = this.thermalParks.filter((p) => p.s_hot != null && p.start_ts >= this.t0)
        .map((p) => ({ t: p.start_ts, v: p.s_hot / 3600, note: `parked ${fmtDuration(p.duration)} · peak ${fmtC(p.soc_max)}` }))
      return [{ key: "hot", label: "Time over the parked limit", color: "var(--vc-2)", points }]
    },
    driveTempSeries() {
      const points = this.drives.filter((s) => s.soc_max != null)
        .map((s) => ({ t: s.start_ts, v: s.soc_max, note: `drive ${fmtDuration(s.end_ts - s.start_ts)} · average ${fmtC(s.soc_mean)}` }))
      return [{ key: "drive", label: "Device peak while driving", color: "var(--vc-2)", points }]
    },
    detailTempSeries() {
      const samples = this.rangeSamples.filter((s) => s.soc_mean != null)
      const state = (s) => (s.onroad ? "driving" : "parked")
      return [
        { key: "soc", label: "Device", color: "var(--vc-2)", band: true, rangeLabel: "average to peak",
          points: samples.map((s) => ({ t: (s.ts_start + s.ts_end) / 2, v: s.soc_mean, lo: s.soc_mean, hi: s.soc_max, note: state(s) })) },
        { key: "intake", label: "Cabin air", color: "var(--vc-1)",
          points: samples.filter((s) => s.intake_mean != null).map((s) => ({ t: (s.ts_start + s.ts_end) / 2, v: s.intake_mean })) },
      ]
    },
    detailFanSeries() {
      const points = this.rangeSamples.filter((s) => s.fan_pct_mean != null)
        .map((s) => ({ t: (s.ts_start + s.ts_end) / 2, v: s.fan_pct_mean, note: s.fan_rpm_mean != null ? `${Math.round(s.fan_rpm_mean)} rpm` : "" }))
      return [{ key: "fan", label: "Fan", color: "var(--vc-3)", points }]
    },
    fanLines() { return [{ value: this.thermal.parkedFanCapPct, label: "Parked cap" }] },
    parkTempRows() {
      return this.rangeParks.slice().reverse().slice(0, this.parkLimit).map((p) => ({
        key: p.id,
        start: fmtDate(p.start_ts),
        duration: fmtDuration(p.duration),
        cabinStart: fmtC(p.intake_at_start),
        cabinPeak: fmtC(p.intake_max),
        peak: fmtC(p.soc_max),
        mean: fmtC(p.soc_mean),
        overCabin: fmtDelta(p.self_heat_mean),
        fan: fmtPct(p.fan_pct_mean),
        hot: fmtHot(p.s_hot),
        reason: p.end_reason == null ? "In progress" : (END_REASONS[p.end_reason] || p.end_reason),
        note: p.fromBoot ? "cabin at device start" : "",
      }))
    },
    sampleTempRows() {
      return this.rangeSamples.slice().reverse().slice(0, this.sampleLimit).map((s) => ({
        key: s.ts_start, time: fmtDate(s.ts_start), mean: fmtC(s.soc_mean), peak: fmtC(s.soc_max), cpu: fmtC(s.cpu_max), gpu: fmtC(s.gpu_max),
        cabin: fmtC(s.intake_mean), exhaust: fmtC(s.exhaust_mean), fan: fmtPct(s.fan_pct_mean), hot: fmtHot(s.s_hot), state: s.onroad ? "Driving" : "Parked",
      }))
    },
  },
  watch: {
    metric() { this.load() },
    tab() { this.load() },
    trendDays() { this.parkLimit = PARK_ROWS; this.load() },
    detailDays() { this.sampleLimit = SAMPLE_ROWS; this.load() },
  },
  methods: {
    setMetric(metric) { navigate(metric === "thermal" ? "/history?metric=thermal" : "/history") },
    async load() {
      // a quick tab, range or metric switch can leave an older request still running: only the latest one counts
      const request = ++this.requestId
      this.loading = true
      this.error = ""
      try {
        const response = await api.getDeviceHistory(this.days, this.tab === "detail", this.metric)
        if (request !== this.requestId) return
        this.response = response
        // the Galaxy's live 12V subscriber starts on request and isn't waited for: ask once more when it has a reading
        if (this.metric === "battery" && !response?.live?.live && !this.liveRetried) {
          this.liveRetried = true
          clearTimeout(this.liveTimer)
          this.liveTimer = setTimeout(() => this.load(), LIVE_RETRY_MS)
        }
      } catch (err) {
        if (request !== this.requestId) return
        this.error = err?.message || String(err)
        if (this.data) showSnackbar("Couldn't refresh device history.", "error")
      } finally {
        if (request === this.requestId) this.loading = false
      }
    },
  },
  created() {
    this.requestId = 0
    this.liveRetried = false
    // the page used to be /battery: replace it, in the Galaxy's own back stack too, so Back leaves the page
    if (store.route === "/battery") {
      const i = store.history.lastIndexOf("/battery")
      if (i >= 0) store.history.splice(i, 1)
      window.location.replace("#/history")
    }
  },
  beforeUnmount() { clearTimeout(this.liveTimer) },
  mounted() { this.load() },
  template: `
    <div class="gx-history">
      <div class="dh-hero">
        <div class="dh-hero__info">
          <h1 class="dh-title">Device history</h1>
          <p class="dh-sub" v-if="metric === 'battery'">12V battery voltage at the car's OBD port, as the comma device reads it.</p>
          <p class="dh-sub" v-else>How hot the device gets, parked and driving, against the cabin air at its fan intake.</p>
        </div>
        <button type="button" class="gx-btn gx-btn--tonal" :disabled="loading" @click="load"><i class="bi bi-arrow-clockwise"></i> Refresh</button>
      </div>

      <div class="gx-history__ranges gx-history__metrics" role="group" aria-label="What to show">
        <button v-for="(label, key) in METRICS" :key="key" type="button" class="gx-tab" :class="{ active: metric === key }" :aria-pressed="metric === key" @click="setMetric(key)">{{ label }}</button>
      </div>

      <section v-if="metric === 'battery'" class="gx-card gx-history__now">
        <template v-if="hero">
          <strong class="gx-history__value">{{ hero.value }}</strong>
          <span v-if="hero.status" class="gx-history__status" :class="'is-' + hero.status.level"><i class="bi" :class="hero.status.icon"></i>{{ hero.status.label }}</span>
          <small>{{ hero.note }}</small>
        </template>
        <span v-else class="dh-muted">No reading yet. The device records once the panda reports a voltage.</span>
      </section>

      <section v-else class="gx-card gx-history__now gx-history__stats">
        <template v-if="thermalSummary">
          <div v-for="s in thermalSummary" :key="s.label" class="gx-history__stat">
            <small>{{ s.label }}</small>
            <strong class="gx-history__value">{{ s.value }}</strong>
            <span v-if="s.status" class="gx-history__status" :class="'is-' + s.status.level"><i class="bi" :class="s.status.icon"></i>{{ s.status.label }}</span>
            <small v-if="s.note">{{ s.note }}</small>
          </div>
        </template>
        <span v-else class="dh-muted">No parked temperatures recorded in this range yet.</span>
      </section>

      <div v-if="error && !data" class="gx-alert gx-alert--warn"><i class="bi bi-exclamation-triangle-fill gx-alert__icon"></i><div class="gx-alert__body"><strong>Couldn't load device history</strong><span>{{ error }}</span></div></div>

      <GalaxyTabs :items="TABS" :active="tab" @select="tab = $event" />

      <template v-if="tab === 'trend'">
        <div class="gx-history__ranges" role="group" aria-label="Time range">
          <button v-for="d in TREND_RANGES" :key="d" type="button" class="gx-tab" :class="{ active: trendDays === d }" :aria-pressed="trendDays === d" @click="trendDays = d">{{ d === 365 ? '1 year' : d + ' days' }}</button>
        </div>

        <template v-if="metric === 'battery'">
          <section class="gx-card gx-history__chart">
            <h3>Parked voltage</h3>
            <p class="gx-note">One point per park, measured 1 and 3 hours into its rest (with the device's own draw). The rest starts when the car is turned off, or again when the car stops charging the 12V while parked. A steady downward drift over months points at an aging battery.</p>
            <HistoryChart :series="trendSeries" :t0="t0" :t1="now" :ref-lines="cutoffLines" :empty-text="trendEmpty" aria-label="Voltage 1 and 3 hours into each park's rest" />
          </section>

          <section class="gx-card gx-history__chart">
            <h3>While driving</h3>
            <p class="gx-note">Average voltage per drive. A drop here points at charging (DC-DC converter) rather than the battery.</p>
            <HistoryChart :series="driveSeries" :t0="t0" :t1="now" :height="180" gutter aria-label="Average voltage per drive" />
          </section>

          <section class="gx-card gx-history__table">
            <div class="gx-history__table-head">
              <h3>Parks</h3>
              <button type="button" class="gx-btn gx-btn--tonal" :aria-expanded="showTable" @click="showTable = !showTable">{{ showTable ? 'Hide table' : 'Show table' }}</button>
            </div>
            <div v-if="showTable" class="gx-history__scroll" tabindex="0" aria-label="Park history table">
              <table>
                <thead><tr><th>Parked</th><th>Length</th><th>1 h at rest</th><th>3 h at rest</th><th>6 h at rest</th><th>Last</th><th>Drop</th><th>Top-ups</th><th>Ended</th></tr></thead>
                <tbody>
                  <tr v-for="row in parkRows" :key="row.key">
                    <td>{{ row.start }}<small v-if="row.note"> ({{ row.note }})</small></td><td>{{ row.duration }}</td><td>{{ row.h1 }}</td><td>{{ row.h3 }}</td><td>{{ row.h6 }}</td><td>{{ row.end }}</td><td>{{ row.drop }}</td><td>{{ row.topups }}</td><td>{{ row.reason }}</td>
                  </tr>
                  <tr v-if="!parkRows.length"><td colspan="9" class="gx-empty">No parks recorded in this range.</td></tr>
                </tbody>
              </table>
            </div>
            <div v-if="showTable && rangeParks.length > parkRows.length" class="gx-history__more">
              <small>Showing the newest {{ parkRows.length }} of {{ rangeParks.length }} parks</small>
              <button type="button" class="gx-btn gx-btn--text" @click="parkLimit += ${PARK_ROWS}">Show more</button>
            </div>
          </section>
        </template>

        <template v-else>
          <section class="gx-card gx-history__chart">
            <h3>Parked temperatures</h3>
            <p class="gx-note">One point per park: the device's peak (hottest of CPU, GPU, memory and power chips) and the cabin air at its fan intake. While parked, the fan is capped at {{ thermal.parkedFanCapPct }}% and nothing shuts the device down for heat; after 5 minutes above {{ thermal.dangerC }} °C (the parked limit) it won't start a drive until it cools.</p>
            <HistoryChart unit="°C" :series="parkTempSeries" :t0="t0" :t1="now" :ref-lines="tempLines" aria-label="Peak device and cabin temperature per park" />
          </section>

          <section class="gx-card gx-history__chart">
            <h3>Time over the parked limit</h3>
            <p class="gx-note">Hours per park the device spent over {{ thermal.dangerC }} °C by the device's own count, unable to start a drive.</p>
            <HistoryChart unit="h" :series="parkHotSeries" :t0="t0" :t1="now" :height="160" gutter aria-label="Hours over the parked limit, per park" />
          </section>

          <section class="gx-card gx-history__chart">
            <h3>While driving</h3>
            <p class="gx-note">Peak device temperature per drive. Held above {{ thermal.overheatedC }} °C, the device counts as overheated and shows an overheat alert.</p>
            <HistoryChart unit="°C" :series="driveTempSeries" :t0="t0" :t1="now" :height="180" :ref-lines="[{ value: thermal.overheatedC, label: 'Overheated' }]" gutter aria-label="Peak device temperature per drive" />
          </section>

          <section class="gx-card gx-history__table">
            <div class="gx-history__table-head">
              <h3>Parks</h3>
              <button type="button" class="gx-btn gx-btn--tonal" :aria-expanded="showTable" @click="showTable = !showTable">{{ showTable ? 'Hide table' : 'Show table' }}</button>
            </div>
            <div v-if="showTable" class="gx-history__scroll" tabindex="0" aria-label="Park temperature table">
              <table>
                <thead><tr><th>Parked</th><th>Length</th><th>Cabin at start</th><th>Cabin peak</th><th>Device peak</th><th>Device avg</th><th>Over cabin</th><th>Fan</th><th>Over limit</th><th>Ended</th></tr></thead>
                <tbody>
                  <tr v-for="row in parkTempRows" :key="row.key">
                    <td>{{ row.start }}<small v-if="row.note"> ({{ row.note }})</small></td><td>{{ row.duration }}</td><td>{{ row.cabinStart }}</td><td>{{ row.cabinPeak }}</td><td>{{ row.peak }}</td><td>{{ row.mean }}</td><td>{{ row.overCabin }}</td><td>{{ row.fan }}</td><td>{{ row.hot }}</td><td>{{ row.reason }}</td>
                  </tr>
                  <tr v-if="!parkTempRows.length"><td colspan="10" class="gx-empty">No parks recorded in this range.</td></tr>
                </tbody>
              </table>
            </div>
            <div v-if="showTable && rangeParks.length > parkTempRows.length" class="gx-history__more">
              <small>Showing the newest {{ parkTempRows.length }} of {{ rangeParks.length }} parks</small>
              <button type="button" class="gx-btn gx-btn--text" @click="parkLimit += ${PARK_ROWS}">Show more</button>
            </div>
          </section>
        </template>
      </template>

      <template v-else>
        <div class="gx-history__ranges" role="group" aria-label="Time range">
          <button v-for="d in DETAIL_RANGES" :key="d" type="button" class="gx-tab" :class="{ active: detailDays === d }" :aria-pressed="detailDays === d" @click="detailDays = d">{{ d === 1 ? '24 hours' : d + ' days' }}</button>
        </div>

        <template v-if="metric === 'battery'">
          <section class="gx-card gx-history__chart">
            <h3>Voltage</h3>
            <p class="gx-note">10-minute averages with the min–max range. Breaks are times the device was off. Kept for {{ Math.round(data?.sampleRetentionDays || 30) }} days.</p>
            <HistoryChart :series="detailSeries" :t0="t0" :t1="now" :ref-lines="cutoffLines" :spans="drivingSpans" span-label="Driving" :gap-s="gapS" :height="240" aria-label="12V battery voltage, 10-minute averages" />
          </section>

          <section class="gx-card gx-history__table">
            <div class="gx-history__table-head">
              <h3>Samples</h3>
              <button type="button" class="gx-btn gx-btn--tonal" :aria-expanded="showTable" @click="showTable = !showTable">{{ showTable ? 'Hide table' : 'Show table' }}</button>
            </div>
            <div v-if="showTable" class="gx-history__scroll" tabindex="0" aria-label="Voltage samples table">
              <table>
                <thead><tr><th>Time</th><th>Average</th><th>Min</th><th>Max</th><th>State</th></tr></thead>
                <tbody>
                  <tr v-for="row in sampleRows" :key="row.key"><td>{{ row.time }}</td><td>{{ row.mean }}</td><td>{{ row.min }}</td><td>{{ row.max }}</td><td>{{ row.state }}</td></tr>
                  <tr v-if="!sampleRows.length"><td colspan="5" class="gx-empty">No samples in this range.</td></tr>
                </tbody>
              </table>
            </div>
            <div v-if="showTable && rangeSamples.length > sampleRows.length" class="gx-history__more">
              <small>Showing the newest {{ sampleRows.length }} of {{ rangeSamples.length }} samples</small>
              <button type="button" class="gx-btn gx-btn--text" @click="sampleLimit += ${SAMPLE_ROWS}">Show more</button>
            </div>
          </section>
        </template>

        <template v-else>
          <section class="gx-card gx-history__chart">
            <h3>Temperatures</h3>
            <p class="gx-note">10-minute averages of the device, with the band up to its peak, and of the cabin air at the fan intake. Breaks are times the device was off. Kept for {{ Math.round(data?.sampleRetentionDays || 30) }} days.</p>
            <HistoryChart unit="°C" :series="detailTempSeries" :t0="t0" :t1="now" :ref-lines="tempLines" :spans="drivingSpans" span-label="Driving" :gap-s="gapS" :height="240" aria-label="Device and cabin temperature, 10-minute averages" />
          </section>

          <section class="gx-card gx-history__chart">
            <h3>Fan</h3>
            <p class="gx-note">Fan speed the device asked for. Parked, it can't go above {{ thermal.parkedFanCapPct }}%: a fan flat at the cap while the device keeps warming means the cap is the limit.</p>
            <HistoryChart unit="%" :series="detailFanSeries" :t0="t0" :t1="now" :ref-lines="fanLines" :spans="drivingSpans" span-label="Driving" :gap-s="gapS" :height="160" gutter aria-label="Fan speed, 10-minute averages" />
          </section>

          <section class="gx-card gx-history__table">
            <div class="gx-history__table-head">
              <h3>Samples</h3>
              <button type="button" class="gx-btn gx-btn--tonal" :aria-expanded="showTable" @click="showTable = !showTable">{{ showTable ? 'Hide table' : 'Show table' }}</button>
            </div>
            <div v-if="showTable" class="gx-history__scroll" tabindex="0" aria-label="Temperature samples table">
              <table>
                <thead><tr><th>Time</th><th>Device avg</th><th>Device peak</th><th>CPU peak</th><th>GPU peak</th><th>Cabin</th><th>Exhaust</th><th>Fan</th><th>Over limit</th><th>State</th></tr></thead>
                <tbody>
                  <tr v-for="row in sampleTempRows" :key="row.key"><td>{{ row.time }}</td><td>{{ row.mean }}</td><td>{{ row.peak }}</td><td>{{ row.cpu }}</td><td>{{ row.gpu }}</td><td>{{ row.cabin }}</td><td>{{ row.exhaust }}</td><td>{{ row.fan }}</td><td>{{ row.hot }}</td><td>{{ row.state }}</td></tr>
                  <tr v-if="!sampleTempRows.length"><td colspan="10" class="gx-empty">No samples in this range.</td></tr>
                </tbody>
              </table>
            </div>
            <div v-if="showTable && rangeSamples.length > sampleTempRows.length" class="gx-history__more">
              <small>Showing the newest {{ sampleTempRows.length }} of {{ rangeSamples.length }} samples</small>
              <button type="button" class="gx-btn gx-btn--text" @click="sampleLimit += ${SAMPLE_ROWS}">Show more</button>
            </div>
          </section>
        </template>
      </template>
    </div>
  `,
}
