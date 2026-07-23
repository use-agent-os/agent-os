// Chat view — pure logic helpers ported verbatim from the legacy
// static/js/views/chat.js. Every function here is pure and side-effect free:
// URL / storage inputs are injected as strings rather than read off `window`,
// so each helper is unit-testable in isolation. Cited legacy line ranges are
// against static/js/views/chat.js.

import {
  isApprovalBypassMode,
  normalizeElevatedMode,
  type ElevatedMode,
} from '@/services/approval-monitor'
import { artifactDownloadUrl, type Artifact } from './transcript/artifacts'
import type { ChatMessage, Role } from './types'

// The elevated-mode model is SHARED with the approvals view: the storage keys,
// version-downgrade reader, normalizer, and bypass predicate all live in the
// single source of truth (services/approval-monitor.ts). The chat toolbar
// re-exports the pieces it needs so callers can import from one module, but the
// implementations are NOT duplicated here — there is exactly one elevated-mode
// store.
export { isApprovalBypassMode, normalizeElevatedMode }
export type { ElevatedMode }

// The stable webchat session key (chat.js:11).
const WEBCHAT_SESSION_KEY = 'agent:main:webchat:default'

/**
 * Normalize an agent id (chat.js:1138-1143). Lowercased, non-`[a-z0-9_-]`
 * collapsed to `-`, leading/trailing `-` trimmed; empty or `default` → `main`.
 */
export function normalizeAgentId(agentId: string): string {
  const raw = String(agentId ?? '')
    .trim()
    .toLowerCase()
  if (!raw || raw === 'default') return 'main'
  const normalized = raw.replace(/[^a-z0-9_-]/g, '-').replace(/^-+|-+$/g, '')
  return normalized && normalized !== 'default' ? normalized : 'main'
}

/**
 * Extract the agent id from a session key (chat.js:1145-1149). A non-`agent:`
 * key → `main`; otherwise segment [1] normalized.
 */
export function agentIdFromSessionKey(key: string): string {
  const value = String(key ?? '').trim()
  if (!value.startsWith('agent:')) return 'main'
  return normalizeAgentId(value.split(':')[1] || 'main')
}

/**
 * Build a webchat session key for an agent (chat.js:1151-1153).
 */
export function webchatSessionKey(agentId: string, suffix = 'default'): string {
  return 'agent:' + normalizeAgentId(agentId) + ':webchat:' + suffix
}

/**
 * Canonicalize a session key / alias to the stable key (chat.js:1159-1165).
 * Empty / `default` / `webchat:default` → the stable webchat key; an
 * `agent:default:` prefix is rewritten to `agent:main:`; a legacy `sess-`
 * prefix becomes an `agent:main:webchat:` key; anything else passes through.
 */
export function canonicalSessionKey(key: string): string {
  const value = (key ?? '').trim()
  if (!value || value === 'default' || value === 'webchat:default') return WEBCHAT_SESSION_KEY
  if (value.startsWith('agent:default:'))
    return 'agent:main:' + value.slice('agent:default:'.length)
  if (value.startsWith('sess-')) return 'agent:main:webchat:' + value.slice('sess-'.length)
  return value
}

/** chat.js:1173 — the localStorage key the active session persists under. */
export const ACTIVE_SESSION_STORAGE_KEY = 'agentos_active_session'

/**
 * A session-list item is either a bare key string or an object carrying the
 * key under `key` / `session` / `sessionKey` (chat.js:1858 `_itemKey`).
 */
export type SessionListItem =
  | string
  | {
      key?: string
      session?: string
      sessionKey?: string
      channel_kind?: string
      channelKind?: string
      channel?: string
      source_kind?: string
      sourceKind?: string
      // Run-status fields (chat.js:1611 reads these off the item too).
      run_status?: string
      runStatus?: string
      active_task?: RunTask | null
      activeTask?: RunTask | null
      last_task?: RunTask | null
      lastTask?: RunTask | null
      [k: string]: unknown
    }

/** Extract the key from a session-list item (chat.js:1858-1860 `_itemKey`). */
export function sessionItemKey(item: SessionListItem): string {
  if (typeof item === 'string') return item
  return item.key || item.session || item.sessionKey || ''
}

/**
 * The switcher-group buckets (chat.js:1903 group order). A session item that
 * classifies to `null` (empty / `unknown` key) is dropped from the list.
 */
export type SessionGroup = 'Web chat' | 'CLI' | 'Sub-agents' | 'Agents' | 'Sessions' | 'Other'

/**
 * Bucket a session item into a switcher group (chat.js:1862-1881 `_classifyKey`).
 * An explicit channel/source kind wins; otherwise the key's shape decides.
 * Returns null for an empty / `unknown` key (so it is skipped in the list).
 */
export function classifySessionKey(item: SessionListItem): SessionGroup | null {
  const key = sessionItemKey(item)
  if (!key || key === 'unknown') return null
  const obj = typeof item === 'object' && item ? item : null
  const channelKind = obj ? obj.channel_kind || obj.channelKind || obj.channel || '' : ''
  const sourceKind = obj ? obj.source_kind || obj.sourceKind || '' : ''
  if (channelKind === 'webchat' || sourceKind === 'webui') return 'Web chat'
  if (channelKind === 'cli' || sourceKind === 'cli') return 'CLI'
  if (key.startsWith('agent:')) {
    if (key.includes(':webchat')) return 'Web chat'
    if (key.includes(':cli:') || key.includes(':standalone:')) return 'CLI'
    if (key.includes(':subagent')) return 'Sub-agents'
    return 'Agents'
  }
  if (key.startsWith('sess-')) return 'Sessions'
  return 'Other'
}

/* ── Run status (chat.js:1571-1621) ──────────────────────────────────────── */

/** A run task carried on a session/task payload (chat.js:1613-1619). */
export interface RunTask {
  status?: string
  task_id?: string
  terminal_reason?: string
  terminalReason?: string
  queue_position?: number
  queuePosition?: number
  [k: string]: unknown
}

/** The normalized run status vocabulary (chat.js:1591). */
export type RunStatus =
  | 'idle'
  | 'queued'
  | 'running'
  | 'approval_pending'
  | 'interrupted'
  | 'failed'
  | 'timeout'
  | 'cancelled'

/** The source a run status is derived from (chat.js:1611-1620). */
export interface RunStatusSource {
  run_status?: string
  runStatus?: string
  active_task?: RunTask | null
  activeTask?: RunTask | null
  last_task?: RunTask | null
  lastTask?: RunTask | null
  [k: string]: unknown
}

