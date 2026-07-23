// Pure overview-view helpers ported 1:1 from the legacy view
// (src/agentos/gateway/static/js/views/overview.js) and the shared UI helpers
// it consumes (static/js/components.js relTime / sessionStatus*). Each function
// carries the legacy line range it mirrors so the parity matrix stays auditable.
// RPC calls, event subscriptions, and rendering live in OverviewPage.tsx; this
// module owns the pure derivations (label mapping, formatting, session sort).

/** A recent session row as returned by sessions.list (all fields optional). */
export interface OverviewSession {
  key?: string
  status?: string
  model?: string
  message_count?: number
  updated_at?: string
}

/** overview.js:352-365 — readiness status -> human label. Known tokens map
 *  directly; anything else is Title-cased by splitting _/- separators. Empty /
 *  nullish falls back to "Unknown". */
export function readinessStatusLabel(status?: string | null): string {
  const labels: Record<string, string> = {
    ready: 'Ready',
    degraded: 'Degraded',
    action_required: 'Action required',
    unavailable: 'Unavailable',
    unknown: 'Unknown',
  }
  const key = String(status || 'unknown').toLowerCase()
  if (labels[key]) return labels[key]
  return key.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

/** overview.js:234-242 — uptime_ms -> "Hh Mm Ss"; null/undefined -> "—". */
export function formatUptime(ms?: number | null): string {
  if (ms == null) return '—'
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h ${m}m ${s % 60}s`
}

// components.js:249-269 — session status -> dot color variant / tooltip label.
const SESSION_STATUS_DOT: Record<string, string> = {
  running: 'ok',
  done: 'off',
  failed: 'err',
  killed: 'off',
  timeout: 'warn',
}
const SESSION_STATUS_LABEL: Record<string, string> = {
  running: 'Running',
  done: 'Completed',
  failed: 'Failed',
  killed: 'Aborted by operator',
  timeout: 'Timed out',
}

/** components.js:272-275 — dot color variant ("ok"/"warn"/"err"/"off"). */
export function sessionStatusClass(status?: string | null): string {
  const k = String(status || '').toLowerCase()
  return SESSION_STATUS_DOT[k] || 'off'
}

/** components.js:284-287 — tooltip label; raw string else "Unknown" when empty. */
export function sessionStatusLabel(status?: string | null): string {
  const k = String(status || '').toLowerCase()
  return SESSION_STATUS_LABEL[k] || (status ? String(status) : 'Unknown')
}

/** components.js:228-241 — relative time. Numeric input is treated as an epoch
 *  (seconds when < 1e10, else millis); strings parse as ISO. Invalid -> "—". */
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
  if (Number.isNaN(d.getTime())) return '—'
  const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86_400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86_400)}d ago`
}

/** overview.js:320-326 — JSON-stringify a payload, truncating at 80 chars with
 *  an ellipsis; fall back to String() when serialization throws. */
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

/** overview.js:318-319 — a Date -> "HH:MM:SS" (local time). */
export function formatEventTs(now: Date): string {
  return now.toTimeString().slice(0, 8)
}

/** overview.js:274-281 — sort sessions by updated_at descending (missing ->
 *  epoch 0) and slice to the first 6. Never mutates the input. */
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

/** overview.js:263 — localized token count; null/undefined -> "—". */
export function formatTokens(total?: number | null): string {
  return total != null ? total.toLocaleString() : '—'
}

/** overview.js:266-268 — total cost as "$X.XXXX"; null/undefined -> "—". */
export function formatCost(usd?: number | null): string {
  return usd != null ? '$' + Number(usd).toFixed(4) : '—'
}
