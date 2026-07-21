import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { Copy, RotateCcw } from 'lucide-react'
import {
  classifySessionKey,
  sessionItemKey,
  sessionRunStatus,
  type SessionGroup,
  type SessionListItem,
} from './logic'

/**
 * Session chip + switcher (React) — ported from the legacy topbar-center chip
 * (chat.js:1219-1229 render, 1836-2089 `_bindSessionChip`).
 *
 * One chip acts as the switcher trigger; a sibling copy button copies the key
 * and a reset button runs `sessions.reset`. Opening the chip fetches the session
 * list from `/api/sessions` (chat.js:2026), grouping items via
 * `classifySessionKey` (chat.js:1862) and tagging each with its run status
 * (chat.js:1611). Selecting a session calls `onSwitch(key)` — the transcript
 * owner (ChatPage) re-points `useTranscript` at the new key, which parks the old
 * session's stream, re-subscribes, and reloads history. When the list fetch
 * fails, the popover degrades to a manual key-entry field (chat.js:2038-2069).
 */

// chat.js:1903 — the switcher group order (empty groups are skipped).
const GROUP_ORDER: SessionGroup[] = ['Web chat', 'CLI', 'Sub-agents', 'Agents', 'Sessions', 'Other']

export interface SessionChipProps {
  /** The current (canonical) session key (chat.js:1223). */
  sessionKey: string
  /** Switch to a different session (chat.js:1809 `_switchToSession`). */
  onSwitch: (key: string) => void
  /** Reset the current session (chat.js:2723 `sessions.reset`). */
  onReset: () => void
  /**
   * Copy the current key to the clipboard (chat.js:1782
   * `_copySessionKeyToClipboard`). Injected so the component stays pure of the
   * clipboard/execCommand fallback; defaults to `navigator.clipboard`.
   */
  onCopy?: (key: string) => Promise<void>
  /**
   * Fetch the session list (chat.js:2026 `GET /api/sessions`). Injected for
   * testability; defaults to the real `fetch`. Resolves the raw items, or throws
   * → the popover degrades to manual entry.
   */
  fetchSessions?: () => Promise<SessionListItem[]>
}

function defaultCopy(key: string): Promise<void> {
  // chat.js:1784-1806 — clipboard API with an execCommand fallback.
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    return navigator.clipboard.writeText(key)
  }
  const textarea = document.createElement('textarea')
  textarea.value = key
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.left = '-9999px'
  textarea.style.top = '0'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  let copied = false
  try {
    copied = document.execCommand('copy')
  } finally {
    textarea.remove()
  }
  return copied ? Promise.resolve() : Promise.reject(new Error('Copy command failed'))
}

async function defaultFetchSessions(): Promise<SessionListItem[]> {
  // chat.js:2026-2032 — GET /api/sessions → data.sessions || data.keys, filtered
  // to items that actually carry a key. A non-OK response throws → manual entry.
  const resp = await fetch('/api/sessions')
  if (!resp.ok) throw new Error('Session list unavailable')
  const data = (await resp.json()) as { sessions?: SessionListItem[]; keys?: SessionListItem[] }
  const raw = data.sessions || data.keys || []
  return raw.filter((s) => !!sessionItemKey(s))
}