/** The resolved run status (chat.js:1620). */
export interface RunStatusResult {
  status: RunStatus
  label: string
  task: RunTask | null
}

/** chat.js:1571-1583 — the human label for a normalized run status. */
export function runStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: 'Queued',
    running: 'Running',
    approval_pending: 'Waiting for approval',
    interrupted: 'Interrupted',
    failed: 'Failed',
    timeout: 'Timed out',
    cancelled: 'Cancelled',
    idle: 'Idle',
  }
  return labels[status] || 'Idle'
}

/** chat.js:1585-1595 — collapse legacy synonyms onto the normalized vocabulary. */
export function normalizeRunStatus(status: unknown): RunStatus {
  const value = String(status || '').toLowerCase()
  if (value === 'abandoned') return 'interrupted'
  if (value === 'killed') return 'cancelled'
  if (value === 'waiting for approval') return 'approval_pending'
  if (value === 'succeeded' || value === 'success' || value === 'complete') return 'idle'
  if (
    [
      'queued',
      'running',
      'approval_pending',
      'interrupted',
      'failed',
      'timeout',
      'cancelled',
    ].includes(value)
  ) {
    return value as RunStatus
  }
  return 'idle'
}

/**
 * chat.js:1600-1609 — the chip color class for the header run-status pill. Idle
 * and cancelled stay muted (plain chip) so finished sessions don't compete for
 * attention; the rest map to warn / ok / danger tones.
 */
export function runStatusChipClass(status: string): string {
  const map: Record<string, string> = {
    queued: 'chip-warn',
    running: 'chip-ok',
    approval_pending: 'chip-warn',
    interrupted: 'chip-warn',
    failed: 'chip-danger',
    timeout: 'chip-warn',
  }
  return map[status] || ''
}

/**
 * chat.js:1611-1621 `_sessionRunStatus` — derive `{ status, label, task }` from
 * a session/task source. `run_status` (camel or snake) is the base, falling back
 * to the active/last task's status; an active task that is queued/running/
 * approval_pending overrides the base status (so a live turn shows through even
 * when `run_status` lags at idle). The winning task is `active || last || null`.
 */
export function sessionRunStatus(source: RunStatusSource | null | undefined): RunStatusResult {
  const src = source || {}
  const active = src.active_task || src.activeTask || null
  const last = src.last_task || src.lastTask || null
  const activeStatus = active ? normalizeRunStatus(active.status) : ''
  const rawStatus = src.run_status || src.runStatus || active?.status || last?.status || ''
  let status = normalizeRunStatus(rawStatus)
  if (active && ['queued', 'running', 'approval_pending'].includes(activeStatus)) {
    status = activeStatus as RunStatus
  }
  const task = active || last || null
  return { status, label: runStatusLabel(status), task }
}

/** chat.js:1694-1701 — terminal session-change predicate, including run-status-only frames. */
export function sessionChangeIsTerminal(payload: RunStatusSource | null | undefined): boolean {
  const source = (payload || {}) as RunStatusSource & { reason?: unknown; status?: unknown }
  const reason = String(source.reason || '').toLowerCase()
  if (reason === 'turn_complete' || reason === 'task_terminal') return true
  const lifecycle = String(source.status || '').toLowerCase()
  if (['done', 'failed', 'killed', 'timeout'].includes(lifecycle)) return true
  const runStatus = normalizeRunStatus(source.run_status || source.runStatus)
  return ['failed', 'timeout', 'cancelled', 'interrupted'].includes(runStatus)
}

/** chat.js:5300-5303 — only unexpected replay gaps warrant a user-facing warning. */
export function replayGapShouldWarn(reason: unknown): boolean {
  const value = String(reason || '').toLowerCase()
  return !['stream_buffer_empty', 'stream_buffer_reset', 'cursor_ahead_of_stream'].includes(value)
}

/** chat.js:1703-1711 — idle terminal subscribe snapshots need a history refresh. */
export function subscribeResultNeedsTerminalHistorySync(
  source: (RunStatusSource & { replayed_count?: unknown }) | null | undefined,
): boolean {
  if (!source || Number(source.replayed_count || 0) > 0) return false
  const state = sessionRunStatus(source)
  if (state.status !== 'idle' || !state.task) return false
  const taskStatus = String(state.task.status || '').toLowerCase()
  const terminalReason = String(
    state.task.terminal_reason || state.task.terminalReason || '',
  ).toLowerCase()
  return (
    ['succeeded', 'success', 'complete', 'completed', 'done'].includes(taskStatus) ||
    terminalReason === 'completed'
  )
}

/**
 * Read `?session=` from a search string (chat.js:1182-1187), pure over the
 * injected search rather than `window.location.search`. Returns the value or
 * `null` when absent / unparseable (legacy returns '' from `_readSessionFromUrl`;
 * the caller treats falsy as "no session", so `null` is the faithful pure form).
 */
export function readSessionFromUrl(search: string): string | null {
  try {
    const params = new URLSearchParams(search)
    return params.get('session')
  } catch {
    return null
  }
}

/**
 * Read `?agent=` from a search string (chat.js:1189-1194). Pure over the injected
 * search; returns the value or `null` when absent / unparseable. The initial
 * session priority is URL `?session=` > `?agent=` (→ webchat key) > stored
 * (chat.js:1211-1214).
 */
export function readAgentFromUrl(search: string): string | null {
  try {
    const params = new URLSearchParams(search)
    return params.get('agent')
  } catch {
    return null
  }
}

/**
 * The stable transcript id for a message (chat.js:3086-3090). Legacy reads the
 * raw `transcript_id` field and coerces via `Number`, returning the number when
 * finite else `null`. We return the finite value stringified (the brief's
 * `string | null` contract) so downstream identity maps key on a string.
 */
export function messageTranscriptId(msg: ChatMessage): string | null {
  const raw = (msg as { transcript_id?: unknown })?.transcript_id
  const value = Number(raw)
  return Number.isFinite(value) ? String(value) : null
}

/**
 * Stable history identity for a message (chat.js:5833-5836): `message_id` else
 * `id`, stringified; empty string when neither is present. These fields ride on
 * the raw history payload, not the narrowed ChatMessage, so they are read off
 * the loosely-typed object exactly as legacy does.
 */
export function historyStableMessageIdentity(msg: ChatMessage): string {
  const raw = msg as { message_id?: unknown; id?: unknown }
  const stableId = raw?.message_id || raw?.id || ''
  return stableId ? String(stableId) : ''
}

