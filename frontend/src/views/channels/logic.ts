// Pure channels-view helpers ported 1:1 from the legacy view
// (src/agentos/gateway/static/js/views/channels.js). Each function below carries
// the legacy line range it mirrors so the parity matrix stays auditable. RPC
// calls, event subscriptions, and rendering live in ChannelsPage.tsx; this
// module owns the pure derivations (merge/sort, stats, status mapping, sender
// formatting).

/** A raw channel row from channels.status (all fields optional). */
export interface RawChannel {
  name?: string
  id?: string
  type?: string
  status?: string
  connected?: boolean
  connected_since?: string | number
  restart_attempts?: number
  enabled?: boolean
  configured?: boolean
  [key: string]: unknown
}

/** One access account (pending or approved) from channels.access.list. */
export interface AccessAccount {
  sender_id?: string | number
  username?: string
  display_name?: string
  code?: string
  expires_at?: string | number
  source?: string
  [key: string]: unknown
}

/** Per-channel access entry from channels.access.list. */
export interface ChannelAccess {
  name?: string
  mode?: string
  group_mode?: string
  locked_until?: number
  pending?: AccessAccount[]
  approved?: AccessAccount[]
  [key: string]: unknown
}

/** A channel merged with its access entry (channels.js:93-95). */
export interface MergedChannel extends RawChannel {
  access: ChannelAccess | null
}

export type Tone = 'ok' | 'danger' | 'off'

/**
 * channels.js:85-90,93-95 — filter out channels explicitly configured:false,
 * then attach each channel's access entry keyed by name (null when unmatched).
 */
export function mergeChannels(
  channels: RawChannel[] | undefined,
  access: ChannelAccess[] | undefined,
): MergedChannel[] {
  const accessByName = new Map<string, ChannelAccess>(
    (access || []).map((item) => [String(item.name || ''), item]),
  )
  return (channels || [])
    .filter((c) => c && c.configured !== false)
    .map((item) => ({
      ...item,
      access: accessByName.get(String(item.name || '')) || null,
    }))
}

// channels.js:92 — operator-urgency status order; unknown statuses fall to 1.
export const STATUS_ORDER: Record<string, number> = {
  running: 0,
  connected: 0,
  restarting: 1,
  exhausted: 1,
  dead: 1,
  stopped: 2,
  disabled: 3,
}

/**
 * channels.js:96-103 — sort by pending-access count desc, then status urgency.
 * Non-mutating (operates on a copy) so callers can pass query data directly.
 */
export function sortChannels(channels: MergedChannel[]): MergedChannel[] {
  return [...channels].sort((a, b) => {
    const pendingA = Number(a.access?.pending?.length || 0)
    const pendingB = Number(b.access?.pending?.length || 0)
    if (pendingA !== pendingB) return pendingB - pendingA
    const oa = STATUS_ORDER[String(a.status ?? '')] ?? 1
    const ob = STATUS_ORDER[String(b.status ?? '')] ?? 1
    return oa - ob
  })
}

/** channels.js:398-400 — statuses that need operator attention. */
export function needsAttention(status: string | undefined): boolean {
  return status === 'dead' || status === 'restarting' || status === 'exhausted'
}

export interface ChannelStats {
  total: number
  connected: number
  attention: number
  inactive: number
  disabled: number
  restarts: number
  pendingAccess: number
  typeCount: number
}

/** channels.js:113-121 — derive the stat-row numbers from the merged channels. */
export function channelStats(channels: MergedChannel[]): ChannelStats {
  const total = channels.length
  const connected = channels.filter(
    (c) => c.status === 'running' || c.status === 'connected',
  ).length
  const attention = channels.filter((c) => needsAttention(c.status)).length
  const inactive = total - connected - attention
  const disabled = channels.filter((c) => c.status === 'disabled').length
  const restarts = channels.reduce((acc, c) => acc + (Number(c.restart_attempts) || 0), 0)
  const pendingAccess = channels.reduce((acc, c) => acc + (c.access?.pending?.length || 0), 0)
  const types = new Set<string>()
  channels.forEach((c) => {
    if (c.type) types.add(c.type)
  })
  return {
    total,
    connected,
    attention,
    inactive,
    disabled,
    restarts,
    pendingAccess,
    typeCount: types.size,
  }
}

