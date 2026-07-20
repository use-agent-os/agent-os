// Chat view — pure logic helpers ported verbatim from the legacy
// static/js/views/chat.js. Every function here is pure and side-effect free:
// URL / storage inputs are injected as strings rather than read off `window`,
// so each helper is unit-testable in isolation. Cited legacy line ranges are
// against static/js/views/chat.js.

import type { ChatMessage, Role } from './types'

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

/**
 * Fallback history identity when there is no stable id (chat.js:5838-5839):
 * `${role}|${text}`. Legacy pipes the text through `_historyFallbackText`
 * (chat.js:5842-5846), a role-specific strip pipeline ported by later tasks;
 * this foundation trims the text (the common tail of every legacy branch).
 */
export function historyFallbackMessageIdentity(role: Role, text: string): string {
  return `${role || ''}|${(text || '').trim()}`
}