// chat.js:383-433 — assistant control/protocol markers are display-only
// transport details and must never leak into rendered text or fallback ids.
const DIRECTIVE_TAG_RE = /\[\[\s*(?:reply_to_current|reply_to\s*:\s*[^\]\n]+)\s*\]\]\s*/g
const GENERATED_ARTIFACT_MARKER_RE = /(?:^|\s*)\[generated artifact omitted:\s*[^\]\n]+?\]\s*/gi
const PROTOCOL_TEXT_MARKER_RE =
  /<\s*(?:minimax:tool_call|tool_calls?|tvoe_calls|invoke\b|parameter\b|effect_calls\b|details\b|angle\s+brackets\b)/i
const PROTOCOL_TEXT_PARAMETER_RE =
  /<\s*parameter\s+name\s*=\s*["'](?:path|content|command|code|patch)["']/i
const PROTOCOL_TEXT_INVOKE_RE = /<\s*invoke\s+name\s*=\s*["'][A-Za-z_][A-Za-z0-9_.:-]*["']/i
const PROTOCOL_TEXT_HTML_RE = /<!doctype\s+html\b|<html\b|<\/html\s*>/i
const PROTOCOL_TEXT_CLOSE_RE = /<\/\s*invoke\s*>|<\/\s*(?:tool_calls?|tvoe_calls)\s*>/i
const PROTOCOL_TEXT_STANDALONE_RE =
  /<\s*(?:parameter|effect_calls|tool_calls?|tvoe_calls|angle\s+brackets)\s*>/i
const PROTOCOL_TEXT_DETAILS_RE = /<\s*details\s*>\s*<\s*summary\s*>\s*View areas around line\b/i

/** chat.js:387-389 — strip reply-threading control directives before display. */
export function stripDirectiveTags(text: string): string {
  return String(text || '')
    .replace(DIRECTIVE_TAG_RE, '')
    .replace(/^\n+/, '')
}

/** chat.js:391-400 — remove generated-artifact omission markers and normalize whitespace. */
export function stripGeneratedArtifactMarkers(text: string): string {
  const value = String(text || '')
  if (!value.includes('[generated artifact omitted:')) return value
  return value
    .replace(/\r\n/g, '\n')
    .replace(GENERATED_ARTIFACT_MARKER_RE, '')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

function looksLikeProtocolTextSuffix(suffix: string): boolean {
  if (/<\s*minimax:tool_call\s*>/i.test(suffix)) return true
  if (PROTOCOL_TEXT_STANDALONE_RE.test(suffix)) return true
  if (PROTOCOL_TEXT_DETAILS_RE.test(suffix)) return true
  if (PROTOCOL_TEXT_PARAMETER_RE.test(suffix)) return true
  if (PROTOCOL_TEXT_INVOKE_RE.test(suffix) && PROTOCOL_TEXT_CLOSE_RE.test(suffix)) return true
  if (PROTOCOL_TEXT_HTML_RE.test(suffix) && PROTOCOL_TEXT_INVOKE_RE.test(suffix)) return true
  return false
}

/** chat.js:419-426 — trim only suffixes that positively match a tool-protocol leak. */
export function stripProtocolTextLeak(text: string): string {
  const value = String(text || '')
  if (!value) return value
  const match = PROTOCOL_TEXT_MARKER_RE.exec(value)
  if (!match) return value
  const suffix = value.slice(match.index)
  if (!looksLikeProtocolTextSuffix(suffix)) return value
  return value.slice(0, match.index).trimEnd()
}

/** chat.js:5842-5846 — role-specific normalized text used by id-less history rows. */
export function historyFallbackText(role: Role, text: string): string {
  if (role === 'assistant') {
    return stripProtocolTextLeak(
      stripDirectiveTags(stripGeneratedArtifactMarkers(text || '')),
    ).trim()
  }
  if (role === 'user') return stripTimePrefix(text || '').trim()
  return (text || '').trim()
}

/** chat.js:5838-5846 — fallback identity after the exact legacy strip pipeline. */
export function historyFallbackMessageIdentity(role: Role, text: string): string {
  return `${role || ''}|${historyFallbackText(role, text)}`
}

// chat.js:430 — the "[<iso> <weekday> <tz>]\n" prefix the engine prepends to
// user messages for the model; stripped from the display text.
const TIME_PREFIX_RE =
  /^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+\-]\d{2}:\d{2} (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Za-z0-9_+\-/]+\]\n/

/** chat.js:431-433 — strip the leading time prefix from a user message. */
export function stripTimePrefix(text: string): string {
  return typeof text === 'string' ? text.replace(TIME_PREFIX_RE, '') : text
}

/** chat.js:7833-7838 — the `YYYY-MM-DD` day key for a timestamp ('' when bad). */
export function dayKey(ts: string | number | null | undefined): string {
  if (!ts) return ''
  const d = typeof ts === 'number' ? new Date(ts) : new Date(ts)
  if (isNaN(d.getTime())) return ''
  return d.toISOString().slice(0, 10)
}

/** chat.js:7840-7849 — human label for a day key (Today/Yesterday/`Mon D`). */
export function dayLabel(isoDay: string): string {
  if (!isoDay) return ''
  const today = new Date()
  const todayKey = today.toISOString().slice(0, 10)
  const yesterKey = new Date(today.getTime() - 86400000).toISOString().slice(0, 10)
  if (isoDay === todayKey) return 'Today'
  if (isoDay === yesterKey) return 'Yesterday'
  const d = new Date(isoDay + 'T12:00:00')
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

/**
 * Whether the composer should autofocus on view entry (chat.js:1353-1360
 * `_shouldAutofocusComposer`). Legacy returns false on a narrow viewport
 * (`max-width:768px`) or a coarse pointer (touch), else true; a `matchMedia`
 * throw falls through to true. Ported pure over an injected env (an object
 * exposing `matchMedia`) so it is testable without a real `window` — the
 * component passes `window`.
 */
export function shouldAutofocusComposer(env: {
  matchMedia?: (query: string) => { matches: boolean }
}): boolean {
  try {
    const mm = env?.matchMedia
    if (typeof mm !== 'function') return true
    if (mm('(max-width: 768px)').matches) return false
    if (mm('(pointer: coarse)').matches) return false
  } catch {
    /* legacy swallows matchMedia errors and autofocuses (chat.js:1357) */
  }
  return true
}

/**
 * Send-button enable + label state.
 *
 * Label is the verbatim port of `_updateSendButton`'s title ternary
 * (chat.js:7012-7016): compaction-in-flight wins over streaming, which wins
 * over the plain "Send". Legacy keeps the button ALWAYS enabled (a click while
 * streaming enqueues, chat.js:7004-7008) and lets `_onSend` no-op on an empty
 * composer (chat.js:6118). The React composer instead disables Send when the
 * trimmed input is empty — a UI affordance, NOT a legacy behavior — so the
 * button visibly reflects "nothing to send". The enqueue-while-streaming path
 * (and its attachments/slash nuances) lands in Task 9; until then `busy` only
 * drives the label, never re-enabling an empty composer.
 */
export function sendButtonState(
  input: string,
  busy: boolean,
  pendingCompaction: boolean,
  hasPendingAttachments = false,
): { disabled: boolean; label: string } {
  // Task-9 carry-forward: attachment-aware enable. Legacy `hasPayload = text ||
  // _pendingAttachments.length > 0` (chat.js:6064) permits a send with empty
  // text but pending attachments; the disable-on-empty React affordance must
  // therefore stay enabled when attachments are pending.
  const hasText = (input ?? '').trim().length > 0
  const disabled = !hasText && !hasPendingAttachments
  const label = pendingCompaction
    ? 'Send (queues until compaction finishes)'
    : busy
      ? 'Send (queues for after current response)'
      : 'Send'
  return { disabled, label }
}

// chat.js:661 — minimal HTML-entity escape for text interpolated into innerHTML.
export function esc(s: string): string {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/* ── Slash commands — key/normalize/input-parse
   ─────────────────────────────────────────────────────────────────────────────
   Ported VERBATIM from static/js/views/chat.js:2597-2651. `slashCommandKey` +
   `normalizeSlashCommand` shape the catalog; `parseSlashInput` decides whether
   the menu opens for a given composer value (and with what filter query). All
   three are pure — the RPC load, the menu render, and command execution live in
   <SlashMenu>. */

/** A slash command as the catalog holds it after `normalizeSlashCommand`. The
 * raw RPC fields (`usage`, `execution`, `argument_choices`, …) ride through via
 * the index signature so execution/dispatch can read them (chat.js:2606 spread). */
export interface SlashCommand {
  name: string
  cmd: string
  label: string
  desc: string
  aliases: string[]
  execution?: { action?: string; kind?: string; rpc_method?: string } | null
  [key: string]: unknown
}

/**
 * chat.js:2597-2601 `_slashCommandKey`. Trim the value, take the first
 * whitespace-delimited token, lowercase it, and prefix `/` when absent; an
 * empty / whitespace / nullish value yields ''.
 */
export function slashCommandKey(value: string): string {
  const raw = (
    String(value || '')
      .trim()
      .split(/\s+/, 1)[0] || ''
  ).toLowerCase()
  if (!raw) return ''
  return raw.startsWith('/') ? raw : '/' + raw
}

/**
 * chat.js:2603-2613 `_normalizeSlashCommand`. Derive `name` from `name || cmd`,
 * mirror it into `cmd`, default `label` to the name, resolve `desc` through
 * `description || desc || usage || ''`, and coerce `aliases` to an array. The
 * rest of the raw command (execution, usage, …) is preserved via spread.
 */
export function normalizeSlashCommand(cmd: {
  name?: string
  cmd?: string
  label?: string
  description?: string
  desc?: string
  usage?: string
  aliases?: string[]
  execution?: { action?: string; kind?: string; rpc_method?: string } | null
  [key: string]: unknown
}): SlashCommand {
  const name = cmd?.name || cmd?.cmd || ''
  return {
    ...cmd,
    name,
    cmd: name,
    label: cmd?.label || name,
    desc: cmd?.description || cmd?.desc || cmd?.usage || '',
    aliases: Array.isArray(cmd?.aliases) ? cmd.aliases : [],
  }
}

/** The parse of a composer value for slash-menu purposes (chat.js:2637-2651). */
export interface SlashInputParse {
  /** Whether the menu should be considered open for this input shape. */
  active: boolean
  /** The lowercased filter query (the text after the leading `/`). */
  query: string
}

/**
 * chat.js:2637-2651 `_handleSlashInput` (the input-shape decision, without the
 * DOM side effects). The menu is active ONLY when the raw value starts with a
 * single `/` and contains no space; the `//` literal-slash escape (chat.js:2640)
 * and any spaced/argument input close it. `query` is the post-`/` remainder,
 * lowercased. Legacy additionally requires the filtered catalog to be non-empty
 * to actually open (chat.js:2644) — that is the caller's concern (it has the
 * catalog), so this pure parse reports the input-shape intent only.
 */
export function parseSlashInput(text: string): SlashInputParse {
  const val = String(text ?? '')
  // chat.js:2640 — `//…` is the literal-slash escape; never opens the menu.
  if (val.startsWith('//')) return { active: false, query: '' }
  // chat.js:2641 — a single `/` with no space is a command-in-progress.
  if (val.startsWith('/') && !val.includes(' ')) {
    return { active: true, query: val.slice(1).toLowerCase() }
  }
  return { active: false, query: '' }
}

/* ── Attachments — mime allowlist, caps, mime resolution, payload normalization
   ─────────────────────────────────────────────────────────────────────────────
   Ported verbatim from static/js/views/chat.js:251-334 (constants), 8291-8325
   (mime + download helpers), 7932-8050 (page-dump detection + outgoing payload
   normalization). Values CONFIRMED against source, not the brief. */

// chat.js:251 — inline (base64-on-frame) threshold, also the text-family hard cap.
export const INLINE_THRESHOLD_BYTES = 2_000_000
export const ATTACHMENT_TEXT_HARD_CAP_BYTES = INLINE_THRESHOLD_BYTES // chat.js:252
export const LARGE_PASTE_CHARS = 20_000 // chat.js:253
export const PAGE_DUMP_CHARS = 8_000 // chat.js:254
export const PAGE_DUMP_MARKER_MIN_SCORE = 3 // chat.js:255
// chat.js:256-267 — the page-dump heuristic markers (matched case-insensitively).
export const PAGE_DUMP_MARKERS = [
  'Chat session',
  'agent:main:webchat:',
  'Still waiting for agent response',
  'AI MODEL ROUTER',
  'The provider returned an empty response',
  'Pulsing',
  'Running',
  'Send a message',
  'SYSTEM',
  'CAP',
]
export const ATTACHMENT_IMAGE_HARD_CAP_BYTES = 5 * 1024 * 1024 // chat.js:268
export const ATTACHMENT_PDF_HARD_CAP_BYTES = 30 * 1024 * 1024 // chat.js:269 (staged PDF bridge cap)
export const ATTACHMENT_IMAGE_MIMES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'] // chat.js:270-275
export const ATTACHMENT_TEXT_MIMES = [
  'text/plain',
  'text/markdown',
  'text/html',
  'text/csv',
  'application/json',
] // chat.js:276-282
export const ATTACHMENT_ALLOWED_MIMES = [
  ...ATTACHMENT_IMAGE_MIMES,
  'application/pdf',
  ...ATTACHMENT_TEXT_MIMES,
] // chat.js:283-287
export const ATTACHMENT_EXTENSION_MIMES: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp',
  pdf: 'application/pdf',
  txt: 'text/plain',
  md: 'text/markdown',
  markdown: 'text/markdown',
  html: 'text/html',
  htm: 'text/html',
  csv: 'text/csv',
  json: 'application/json',
} // chat.js:288-302
// chat.js:303 — the exact allowed-types label shown in the rejection message.
export const ATTACHMENT_ALLOWED_LABEL = 'PNG, JPEG, GIF, WEBP, PDF, TXT, MD, HTML, CSV, JSON'

/** chat.js:304 — is this mime in the attachment allowlist. */
export function isAllowedAttachmentMime(mime: string): boolean {
  return typeof mime === 'string' && ATTACHMENT_ALLOWED_MIMES.indexOf(mime) !== -1
}
/** chat.js:307 — is this an image mime. */
export function isImageAttachmentMime(mime: string): boolean {
  return typeof mime === 'string' && ATTACHMENT_IMAGE_MIMES.indexOf(mime) !== -1
}
/** chat.js:310 — is this a text-family mime. */
export function isTextAttachmentMime(mime: string): boolean {
  return typeof mime === 'string' && ATTACHMENT_TEXT_MIMES.indexOf(mime) !== -1
}
/** chat.js:313 — only images and PDFs may be staged-uploaded (>2 MB). */
export function canStageAttachmentMime(mime: string): boolean {
  return mime === 'application/pdf' || isImageAttachmentMime(mime)
}
/** chat.js:316-321 — the per-type hard cap in bytes (unknown → the image cap). */
export function attachmentHardCapBytes(mime: string): number {
  if (mime === 'application/pdf') return ATTACHMENT_PDF_HARD_CAP_BYTES
  if (isImageAttachmentMime(mime)) return ATTACHMENT_IMAGE_HARD_CAP_BYTES
  if (isTextAttachmentMime(mime)) return ATTACHMENT_TEXT_HARD_CAP_BYTES
  return ATTACHMENT_IMAGE_HARD_CAP_BYTES
}

/**
 * chat.js:8291-8297 — resolve a file's effective mime. An allowed `file.type`
 * wins; otherwise the extension map; otherwise the raw `file.type`; otherwise
 * `application/octet-stream`.
 */
export function resolveAttachmentMime(
  file:
    | {
        name?: string
        type?: string
      }
    | null
    | undefined,
): string {
  const name = file && file.name ? String(file.name) : ''
  const ext = name.includes('.') ? (name.split('.').pop() || '').toLowerCase() : ''
  const extensionMime = ATTACHMENT_EXTENSION_MIMES[ext]
  if (file && file.type && isAllowedAttachmentMime(file.type)) return file.type
  return extensionMime || (file && file.type) || 'application/octet-stream'
}

// chat.js:7933 — cheap token estimate (floor(len/4), min 1 for non-empty).
export function estimateTextTokens(text: string): number {
  return text ? Math.max(1, Math.floor(text.length / 4)) : 0
}

// chat.js:7936-7941 — count distinct page-dump markers present (case-insensitive).
export function pageDumpMarkerScore(text: string): number {
  const lowered = String(text || '').toLowerCase()
  return PAGE_DUMP_MARKERS.reduce(
    (score, marker) => (lowered.includes(marker.toLowerCase()) ? score + 1 : score),
    0,
  )
}

// chat.js:7943-7950 — base64-encode bytes in 0x8000-char chunks (avoids the
// call-stack blowup of spreading a huge Uint8Array into String.fromCharCode).
function bytesToBase64(bytes: Uint8Array): string {
  const chunkSize = 0x8000
  const chunks: string[] = []
  for (let i = 0; i < bytes.length; i += chunkSize) {
    chunks.push(String.fromCharCode(...bytes.subarray(i, i + chunkSize)))
  }
  return btoa(chunks.join(''))
}

// chat.js:7952-7955 — the generated .txt name for a large paste / page dump.
export function largePasteAttachmentName(kind: 'page_dump' | 'large_paste'): string {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').replace('T', '-').replace('Z', '')
  return `${kind === 'page_dump' ? 'webchat-page-dump' : 'webchat-paste'}-${stamp}.txt`
}

/**
 * A pending-attachment buffer entry (chat.js:247-249). Two-mode: `inline`
 * (≤ 2 MB, base64-on-frame) and `staged` (image/PDF > 2 MB, POSTed to
 * /api/v1/files/upload → `file_uuid`), plus the transient `inline_pending`
 * (FileReader in flight) and `uploading` (staged POST in flight) states.
 */
export interface PendingAttachment {
  kind: 'inline' | 'staged' | 'inline_pending' | 'uploading'
  local_id: number
  name: string
  mime: string
  size: number
  data?: string
  dataUrl?: string
  file_uuid?: string
  // Provenance for a normalization-generated attachment (chat.js:8025-8033).
  generated?: boolean
  normalizationKind?: 'page_dump' | 'large_paste'
  inputNormalization?: {
    kind: 'page_dump' | 'large_paste'
    originalChars: number
    markerScore: number
    materialEstimatedTokens: number
    guardAction: string
  }
}

/** The RPC attachment shape sent on `chat.send` params (chat.js:8159-8164). */
export type OutgoingAttachment =
  | { type: string; file_uuid: string; mime: string; name: string }
  | { type: string; data?: string; mime: string; name: string }

/** chat.js:8299-8301 — is a read (inline_pending) or upload (uploading) in flight. */
export function hasPendingAttachmentWork(attachments: PendingAttachment[]): boolean {
  return (attachments || []).some(
    (att) => att.kind === 'inline_pending' || att.kind === 'uploading',
  )
}

/** chat.js:8308-8311 — the download filename, defaulting to "attachment". */
export function attachmentDownloadName(att: { name?: string } | null | undefined): string {
  const raw = String((att && att.name) || 'attachment').trim()
  return raw || 'attachment'
}

/**
 * chat.js:8313-8325 — resolve a safe download href for an attachment. Prefers an
 * embedded `dataUrl` (rejecting `javascript:`), then base64 `data`, then a
 * remote url; empty string when none is safe/available.
 */
export function attachmentDownloadHref(
  att:
    | { dataUrl?: string; data?: string; url?: string; download_url?: string; downloadUrl?: string }
    | null
    | undefined,
  mime?: string,
): string {
  if (!att) return ''
  const safeHref = (value: unknown, imageOnly = false): string => {
    const raw = String(value || '').trim()
    if (!raw) return ''
    try {
      // The URL parser normalizes ASCII tabs/newlines in schemes, so inputs
      // such as `java\nscript:` cannot bypass the protocol allowlist. Relative
      // gateway URLs resolve against the inert base and remain permitted.
      const parsed = new URL(raw, 'https://agentos.invalid/')
      if (!['http:', 'https:', 'blob:', 'data:'].includes(parsed.protocol.toLowerCase())) return ''
      if (
        imageOnly &&
        parsed.protocol === 'data:' &&
        !/^data:image\/[a-z0-9.+-]+(?:[;,])/i.test(raw)
      ) {
        return ''
      }
      return raw
    } catch {
      return ''
    }
  }
  if (att.dataUrl) {
    return safeHref(
      att.dataUrl,
      String(mime || '')
        .toLowerCase()
        .startsWith('image/'),
    )
  }
  if (att.data) {
    return `data:${mime || 'application/octet-stream'};base64,${String(att.data)}`
  }
  return safeHref(att.url || att.download_url || att.downloadUrl || '')
}

/**
 * chat.js:8327-8344 — one attachment chip used by both live user turns and
 * persisted-history reconstruction. All payload-derived attributes/text are
 * escaped before the imperative transcript inserts this HTML.
 */
export function renderMessageAttachmentHtml(attachment: unknown): string {
  if (!attachment || typeof attachment !== 'object' || Array.isArray(attachment)) return ''
  const att = attachment as Record<string, unknown>
  const mime = String(att.type || att.mime || '')
  const name = String(att.name || 'attachment')
  const href = attachmentDownloadHref(
    {
      dataUrl: typeof att.dataUrl === 'string' ? att.dataUrl : undefined,
      data: typeof att.data === 'string' ? att.data : undefined,
      url: typeof att.url === 'string' ? att.url : undefined,
      download_url: typeof att.download_url === 'string' ? att.download_url : undefined,
      downloadUrl: typeof att.downloadUrl === 'string' ? att.downloadUrl : undefined,
    },
    mime,
  )
  if (mime.toLowerCase().startsWith('image/') && (att.dataUrl || att.data)) {
    if (!href) return ''
    return `<img class="msg-thumb" src="${esc(href)}" alt="${esc(name)}">`
  }

  const inner =
    '<span class="msg-file-chip__icon" aria-hidden="true">file</span>' +
    `<span class="msg-file-chip__name">${esc(name)}</span>` +
    `<span class="msg-file-chip__meta">${esc(mime || 'attachment')}</span>`
  if (href) {
    const downloadName = attachmentDownloadName({ name })
    return `<a class="msg-file-chip msg-file-chip--download" title="${esc(name)}" href="${esc(href)}" download="${esc(downloadName)}">${inner}</a>`
  }
  return `<span class="msg-file-chip msg-file-chip--disabled" title="${esc(name)}">${inner}</span>`
}

/** The result of normalizing an outgoing composer payload (chat.js:7982). */
export interface NormalizedComposerPayload {
  text: string
  displayText: string
  attachments: PendingAttachment[]
  normalized: {
    kind: 'page_dump' | 'large_paste'
    originalChars: number
    markerScore: number
    materialEstimatedTokens: number
  } | null
}

/**
 * chat.js:7982-8050 `_normalizeOutgoingComposerPayload`. A short / plain message
 * (or an allowed slash command) passes through untouched. A >= 20k-char paste
 * (`LARGE_PASTE_CHARS`) OR a >= 8k-char page dump with marker score >= 3 is
 * converted into a generated inline `.txt` attachment carrying provenance, and
 * the message text is replaced with a canned instruction. Returns `null` (and
 * toasts) when the pasted bytes exceed the text hard cap (chat.js:8007-8014).
 *
 * `UI.toast` is a side effect in legacy; here it is injected as `options.onToast`
 * so the helper stays pure and unit-testable. The component passes the real toast.
 */
export async function normalizeOutgoingComposerPayload(
  text: string,
  attachments: PendingAttachment[],
  options: {
    allowSlashCommand?: boolean
    onToast?: (message: string, level: 'warn' | 'info', ms?: number) => void
    nextLocalId?: () => number
  } = {},
): Promise<NormalizedComposerPayload | null> {
  const onToast = options.onToast ?? (() => {})
  const nextLocalId = options.nextLocalId ?? (() => Date.now() + Math.floor(Math.random() * 1000))
  const raw = String(text || '')
  const markerScore = pageDumpMarkerScore(raw)
  const isPageDump = raw.length >= PAGE_DUMP_CHARS && markerScore >= PAGE_DUMP_MARKER_MIN_SCORE
  const isLargePaste = raw.length >= LARGE_PASTE_CHARS
  // chat.js:7987 — an allowed slash command bypasses normalization.
  if (options.allowSlashCommand && raw.startsWith('/')) {
    return {
      text: raw,
      displayText: raw,
      attachments: attachments.map((a) => ({ ...a })),
      normalized: null,
    }
  }
  // chat.js:7995 — neither trigger fired: pass through.
  if (!isPageDump && !isLargePaste) {
    return {
      text: raw,
      displayText: raw,
      attachments: attachments.map((a) => ({ ...a })),
      normalized: null,
    }
  }

  const kind: 'page_dump' | 'large_paste' = isPageDump ? 'page_dump' : 'large_paste'
  const bytes = new TextEncoder().encode(raw)
  const materialEstimatedTokens = estimateTextTokens(raw)
  if (bytes.length > ATTACHMENT_TEXT_HARD_CAP_BYTES) {
    onToast(
      `Pasted text is too large to attach directly (${Math.round(bytes.length / 1000 / 1000)} MB). Save it as a file or send a shorter summary.`,
      'warn',
      6000,
    )
    return null
  }

  const encoded = bytesToBase64(bytes)
  const generatedAttachment: PendingAttachment = {
    kind: 'inline',
    local_id: nextLocalId(),
    name: largePasteAttachmentName(kind),
    mime: 'text/plain',
    size: bytes.length,
    data: encoded,
    dataUrl: `data:text/plain;base64,${encoded}`,
    generated: true,
    normalizationKind: kind,
    inputNormalization: {
      kind,
      originalChars: raw.length,
      markerScore,
      materialEstimatedTokens,
      guardAction: 'generated_text_attachment',
    },
  }
  const message =
    kind === 'page_dump'
      ? 'Please process the attached WebChat page dump.'
      : 'Please process the attached pasted text.'
  onToast('Large pasted text was attached as a .txt file.', 'info', 2500)
  return {
    text: message,
    displayText: message,
    attachments: [...attachments.map((a) => ({ ...a })), generatedAttachment],
    normalized: { kind, originalChars: raw.length, markerScore, materialEstimatedTokens },
  }
}

/**
 * chat.js:8157-8164 — map a pending attachment into its `chat.send` RPC shape. A
 * `staged` entry ships its `file_uuid`; everything else ships inline base64
 * `data` (defaulting the type to `image/png` as legacy does, chat.js:8163).
 */
export function outgoingAttachment(att: PendingAttachment): OutgoingAttachment {
  if (att.kind === 'staged') {
    return { type: att.mime, file_uuid: att.file_uuid || '', mime: att.mime, name: att.name }
  }
  return { type: att.mime || 'image/png', data: att.data, mime: att.mime, name: att.name }
}

/**
 * chat.js:7963-7980 — build the input-normalization provenance object from any
 * generated attachments, for the `chat.send` `inputProvenance` param. Returns
 * null when no generated attachment is present.
 */
export function inputNormalizationProvenanceFromAttachments(attachments: PendingAttachment[]): {
  kind: 'web_message'
  source: 'WebChat'
  input_normalization: {
    source: 'input_normalization'
    original_chars: number
    material_estimated_tokens: number
    marker_score: number
    generated_attachment_count: number
    guard_action: string
  }
} | null {
  const nonNegativeInteger = (value: unknown): number => {
    const number = Number(value)
    if (!Number.isFinite(number) || number < 0) return 0
    return Math.floor(number)
  }
  const generated = (attachments || []).filter(
    (a) => a && a.generated === true && a.inputNormalization,
  )
  const first = generated[0]
  if (!first || !first.inputNormalization) return null
  const meta = first.inputNormalization
  return {
    kind: 'web_message',
    source: 'WebChat',
    input_normalization: {
      source: 'input_normalization',
      original_chars: nonNegativeInteger(meta.originalChars),
      material_estimated_tokens: nonNegativeInteger(meta.materialEstimatedTokens),
      marker_score: nonNegativeInteger(meta.markerScore),
      generated_attachment_count: generated.length,
      guard_action: meta.guardAction || 'generated_text_attachment',
    },
  }
}

/**
 * chat.js:2221-2223 (_effectiveElevatedMode) — the effective execution mode for
 * the chat session: the browser session override wins, else the global
 * `permissions.default_mode`, normalized to on/bypass/full/''. The two inputs
 * (both already read elsewhere: the session mode from the shared elevated store,
 * the global from config.get) are injected so this stays pure.
 */
export function effectiveElevatedMode(sessionMode: string, globalMode: string): ElevatedMode {
  return normalizeElevatedMode(sessionMode || globalMode)
}

// A single usage.status session row (chat.js:604-615). Keys arrive in either
// snake_case (server) or camelCase; the current row is matched by any of its
// key aliases.
export interface UsageRow {
  session?: string
  sessionKey?: string
  key?: string
  input_tokens?: number
  inputTokens?: number
  output_tokens?: number
  outputTokens?: number
  cache_read_tokens?: number
  cacheReadTokens?: number
  cache_write_tokens?: number
  cacheWriteTokens?: number
  cost_usd?: number
  costUsd?: number
  model?: string
  contextStatus?: unknown
  context_status?: unknown
}

// The normalized per-session usage totals (chat.js:569 `_usageAccum` shape,
// without the widget-specific accounting layered on top).
export interface SessionUsage {
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
  cost: number | null
  model: string
}

/**
 * chat.js:604-607 (_loadCurrentSessionUsage) — find the row for the active
 * session, matching on any of the `session` / `sessionKey` / `key` aliases the
 * server may use. Returns undefined when nothing matches (legacy then clears
 * the readout).
 */
export function findSessionUsage(
  rows: UsageRow[] | undefined | null,
  sessionKey: string,
): UsageRow | undefined {
  if (!Array.isArray(rows)) return undefined
  return rows.find((s) => (s.session || s.sessionKey || s.key) === sessionKey)
}

/**
 * chat.js:609-615 (_loadCurrentSessionUsage) — coerce a usage row into the
 * numeric totals the readout renders. Token fields accept snake/camel aliases;
 * a non-positive cost is nulled (the legacy widget treats 0 as "no cost yet").
 */
export function normalizeSessionUsage(row: UsageRow): SessionUsage {
  const num = (v: unknown): number => Number(v || 0)
  const cost = Number(row.cost_usd || row.costUsd || 0)
  return {
    input: num(row.input_tokens ?? row.inputTokens),
    output: num(row.output_tokens ?? row.outputTokens),
    cacheRead: num(row.cache_read_tokens ?? row.cacheReadTokens),
    cacheWrite: num(row.cache_write_tokens ?? row.cacheWriteTokens),
    cost: cost > 0 ? cost : null,
    model: row.model || '',
  }
}

/* ─── Pending queue + markdown export (chat.js:8389-8663, 335) ─────────────── */

// chat.js:335 — the pending-queue cap. The QUEUE cap (queued sends while busy),
// distinct from the attachment tray. Verbatim value.
export const MAX_PENDING = 5

// chat.js:336 — a queued send: `{ text, attachments, intent }`. `intent` is the
// per-send session intent (e.g. 'new_chat') that rides along when it drains.
export interface PendingItem {
  text: string
  attachments: PendingAttachment[]
  intent: string | null
}

// chat.js:673-679 (`_displayRoleLabel`) — the export/transcript role label:
// user→You, assistant→Cap, subagent→Sub-agent, else Capitalized, ''→''.
export function displayRoleLabel(role: string): string {
  if (role === 'user') return 'You'
  if (role === 'assistant') return 'Cap'
  if (role === 'subagent') return 'Sub-agent'
  if (role) return role.charAt(0).toUpperCase() + role.slice(1)
  return ''
}

// chat.js:8425-8435 (`_artifactExportDownloadUrl`) — the clean relative download
// URL (artifactDownloadUrl strips sessionKey) with the export session key
// re-appended. Empty when the artifact has no resolvable URL (chat.js:8427).
export function artifactExportDownloadUrl(
  artifact: Artifact | null | undefined,
  sessionKey: string,
): string {
  const raw = artifactDownloadUrl(artifact || {})
  if (!raw) return ''
  try {
    const url = new URL(raw, window.location.origin)
    if (sessionKey) url.searchParams.set('sessionKey', sessionKey)
    return url.href
  } catch {
    return raw
  }
}

// chat.js:8411-8423 (`_artifactMarkdownLines`) — the "Artifacts:" block appended
// after a message body. '' when there are no artifacts. Each line is a Download
// link with an optional `mime · size` suffix; size is KB, rounded, 1 KB floor.
export function artifactMarkdownLines(
  artifacts: Artifact[] | null | undefined,
  sessionKey: string,
): string {
  if (!Array.isArray(artifacts) || artifacts.length === 0) return ''
  const lines = artifacts.map((artifact) => {
    const name = artifact && artifact.name ? String(artifact.name) : 'artifact'
    const mime = artifact && artifact.mime ? String(artifact.mime) : ''
    const size =
      artifact && artifact.size ? `${Math.max(1, Math.round(Number(artifact.size) / 1024))} KB` : ''
    const url = artifactExportDownloadUrl(artifact || {}, sessionKey)
    const meta = [mime, size].filter(Boolean).join(' · ')
    const suffix = meta ? ` - ${meta}` : ''
    return `- [Download ${name}](${url})${suffix}`
  })
  return `\n\nArtifacts:\n${lines.join('\n')}`
}

// A transcript message as it appears in the export (legacy `_messages` rows:
// chat.js:6130 — `{ role, text, ts, artifacts }`). Kept separate from the
// narrower on-screen `ChatMessage` because export rows carry `ts` + `artifacts`.
export interface ExportMessage {
  role: string
  text: string
  // chat.js:5648/6130 — `msg.timestamp || msg.ts` is an ISO string (history) or
  // the send-time ISO string (user bubble); `new Date(ts)` accepts either an ISO
  // string or an epoch number (chat.js:8398).
  ts?: number | string | null
  artifacts?: Artifact[]
}

// chat.js:8389-8409 (`_exportMarkdown`) — build the export document. Returns null
// for an empty transcript (chat.js:8390, the caller toasts + skips). The Blob /
// anchor download itself is the side effect the caller performs; this builder is
// pure so it can be unit-tested. Header uses an em-dash (chat.js:8394).
export function exportMarkdownDocument(
  messages: ExportMessage[],
  sessionKey: string,
): string | null {
  if (messages.length === 0) return null
  let md = `# Chat Export — ${sessionKey}\n\n`
  md += `Exported: ${new Date().toISOString()}\n\n---\n\n`
  messages.forEach((msg) => {
    const role = displayRoleLabel(msg.role) || msg.role
    const time = msg.ts ? ` _(${new Date(msg.ts).toLocaleString()})_` : ''
    md += `### ${role}${time}\n\n${msg.text}${artifactMarkdownLines(msg.artifacts || [], sessionKey)}\n\n---\n\n`
  })
  return md
}

/** The result of a pending-queue mutation: the next queue + whether it changed. */
export interface EnqueueResult {
  ok: boolean
  queue: PendingItem[]
}

// chat.js:8505-8533 (`_enqueuePendingInput`) — append a send to the queue. When
// the queue is at MAX_PENDING the enqueue is REJECTED (ok=false) and the SAME
// queue reference is returned unchanged (the caller toasts "queue full"). The
// pushed item's attachments are shallow-cloned per element (chat.js:8522) so a
// later mutation of the source attachments does not leak into the queued copy.
export function enqueuePending(queue: PendingItem[], item: PendingItem): EnqueueResult {
  if (queue.length >= MAX_PENDING) return { ok: false, queue }
  const cloned: PendingItem = {
    text: item.text,
    attachments: item.attachments.map((a) => ({ ...a })),
    intent: item.intent,
  }
  return { ok: true, queue: [...queue, cloned] }
}

/**
 * The result of recovering pending back into the composer (chat.js:8596/8560):
 * the composer draft to set, the stacked attachments, the resolved intent, the
 * now-emptied (pop-all) or trimmed (tail) queue, and whether anything was
 * recovered. Pure — the DOM writes (textarea value / focus / history reset) are
 * the caller's, mirroring how legacy split the model from `_textarea` mutation.
 */
export interface RecoverResult {
  text: string
  attachments: PendingAttachment[]
  intent: string | null
  queue: PendingItem[]
  recovered: boolean
}

// chat.js:8596-8626 (`_popAllPendingIntoComposer`) — recover the ENTIRE queue
// into the composer. Queued texts join the current draft with newlines (FIFO,
// empties dropped, chat.js:8599-8605); attachments stack current-then-queued
// (chat.js:8611); intent keeps the current, else the head's (chat.js:8612). The
// queue is emptied. recovered=false (queue unchanged, draft passed through) when
// the queue was already empty (chat.js:8598).
export function popAllPendingIntoComposer(
  queue: PendingItem[],
  currentText: string,
  currentAttachments: PendingAttachment[],
  currentIntent: string | null,
): RecoverResult {
  if (queue.length === 0) {
    return {
      text: currentText,
      attachments: currentAttachments,
      intent: currentIntent,
      queue: [],
      recovered: false,
    }
  }
  const queuedTexts = queue.map((p) => (typeof p.text === 'string' ? p.text : '')).filter(Boolean)
  const queuedAttachments = queue.flatMap((p) => p.attachments || [])
  const headIntent = queue[0] ? queue[0].intent : null
  const joined = [currentText, ...queuedTexts].filter(Boolean).join('\n')
  return {
    text: joined,
    attachments: [...currentAttachments, ...queuedAttachments],
    intent: currentIntent || headIntent || null,
    queue: [],
    recovered: true,
  }
}

// chat.js:8560-8570 (`_popPendingTail`) — Alt+↑: pop the MOST-RECENT item into
// the composer (replacing the draft, not joining). recovered=false + unchanged
// queue when empty (chat.js:8561).
export function popPendingTail(queue: PendingItem[]): RecoverResult {
  if (queue.length === 0) {
    return { text: '', attachments: [], intent: null, queue: [], recovered: false }
  }
  const next = queue.slice()
  const tail = next.pop() as PendingItem
  return {
    text: tail.text || '',
    attachments: tail.attachments || [],
    intent: tail.intent || null,
    queue: next,
    recovered: true,
  }
}
