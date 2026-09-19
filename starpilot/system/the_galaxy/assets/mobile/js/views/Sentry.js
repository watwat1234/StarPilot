import { api, showSnackbar } from "../api.js"
import { usePolling } from "../composables.js"
import { GalaxyConfirm } from "../components/GalaxyModal.js"
import { GalaxySection } from "../components/GalaxySection.js"

function b64ToBytes(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4)
  const normalized = (value + padding).replace(/-/g, "+").replace(/_/g, "/")
  const raw = window.atob(normalized)
  return Uint8Array.from(raw, (ch) => ch.charCodeAt(0))
}

const HISTORY_PAGE_SIZE = 10

export const Sentry = {
  name: "Sentry",
  components: { GalaxySection },
  data() {
    return {
      loading: true,
      savingKey: "",
      params: {},
      status: {},
      event: {},
      history: [],
      historyTotal: 0,
      historyHasMore: false,
      historyError: false,
      dateFrom: "",
      dateTo: "",
      historyVisible: false,
      historyBusy: false,
      newEvents: false,
      liveCapture: {},
      testBusy: false,
      liveBusy: false,
      deleteBusy: false,
      timelapseBusy: false,
      timelapseCamera: "wide",
      timelapseFormat: "mp4",
      pushBusy: false,
    }
  },
  created() {
    this.historyRequestId = 0
    this.poll = usePolling(() => this.loadStatus(), { interval: 5000 })
    this.poll.start()
  },
  mounted() { this.loadParams() },
  beforeUnmount() {
    this.poll?.destroy()
    this.stopObserving()
  },
  watch: {
    // history is replaced on every load, so this also re-arms the observer when the
    // sentinel is still on screen after an append (e.g. a short first page).
    history() { this.$nextTick(() => this.observeSentinel()) },
    historyHasMore() { this.$nextTick(() => this.observeSentinel()) },
    historyVisible(visible) { if (!visible) this.stopObserving() },
  },
  computed: {
    statusText() { return String(this.status?.state || "unknown") },
    hasEvent() { return !!(this.event && this.event.eventId) },
    filterActive() { return !!(this.dateFrom || this.dateTo) },
  },
  methods: {
    async loadParams() {
      try {
        this.params = (await api.getParams()) || {}
      } catch (e) {
        showSnackbar("Failed to load Sentry settings.", "error")
      } finally {
        this.loading = false
      }
    },
    async loadStatus() {
      try {
        const payload = await api.getSentryStatus()
        this.status = payload?.status || {}
        this.event = payload?.lastEvent || {}
        // Never rebuild the history list from the poll; just flag that it is stale.
        if (this.historyVisible && !this.filterActive && this.history.length && this.hasEvent
          && String(this.event.eventId) !== String(this.history[0].eventId || "")) {
          this.newEvents = true
        }
      } catch (e) {
        if (!this.loading) showSnackbar("Failed to load Sentry status.", "error")
      }
    },
    stopObserving() {
      this.observer?.disconnect()
      this.observer = null
    },
    observeSentinel() {
      this.stopObserving()
      const el = this.$refs.sentinel
      if (!el || typeof IntersectionObserver === "undefined") return
      this.observer = new IntersectionObserver((entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return
        if (this.historyHasMore && !this.historyBusy && !this.historyError) this.loadHistory({ append: true })
      }, { rootMargin: "400px 0px" })
      this.observer.observe(el)
    },
    // Local calendar days -> UTC instants; "until" is exclusive so the end date is inclusive.
    historyRange() {
      const range = {}
      if (this.dateFrom) range.since = new Date(`${this.dateFrom}T00:00:00`).toISOString()
      if (this.dateTo) {
        const end = new Date(`${this.dateTo}T00:00:00`)
        end.setDate(end.getDate() + 1)
        range.until = end.toISOString()
      }
      return range
    },
    applyFilter() {
      if (this.dateFrom && this.dateTo && this.dateFrom > this.dateTo) {
        showSnackbar("The start date is after the end date.", "error")
        return
      }
      this.loadHistory({ force: true })
    },
    clearFilter() {
      this.dateFrom = ""
      this.dateTo = ""
      this.applyFilter()
    },
    async makeTimelapse() {
      if (this.timelapseBusy) return
      this.timelapseBusy = true
      try {
        const { blob, frames } = await api.getSentryTimelapse({
          ...this.historyRange(),
          camera: this.timelapseCamera,
          format: this.timelapseFormat,
        })
        const url = URL.createObjectURL(blob)
        const a = document.createElement("a")
        a.href = url
        a.download = `sentry-timelapse.${this.timelapseFormat}`
        a.click()
        setTimeout(() => URL.revokeObjectURL(url), 1000)
        showSnackbar(`Timelapse ready (${frames} frame${frames === 1 ? "" : "s"}).`)
      } catch (e) {
        showSnackbar(e?.data?.error || e?.message || "Timelapse failed.", "error")
      } finally {
        this.timelapseBusy = false
      }
    },
    async deleteAllHistory() {
      if (this.deleteBusy || !this.historyTotal) return
      const range = this.historyRange()
      const scoped = this.filterActive
      if (!(await GalaxyConfirm({
        title: scoped ? "Delete events in this range?" : "Delete ALL Sentry events?",
        message: scoped
          ? `Delete the ${this.historyTotal} Sentry event${this.historyTotal === 1 ? "" : "s"} in this date range and their camera images? This cannot be undone.`
          : `Delete all ${this.historyTotal} Sentry events and their camera images? This cannot be undone.`,
        confirmLabel: scoped ? "Delete matching" : "Delete all",
        danger: true,
      }))) return
      this.deleteBusy = true
      try {
        const result = await api.deleteSentryEvents({ ...range, all: !scoped })
        const failed = result?.failed ? ` ${result.failed} could not be removed.` : ""
        showSnackbar(`Deleted ${result?.deleted ?? 0} Sentry event${result?.deleted === 1 ? "" : "s"}.${failed}`)
        await this.loadHistory({ force: true })
        this.loadStatus()
      } catch (e) {
        showSnackbar(e?.data?.error || e?.message || "Sentry event deletion failed.", "error")
      } finally {
        this.deleteBusy = false
      }
    },
    async loadHistory({ append = false, force = false } = {}) {
      if (this.historyBusy && !force) return
      const requestId = ++this.historyRequestId
      this.historyBusy = true
      this.historyError = false
      try {
        const payload = await api.getSentryEvents({
          limit: HISTORY_PAGE_SIZE,
          offset: append ? this.history.length : 0,
          ...this.historyRange(),
        })
        if (requestId !== this.historyRequestId) return
        const events = Array.isArray(payload?.events) ? payload.events : []
        if (append) {
          const seen = new Set(this.history.map((e) => String(e?.eventId || "")))
          this.history = [...this.history, ...events.filter((e) => !seen.has(String(e?.eventId || "")))]
        } else {
          this.history = events
          this.newEvents = false
        }
        this.historyTotal = Number.isFinite(payload?.total) ? payload.total : this.history.length
        this.historyHasMore = !!payload?.hasMore
      } catch (e) {
        if (requestId !== this.historyRequestId) return
        this.historyError = true
        showSnackbar("Failed to load Sentry history.", "error")
      } finally {
        if (requestId === this.historyRequestId) this.historyBusy = false
      }
    },
    async toggleHistory() {
      this.historyVisible = !this.historyVisible
      if (this.historyVisible) this.loadHistory()
    },
    numeric(key, fallback) {
      const n = Number(this.params[key])
      return Number.isFinite(n) ? n : fallback
    },
    async saveParam(key, value) {
      this.savingKey = key
      try {
        const payload = await api.updateParam({ key, value })
        this.params = { ...this.params, ...(payload?.updated || {}), [key]: value }
        showSnackbar(payload?.message || "Sentry setting saved.")
      } catch (e) {
        showSnackbar(e?.data?.error || e?.message || `Failed to update ${key}.`, "error")
      } finally {
        this.savingKey = ""
      }
    },
    async sendTestEvent() {
      if (this.testBusy) return
      this.testBusy = true
      try {
        await api.postAction("/api/sentry/test")
        showSnackbar("Test capture started. The images will appear here shortly.")
      } catch (e) {
        showSnackbar(e?.data?.error || "Sentry test failed.", "error")
      } finally {
        this.testBusy = false
      }
    },
    async viewLive() {
      if (this.liveBusy) return
      this.liveBusy = true
      try {
        this.liveCapture = await api.getSentryLive()
        showSnackbar("Live camera snapshot captured.")
      } catch (e) {
        showSnackbar(e?.message || "Live camera capture failed.", "error")
      } finally {
        this.liveBusy = false
      }
    },
    kindLabel(kind) {
      return String(kind || "event").toUpperCase()
    },
    kindColor(kind) {
      const k = String(kind || "")
      if (k === "alarm") return "var(--error)"
      if (k === "warning") return "var(--warning)"
      if (k === "selfie") return "var(--primary)"
      return "var(--text-muted)"
    },
    liveImageUrl(url) {
      const cacheKey = encodeURIComponent(this.liveCapture?.capturedAt || "")
      return cacheKey ? `${url}?t=${cacheKey}` : url
    },
    async enablePush() {
      if (this.pushBusy) return
      this.pushBusy = true
      try {
        if (typeof Notification === "undefined" || !("serviceWorker" in navigator) || !("PushManager" in window)) {
          showSnackbar("This browser does not support Web Push notifications.", "error")
          return
        }
        if (!window.isSecureContext) {
          showSnackbar("Browser notifications require Galaxy over HTTPS.", "error")
          return
        }
        const permission = await Notification.requestPermission()
        if (permission !== "granted") {
          showSnackbar("Browser notification permission was not granted.", "error")
          return
        }
        const config = await api.getSentryPushConfig()
        if (!config?.enabled || !config?.publicKey) {
          showSnackbar(config?.error || "Galaxy Web Push is unavailable.", "error")
          return
        }
        await navigator.serviceWorker.register("/service-worker.js", { scope: "/" })
        const registration = await navigator.serviceWorker.ready
        let subscription = await registration.pushManager.getSubscription()
        if (!subscription) {
          subscription = await registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: b64ToBytes(config.publicKey),
          })
        }
        const body = typeof subscription.toJSON === "function"
          ? subscription.toJSON()
          : {
            endpoint: subscription.endpoint,
            expirationTime: subscription.expirationTime,
            keys: {
              p256dh: subscription.getKey ? btoa(String.fromCharCode(...new Uint8Array(subscription.getKey("p256dh")))) : "",
              auth: subscription.getKey ? btoa(String.fromCharCode(...new Uint8Array(subscription.getKey("auth")))) : "",
            },
          }
        await api.sentryPushSubscribe(body)
        showSnackbar("Browser notifications enabled for this device.")
      } catch (e) {
        showSnackbar(e?.message || "Could not enable browser notifications.", "error")
      } finally {
        this.pushBusy = false
      }
    },
    async sendTestNotification() {
      if (this.pushBusy) return
      this.pushBusy = true
      try {
        const payload = await api.postAction("/api/sentry/test-notification")
        const channels = Object.entries(payload?.channels || {})
          .filter(([, configured]) => configured)
          .map(([channel]) => channel === "webPush" ? "browser" : channel)
        showSnackbar(channels.length ? `Test notification sent through ${channels.join(", ")}.` : "Test notification sent.")
      } catch (e) {
        showSnackbar(e?.message || "Could not send the test notification.", "error")
      } finally {
        this.pushBusy = false
      }
    },
    async deleteEvent(eventId) {
      eventId = String(eventId || "")
      if (!eventId || this.deleteBusy) return
      if (!(await GalaxyConfirm({
        title: "Delete Sentry event?",
        message: "Delete this Sentry event and its camera images? This cannot be undone.",
        confirmLabel: "Delete",
        danger: true,
      }))) return
      this.deleteBusy = true
      try {
        await api.deleteSentryEvent(eventId)
        const before = this.history.length
        this.history = this.history.filter((e) => String(e?.eventId || "") !== eventId)
        if (this.history.length < before) this.historyTotal = Math.max(0, this.historyTotal - 1)
        if (!this.history.length && this.historyHasMore) this.loadHistory()
        if (String(this.event?.eventId || "") === eventId) {
          this.event = {}
          this.loadStatus()
        }
        showSnackbar("Sentry event deleted.")
      } catch (e) {
        showSnackbar(e?.data?.error || e?.message || "Sentry event deletion failed.", "error")
      } finally {
        this.deleteBusy = false
      }
    },
  },
  template: `
    <div>
      <div style="display:flex; align-items:center; justify-content:space-between; gap:8px; flex-wrap:wrap;">
        <div>
          <h2 style="margin-top:0; margin-bottom:4px;">Sentry Mode</h2>
          <p style="margin:0; color:var(--text-muted);">Monitor the parked vehicle and review movement captures.</p>
        </div>
        <span class="gx-chip">{{ statusText }}</span>
      </div>

      <GalaxySection title="Configuration" icon="bi-sliders" :collapsible="false">
        <div style="padding: var(--sp-3);">
          <div v-if="loading" class="gx-loading">Loading Sentry settings...</div>
          <template v-else>
            <div class="gx-row" style="border-top:none;">
              <div class="gx-row__info">
                <span class="gx-row__label"><strong>Enable Sentry Mode</strong></span>
                <span class="gx-row__desc">Detect sustained movement while the vehicle is parked.</span>
              </div>
              <label class="gx-switch">
                <input type="checkbox" :checked="!!params.SentryModeEnabled" :disabled="savingKey === 'SentryModeEnabled'"
                  @change="saveParam('SentryModeEnabled', $event.target.checked)" />
                <span class="gx-switch__track"></span>
                <span class="gx-switch__thumb"></span>
              </label>
            </div>

            <div style="padding: var(--sp-2) 0;">
              <div class="gx-row__label">Webhook URL</div>
              <input class="gx-field gx-field--full" type="url" :value="params.SentryModeWebhook || ''"
                placeholder="https://..." :disabled="savingKey === 'SentryModeWebhook'"
                @change="saveParam('SentryModeWebhook', $event.target.value.trim())" />
              <div class="gx-row__desc">Optional Discord-compatible or custom webhook.</div>
            </div>

            <div style="padding: var(--sp-2) 0;">
              <div class="gx-row__label">ntfy URL</div>
              <input class="gx-field gx-field--full" type="url" :value="params.SentryModeNtfyUrl || ''"
                placeholder="https://ntfy.sh/..." :disabled="savingKey === 'SentryModeNtfyUrl'"
                @change="saveParam('SentryModeNtfyUrl', $event.target.value.trim())" />
              <div class="gx-row__desc">Optional ntfy topic URL for phone notifications.</div>
            </div>

            <div class="gx-row" style="border-top:none; align-items:stretch; flex-direction:column; gap:var(--sp-2);">
              <div class="gx-row__label"><strong>Motion sensitivity</strong></div>
              <div class="gx-slider-row">
                <span class="gx-row__value">{{ numeric('SentryModeSensitivity', 0.04).toFixed(3) }}</span>
                <input type="range" class="gx-slider" min="0.005" max="1" step="0.001" :value="numeric('SentryModeSensitivity', 0.04)"
                  :disabled="savingKey === 'SentryModeSensitivity'" aria-label="Motion sensitivity"
                  @change="saveParam('SentryModeSensitivity', Number($event.target.value))" />
              </div>
              <div class="gx-row__desc">Lower values detect smaller acceleration changes. Default: 0.04.</div>
            </div>

            <div class="gx-row" style="border-top:none; align-items:stretch; flex-direction:column; gap:var(--sp-2);">
              <div class="gx-row__label"><strong>Warning persistence</strong></div>
              <div class="gx-slider-row">
                <span class="gx-row__value">{{ numeric('SentryModeWarningTime', 1).toFixed(1) }} seconds</span>
                <input type="range" class="gx-slider" min="0.1" max="10" step="0.1" :value="numeric('SentryModeWarningTime', 1)"
                  :disabled="savingKey === 'SentryModeWarningTime'" aria-label="Warning persistence"
                  @change="saveParam('SentryModeWarningTime', Number($event.target.value))" />
              </div>
              <div class="gx-row__desc">How long movement must continue before the first alert. Default: 1 second.</div>
            </div>

            <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:var(--sp-2);">
              <button type="button" class="gx-btn" :disabled="pushBusy" @click="enablePush">
                <i class="bi bi-bell"></i> {{ pushBusy ? 'Enabling...' : 'Enable browser notifications' }}
              </button>
              <button type="button" class="gx-btn gx-btn--tonal" :disabled="pushBusy" @click="sendTestNotification">
                {{ pushBusy ? 'Sending...' : 'Send test notification' }}
              </button>
            </div>
            <p class="gx-note">The test notification uses every configured channel: browser Web Push, ntfy, and webhook.</p>
            <p class="gx-note">iPhone users: add Galaxy to your Home Screen as a web app before enabling notifications.</p>
          </template>
        </div>
      </GalaxySection>

      <GalaxySection title="Live view" icon="bi-camera" :collapsible="false">
        <div style="padding: var(--sp-3);">
          <div class="gx-row" style="border-top:none;">
            <span class="gx-row__label">Capture one still from both cameras while parked.</span>
            <span class="gx-row__value">
              <button type="button" class="gx-btn gx-btn--tonal" :disabled="liveBusy" @click="viewLive">
                <i class="bi bi-camera"></i> {{ liveBusy ? 'Capturing...' : 'View live' }}
              </button>
            </span>
          </div>
          <template v-if="Array.isArray(liveCapture.imageUrls) && liveCapture.imageUrls.length">
            <p class="gx-row__desc">Captured {{ liveCapture.capturedAt || 'just now' }}.</p>
            <div style="display:flex; flex-wrap:wrap; gap:8px;">
              <a v-for="(u, i) in liveCapture.imageUrls" :key="u + i" :href="liveImageUrl(u)" target="_blank" rel="noopener" style="flex:1 1 45%; min-width:120px;">
                <img :src="liveImageUrl(u)" :alt="'Live Sentry camera ' + (i + 1)" style="width:100%; border-radius:8px; display:block;" />
              </a>
            </div>
          </template>
          <p v-else class="gx-empty">No live snapshot captured yet.</p>
        </div>
      </GalaxySection>

      <GalaxySection title="Latest Event" icon="bi-shield-exclamation" :collapsible="false">
        <div style="padding: var(--sp-3);">
          <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:var(--sp-2);">
            <button type="button" class="gx-btn gx-btn--tonal" :disabled="historyBusy" @click="toggleHistory">
              {{ historyBusy ? 'Loading...' : (historyVisible ? 'Hide history' : 'View history') }}
            </button>
            <button type="button" class="gx-btn gx-btn--tonal" :disabled="testBusy" @click="sendTestEvent">
              {{ testBusy ? 'Capturing...' : 'Send test capture' }}
            </button>
            <button v-if="hasEvent" type="button" class="gx-btn gx-btn--danger" :disabled="deleteBusy" @click="deleteEvent(event.eventId)">
              {{ deleteBusy ? 'Deleting...' : 'Delete event' }}
            </button>
          </div>
          <p class="gx-row__desc">The latest event refreshes every five seconds. History updates when you refresh it.</p>

          <template v-if="hasEvent">
            <div style="display:flex; align-items:center; gap:8px; margin-top:var(--sp-2);">
              <span class="gx-chip" :style="{ color: kindColor(event.kind) }">{{ kindLabel(event.kind) }}</span>
              <span class="gx-row__desc" style="margin:0;">{{ event.detectedAt || '' }}</span>
            </div>
            <p style="margin:8px 0;"><strong>{{ event.message || 'Movement detected while parked.' }}</strong></p>
            <template v-if="Array.isArray(event.imageUrls) && event.imageUrls.length">
              <div style="display:flex; flex-wrap:wrap; gap:8px;">
                <a v-for="(u, i) in event.imageUrls" :key="u + i" :href="u" target="_blank" rel="noopener" style="flex:1 1 45%; min-width:120px;">
                  <img :src="u" :alt="'Sentry capture ' + (i + 1)" loading="lazy" style="width:100%; border-radius:8px; display:block;" />
                </a>
              </div>
            </template>
            <p v-else-if="event.kind === 'power_off'" class="gx-empty">Power-off alerts do not include camera captures because the device is shutting down.</p>
            <p v-else class="gx-empty">No camera images were available for this event.</p>
          </template>
          <p v-else-if="!loading" class="gx-empty">No Sentry events recorded yet.</p>

          <div v-if="historyVisible" style="margin-top:var(--sp-3); border-top:1px solid var(--border-color, rgba(255,255,255,.08)); padding-top:var(--sp-2);">
            <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:flex-end; margin-bottom:var(--sp-2);">
              <label style="flex:1 1 140px;">
                <div class="gx-row__desc" style="margin:0 0 4px;">From</div>
                <input class="gx-field gx-field--full" type="date" :value="dateFrom" :max="dateTo || null"
                  @change="dateFrom = $event.target.value; applyFilter()" />
              </label>
              <label style="flex:1 1 140px;">
                <div class="gx-row__desc" style="margin:0 0 4px;">To</div>
                <input class="gx-field gx-field--full" type="date" :value="dateTo" :min="dateFrom || null"
                  @change="dateTo = $event.target.value; applyFilter()" />
              </label>
              <button v-if="filterActive" type="button" class="gx-btn gx-btn--tonal" @click="clearFilter">Clear</button>
              <button v-if="historyTotal" type="button" class="gx-btn gx-btn--danger" :disabled="deleteBusy || historyBusy" @click="deleteAllHistory">
                {{ deleteBusy ? 'Deleting...' : (filterActive ? 'Delete matching' : 'Delete all') }}
              </button>
            </div>
            <div v-if="history.length" style="display:flex; gap:8px; flex-wrap:wrap; align-items:flex-end; margin-bottom:var(--sp-2);">
              <label style="flex:1 1 110px;">
                <div class="gx-row__desc" style="margin:0 0 4px;">Camera</div>
                <select class="gx-field gx-field--full" v-model="timelapseCamera" :disabled="timelapseBusy">
                  <option value="wide">Road</option>
                  <option value="driver">Driver</option>
                  <option value="both">Both</option>
                </select>
              </label>
              <label style="flex:1 1 90px;">
                <div class="gx-row__desc" style="margin:0 0 4px;">Format</div>
                <select class="gx-field gx-field--full" v-model="timelapseFormat" :disabled="timelapseBusy">
                  <option value="mp4">MP4</option>
                  <option value="gif">GIF</option>
                </select>
              </label>
              <button type="button" class="gx-btn gx-btn--tonal" :disabled="timelapseBusy || deleteBusy" @click="makeTimelapse">
                {{ timelapseBusy ? 'Encoding...' : (filterActive ? 'Timelapse of range' : 'Make timelapse') }}
              </button>
            </div>
            <div v-if="historyBusy && !history.length" class="gx-loading">Loading Sentry history...</div>
            <template v-else-if="history.length">
              <p class="gx-row__desc">Showing {{ history.length }} of {{ historyTotal }} event{{ historyTotal === 1 ? '' : 's' }}{{ filterActive ? ' in this date range' : '' }}. Events stay here until you delete them.</p>
              <button v-if="newEvents" type="button" class="gx-btn gx-btn--tonal" :disabled="historyBusy" @click="loadHistory()">New event - refresh</button>
              <article v-for="ev in history" :key="ev.eventId" style="border:1px solid var(--glass-border, rgba(255,255,255,.1)); border-radius:12px; padding:var(--sp-3); margin:var(--sp-2) 0;">
                <div style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
                  <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                    <strong>{{ ev.detectedAt || 'Sentry event' }}</strong>
                    <span class="gx-chip" :style="{ color: kindColor(ev.kind) }">{{ kindLabel(ev.kind) }}</span>
                  </div>
                  <button type="button" class="gx-icon-btn" :disabled="deleteBusy" @click="deleteEvent(ev.eventId)" title="Delete event" style="color:var(--error);">
                    <i class="bi bi-trash"></i>
                  </button>
                </div>
                <p style="margin:8px 0;"><strong>{{ ev.message || 'Movement detected while parked.' }}</strong></p>
                <template v-if="Array.isArray(ev.imageUrls) && ev.imageUrls.length">
                  <div style="display:flex; flex-wrap:wrap; gap:8px;">
                    <a v-for="(u, i) in ev.imageUrls" :key="u + i" :href="u" target="_blank" rel="noopener" style="flex:1 1 45%; min-width:120px;">
                      <img :src="u" :alt="'Sentry capture ' + (i + 1)" loading="lazy" style="width:100%; border-radius:8px; display:block;" />
                    </a>
                  </div>
                </template>
                <p v-else class="gx-empty">No camera images were available for this event.</p>
              </article>
              <div v-if="historyHasMore" ref="sentinel" style="text-align:center; padding:var(--sp-2) 0;">
                <button v-if="historyError" type="button" class="gx-btn gx-btn--tonal" @click="loadHistory({ append: true })">
                  Retry loading more
                </button>
                <span v-else class="gx-row__desc">{{ historyBusy ? 'Loading...' : history.length + ' of ' + historyTotal }}</span>
              </div>
            </template>
            <p v-else class="gx-empty">{{ filterActive ? 'No Sentry events in this date range.' : 'No retained Sentry events.' }}</p>
          </div>
        </div>
      </GalaxySection>
    </div>
  `,
}
