/**
 * Names the gateway assigns before a person (or the titler) has named a
 * session. Mirrors PLACEHOLDER_NAMES in src/agentos/gateway/session_titler.py:
 * the titler treats these as "still unnamed", and so does the UI, which shows
 * "New session" instead of, say, "WebChat" while the real title is on its way.
 */
export const PLACEHOLDER_SESSION_NAMES: ReadonlySet<string> = new Set([
  'webchat',
  'chat',
  'new chat',
  'new session',
  'untitled',
])

export function isPlaceholderSessionName(name: string | null | undefined): boolean {
  const trimmed = (name ?? '').trim().toLowerCase()
  return trimmed === '' || PLACEHOLDER_SESSION_NAMES.has(trimmed)
}

/** The name to show: the real one, or '' when it is only a placeholder. */
export function shownSessionName(name: string | null | undefined): string {
  return isPlaceholderSessionName(name) ? '' : (name ?? '').trim()
}
