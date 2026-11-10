// Pure overview helpers, first ported 1:1 from the retired console's
// overview.js view and its components.js helpers (relTime / sessionStatus*);
// the line ranges cited below point into that source. RPC calls, event
// subscriptions and rendering live in the callers (the web console's Home page
// and Settings › Channels section, the desktop's Channels pane); this module
// owns the pure derivations (label mapping, formatting, session sort).

// Registers this view's copy; it ships in this chunk, not the entry bundle.
import '@/i18n/en/overview'
import { t } from '@/i18n'

/** A recent session row as returned by sessions.list (all fields optional). */
export interface OverviewSession {
  key?: string
  status?: string
  model?: string
  message_count?: number
  updated_at?: string
}

/** — readiness status -> human label. Known tokens map
 * directly; anything else is Title-cased by splitting _/- separators. Empty /
 * nullish falls back to "Unknown". */
export function readinessStatusLabel(status?: string | null): string {
  const labels: Record<string, string> = {
    ready: t('overview.readyReady'),
    degraded: t('overview.readyDegraded'),
    action_required: t('overview.readyActionRequired'),
    unavailable: t('overview.readyUnavailable'),
    unknown: t('overview.readyUnknown'),
  }
  const key = String(status || 'unknown').toLowerCase()
  if (labels[key]) return labels[key]
  return key.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

/** — uptime_ms -> "Hh Mm Ss"; null/undefined -> "—". */
export function formatUptime(ms?: number | null): string {
  if (ms == null) return t('common.dash')
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return t('overview.uptime', { hours: h, minutes: m, seconds: s % 60 })
}

// session status -> dot color variant / tooltip label.
const SESSION_STATUS_DOT: Record<string, string> = {
  running: 'ok',
  done: 'off',
  failed: 'err',
  killed: 'off',
  timeout: 'warn',
}

/** — dot color variant ("ok"/"warn"/"err"/"off"). */
export function sessionStatusClass(status?: string | null): string {
  const k = String(status || '').toLowerCase()
  return SESSION_STATUS_DOT[k] || 'off'
}

/** — tooltip label; raw string else "Unknown" when empty. */
export function sessionStatusLabel(status?: string | null): string {
  const labels: Record<string, string> = {
    running: t('overview.sessionRunning'),
    done: t('overview.sessionDone'),
    failed: t('overview.sessionFailed'),
    killed: t('overview.sessionKilled'),
    timeout: t('overview.sessionTimeout'),
  }
  const k = String(status || '').toLowerCase()
  return labels[k] || (status ? String(status) : t('overview.sessionUnknown'))
}

/** — relative time. Numeric input is treated as an epoch
 * (seconds when < 1e10, else millis); strings parse as ISO. Invalid -> "—". */
export function relTime(isoOrTs: string | number): string {
  const numeric =
    typeof isoOrTs === 'number'
      ? isoOrTs
      : typeof isoOrTs === 'string' && isoOrTs.trim() !== ''
        ? Number(isoOrTs)
        : NaN
  const d = Number.isFinite(numeric)
    ? new Date(Math.abs(numeric) < 10_000_000_000 ? numeric * 1000 : numeric)
    : new Date(isoOrTs)
  if (Number.isNaN(d.getTime())) return t('common.dash')
  const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 60) return t('overview.relJustNow')
  if (diff < 3600) return t('overview.relMinutes', { count: Math.floor(diff / 60) })
  if (diff < 86_400) return t('overview.relHours', { count: Math.floor(diff / 3600) })
  return t('overview.relDays', { count: Math.floor(diff / 86_400) })
}

/** — JSON-stringify a payload, truncating at 80 chars with
 * an ellipsis; fall back to String when serialization throws. */
export function formatEventPayload(payload: unknown): string {
  let payloadStr = ''
  try {
    payloadStr = JSON.stringify(payload)
    if (payloadStr.length > 80) payloadStr = payloadStr.slice(0, 80) + '…'
  } catch {
    payloadStr = String(payload)
  }
  return payloadStr
}

/** — a Date -> "HH:MM:SS" (local time). */
export function formatEventTs(now: Date): string {
  return now.toTimeString().slice(0, 8)
}

/** — sort sessions by updated_at descending (missing ->
 * epoch 0) and slice to the first 6. Never mutates the input. */
export function sortRecentSessions(sessions: OverviewSession[]): OverviewSession[] {
  return sessions
    .slice()
    .sort((a, b) => {
      const ta = a.updated_at ? new Date(a.updated_at).getTime() : 0
      const tb = b.updated_at ? new Date(b.updated_at).getTime() : 0
      return tb - ta
    })
    .slice(0, 6)
}

/** — localized token count; null/undefined -> "—". */
export function formatTokens(total?: number | null): string {
  return total != null ? total.toLocaleString() : t('common.dash')
}

/** — total cost as "$X.XXXX"; null/undefined -> "—". */
export function formatCost(usd?: number | null): string {
  return usd != null ? '$' + Number(usd).toFixed(4) : t('common.dash')
}