export function SessionChip({
  sessionKey,
  onSwitch,
  onReset,
  onCopy = defaultCopy,
  fetchSessions = defaultFetchSessions,
}: SessionChipProps) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  // null = not fetched yet / in-flight; [] = fetched-empty. `failed` degrades
  // the popover to manual key entry (chat.js:2038).
  const [sessions, setSessions] = useState<SessionListItem[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [manualKey, setManualKey] = useState('')
  const rootRef = useRef<HTMLDivElement>(null)

  const copy = useCallback(() => {
    if (!sessionKey) return
    onCopy(sessionKey)
      .then(() => toast.info('Session key copied'))
      .catch((err: unknown) =>
        toast.error('Copy failed: ' + (err instanceof Error ? err.message : String(err))),
      )
  }, [sessionKey, onCopy])

  const dismiss = useCallback(() => {
    setOpen(false)
    setFilter('')
    setSessions(null)
    setFailed(false)
    setManualKey(sessionKey)
  }, [sessionKey])

  const toggle = useCallback(() => {
    setOpen((wasOpen) => {
      if (wasOpen) {
        setFilter('')
        setSessions(null)
        setFailed(false)
        setManualKey(sessionKey)
      }
      return !wasOpen
    })
  }, [sessionKey])

  const switchTo = useCallback(
    (key: string) => {
      dismiss()
      if (key && key !== sessionKey) onSwitch(key)
    },
    [dismiss, onSwitch, sessionKey],
  )

  // chat.js:1960-2076 — opening the chip fetches the session list (the close-time
  // reset lives in `toggle`/`dismiss`, so this effect only synchronizes the
  // external fetch while open — no setState-in-effect cascade). Refetch each open
  // so a freshly-created session shows up.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    fetchSessions()
      .then((list) => {
        if (!cancelled) setSessions(list)
      })
      .catch(() => {
        if (!cancelled) {
          setFailed(true)
          setManualKey(sessionKey)
        }
      })
    return () => {
      cancelled = true
    }
  }, [open, fetchSessions, sessionKey])

  // chat.js:2004-2020 — dismiss on outside click / Escape while open.
  useEffect(() => {
    if (!open) return
    const onDocClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) dismiss()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        dismiss()
      }
    }
    // Defer registration so the click that opened us isn't picked up.
    const id = window.setTimeout(() => {
      document.addEventListener('mousedown', onDocClick, true)
      document.addEventListener('keydown', onKey)
    }, 0)
    return () => {
      window.clearTimeout(id)
      document.removeEventListener('mousedown', onDocClick, true)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, dismiss])

  // chat.js:1901-1957 — group the fetched sessions, apply the filter.
  const groups: Array<{ label: SessionGroup; items: SessionListItem[] }> = []
  if (sessions) {
    const bucket: Record<SessionGroup, SessionListItem[]> = {
      'Web chat': [],
      CLI: [],
      'Sub-agents': [],
      Agents: [],
      Sessions: [],
      Other: [],
    }
    for (const item of sessions) {
      const g = classifySessionKey(item)
      if (g) bucket[g].push(item)
    }
    const f = filter.trim().toLowerCase()
    for (const label of GROUP_ORDER) {
      const visible = f
        ? bucket[label].filter((it) => sessionItemKey(it).toLowerCase().includes(f))
        : bucket[label]
      if (visible.length) groups.push({ label, items: visible })
    }
  }
  const total = groups.reduce((n, g) => n + g.items.length, 0)

  return (
    <div className="chat-session" ref={rootRef}>
      <span className="chat-session-label">session</span>
      <button
        type="button"
        className={`chat-session-chip${open ? ' is-active' : ''}`}
        aria-label="Switch chat session"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={toggle}
      >
        <span className="chat-session-chip-key" title={sessionKey}>
          {sessionKey}
        </span>
        <span className="chat-session-chip-caret" aria-hidden="true">
          ▾
        </span>
      </button>
      <button
        type="button"
        className="chat-session-copy"
        title={'Copy session key: ' + sessionKey}
        aria-label="Copy session key"
        onClick={copy}
      >
        <Copy className="chat-session-action-icon" aria-hidden="true" />
        <span className="chat-session-action-label">copy</span>
      </button>
      <button
        type="button"
        className="chat-session-reset"
        title="Reset the current session"
        aria-label="Reset session"
        onClick={onReset}
      >
        <RotateCcw className="chat-session-action-icon" aria-hidden="true" />
        <span className="chat-session-action-label">reset</span>
      </button>

      {open && (
        <div className="chat-session-popover" role="dialog" aria-label="Switch session">
          {failed ? (
            <>
              <input
                type="search"
                className="chat-session-popover-search"
                placeholder="Enter session key..."
                aria-label="Session key"
                autoComplete="off"
                spellCheck={false}
                value={manualKey}
                onChange={(e) => setManualKey(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    switchTo(manualKey.trim())
                  }
                }}
                autoFocus
              />
              <div className="chat-session-popover-list">
                <div className="chat-session-popover-empty">
                  Session list unavailable. Enter a key above.
                </div>
                <button
                  type="button"
                  className="chat-session-popover-item"
                  onClick={() => switchTo(manualKey.trim())}
                >
                  <span className="chat-session-popover-item-key">Switch to typed session</span>
                </button>
              </div>
            </>
          ) : (
            <>
              <input
                type="search"
                className="chat-session-popover-search"
                placeholder="Search sessions…"
                aria-label="Search sessions"
                autoComplete="off"
                spellCheck={false}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                autoFocus
              />
              <div className="chat-session-popover-list">
                {sessions === null ? (
                  <div className="chat-session-popover-empty">Loading…</div>
                ) : total === 0 ? (
                  <div className="chat-session-popover-empty">
                    {filter.trim() ? 'No matches.' : 'No sessions found.'}
                  </div>
                ) : (
                  groups.map((group) => (
                    <div className="chat-session-popover-group" key={group.label}>
                      <div className="chat-session-popover-group-label">{group.label}</div>
                      {group.items.map((item) => {
                        const k = sessionItemKey(item)
                        const run = sessionRunStatus(typeof item === 'object' ? item : {})
                        const isCurrent = k === sessionKey
                        return (
                          <button
                            type="button"
                            key={k}
                            className={`chat-session-popover-item${isCurrent ? ' is-current' : ''}`}
                            onClick={() => switchTo(k)}
                          >
                            <span className="chat-session-popover-item-key" title={k}>
                              {k}
                            </span>
                            {run.status !== 'idle' && (
                              <span
                                className={`chat-session-popover-item-run chat-session-popover-item-run--${run.status}`}
                              >
                                {run.label}
                              </span>
                            )}
                            {isCurrent && (
                              <span className="chat-session-popover-item-tag">current</span>
                            )}
                          </button>
                        )
                      })}
                    </div>
                  ))
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