/** channels.js:402-406 — Inactive-tile hint text. */
export function inactiveHint(inactive: number, disabled: number): string {
  if (!inactive) return 'no inactive channels'
  if (disabled) return `${disabled} disabled`
  return 'configured but idle'
}

export interface ChannelDisplay {
  name: string
  status: string
  isRunning: boolean
  isDead: boolean
  tone: Tone
  attempts: string
  configJson: string
}

/**
 * channels.js:203-217,388-396 — resolve the display fields for one channel
 * card: effective status, the ok/danger/off tone (status color ONLY via
 * --tone), restart-attempt string, and the pretty-printed adapter config.
 */
export function channelDisplay(ch: MergedChannel): ChannelDisplay {
  const name = String(ch.name || ch.id || 'Unknown')
  const status = String(ch.status || (ch.connected ? 'connected' : 'stopped'))
  const isRunning = status === 'running' || status === 'connected'
  const isDead = status === 'dead'
  const tone: Tone = isRunning ? 'ok' : isDead ? 'danger' : 'off'
  const attempts = ch.restart_attempts != null ? String(ch.restart_attempts) : '0'
  let configJson: string
  try {
    configJson = JSON.stringify(ch, null, 2)
  } catch {
    configJson = String(ch)
  }
  return { name, status, isRunning, isDead, tone, attempts, configJson }
}

/** channels.js:388-396 — the status-hint footnote for a channel card. */
export function statusHint(args: {
  status: string
  isRunning: boolean
  isDead: boolean
  enabled: boolean
  name: string
}): string {
  const safeName = args.name || '<name>'
  if (!args.enabled)
    return 'Disabled in config — gateway restart required after re-enabling. Run `agentos onboard configure channels` to change.'
  if (args.isDead)
    return `Adapter is dead. Inspect gateway logs, then \`agentos channels restart ${safeName}\`.`
  if (args.isRunning) return 'Adapter is live in the current gateway process.'
  if (args.status === 'restarting') return 'Adapter is restarting after dispatch errors.'
  if (args.status === 'exhausted')
    return `Adapter exhausted its retry budget. Try \`agentos channels restart ${safeName}\`.`
  return 'Configured on disk but not active in this gateway process — restart the gateway to load it.'
}

const VALID_ACCESS_MODES = new Set(['pairing', 'allowlist', 'open', 'disabled'])

/** channels.js:251-252 — validate the access mode, defaulting invalid→pairing. */
export function resolveAccessMode(mode: string | undefined): string {
  return VALID_ACCESS_MODES.has(String(mode ?? '')) ? String(mode) : 'pairing'
}

/** channels.js:253 — pairing approval is locked while locked_until*1000 > now. */
export function isAccessLocked(lockedUntil: number | undefined, now: number = Date.now()): boolean {
  return Number(lockedUntil || 0) * 1000 > now
}

/** channels.js:373-377 — the primary label for a Telegram account. */
export function senderLabel(item: AccessAccount): string {
  if (item.username) return '@' + item.username
  if (item.display_name) return item.display_name
  return 'Telegram user ' + (item.sender_id || 'unknown')
}

/** channels.js:379-386 — the secondary meta line (bits joined with " · "). */
export function senderMeta(item: AccessAccount): string {
  const bits: string[] = []
  if (item.display_name && item.display_name !== senderLabel(item)) bits.push(item.display_name)
  if (item.sender_id) bits.push('ID ' + item.sender_id)
  if (item.expires_at)
    bits.push('expires ' + new Date(Number(item.expires_at) * 1000).toLocaleTimeString())
  if (item.source) bits.push(item.source)
  return bits.join(' · ')
}
