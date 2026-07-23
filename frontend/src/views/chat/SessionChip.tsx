import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { toast } from 'sonner'
import { ChevronDown, Copy, FileDown, MoreHorizontal, RotateCcw } from 'lucide-react'
import { authenticatedHeaders } from '@/lib/http-auth'
import {
  classifySessionKey,
  runStatusChipClass,
  sessionItemKey,
  sessionRunStatus,
  type RunStatusResult,
  type SessionGroup,
  type SessionListItem,
} from './logic'

/**
 * Session chip + switcher (React) — ported from the legacy topbar-center chip
 * (chat.js:1219-1229 render, 1836-2089 `_bindSessionChip`).
 *
 * One chip acts as the switcher trigger; a compact actions menu keeps copy,
 * reset, and export available inside the Chat-only floating workspace header.
 * Opening the chip fetches the session list from `/api/sessions` (chat.js:2026), grouping items via
 * `classifySessionKey` (chat.js:1862) and tagging each with its run status
 * (chat.js:1611). Selecting a session calls `onSwitch(key)` — the transcript
 * owner (ChatPage) re-points `useTranscript` at the new key, which parks the old
 * session's stream, re-subscribes, and reloads history. When the list fetch
 * fails, the popover degrades to a manual key-entry field (chat.js:2038-2069).
 */

// chat.js:1903 — the switcher group order (empty groups are skipped).
const GROUP_ORDER: SessionGroup[] = ['Web chat', 'CLI', 'Sub-agents', 'Agents', 'Sessions', 'Other']

const COMPACT_RUN_LABEL: Record<RunStatusResult['status'], string> = {
  idle: 'Idle',
  queued: 'Queue',
  running: 'Run',
  approval_pending: 'Wait',
  interrupted: 'Stop',
  failed: 'Fail',
  timeout: 'Time',
  cancelled: 'Done',
}

export interface SessionChipProps {
  /** The current (canonical) session key (chat.js:1223). */
  sessionKey: string
  /** Live current-session run state (chat.js:1767 `_applySessionRunState`). */
  runState?: RunStatusResult
  /** Switch to a different session (chat.js:1809 `_switchToSession`). */
  onSwitch: (key: string) => void
  /** Reset the current session (chat.js:2723 `sessions.reset`). */
  onReset: () => void
  /** Export the current transcript as Markdown. */
  onExport?: () => void
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
  const resp = await fetch('/api/sessions', {
    headers: authenticatedHeaders(),
    credentials: 'same-origin',
  })
  if (!resp.ok) throw new Error('Session list unavailable')
  const data = (await resp.json()) as { sessions?: SessionListItem[]; keys?: SessionListItem[] }
  const raw = data.sessions || data.keys || []
  return raw.filter((s) => !!sessionItemKey(s))
}

export function SessionChip({
  sessionKey,
  runState = sessionRunStatus(undefined),
  onSwitch,
  onReset,
  onExport,
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
  const [actionsOpen, setActionsOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const actionsTriggerRef = useRef<HTMLButtonElement>(null)
  const actionsMenuRef = useRef<HTMLDivElement>(null)

  const getSessionTrigger = useCallback(
    () => document.querySelector<HTMLButtonElement>('#chat-session-switcher-trigger'),
    [],
  )
  const getActionsTrigger = useCallback(
    () => document.querySelector<HTMLButtonElement>('#chat-session-actions-trigger'),
    [],
  )

  const focusBeforeDismiss = useCallback((target: () => HTMLElement | null) => {
    target()?.focus()
  }, [])

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
    setActionsOpen(false)
    setFilter('')
    setSessions(null)
    setFailed(false)
    setManualKey(sessionKey)
  }, [sessionKey])

  const toggle = useCallback(() => {
    setActionsOpen(false)
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

  const toggleActions = useCallback(() => {
    setOpen(false)
    setFilter('')
    setSessions(null)
    setFailed(false)
    setManualKey(sessionKey)
    setActionsOpen((wasOpen) => !wasOpen)
  }, [sessionKey])

  const runHeaderAction = useCallback(
    (action: () => void) => {
      focusBeforeDismiss(getActionsTrigger)
      action()
      dismiss()
    },
    [dismiss, focusBeforeDismiss, getActionsTrigger],
  )

  const switchTo = useCallback(
    (key: string) => {
      focusBeforeDismiss(getSessionTrigger)
      dismiss()
      if (key && key !== sessionKey) onSwitch(key)
    },
    [dismiss, focusBeforeDismiss, getSessionTrigger, onSwitch, sessionKey],
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

  useEffect(() => {
    if (!actionsOpen) return
    actionsMenuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus()
  }, [actionsOpen])

  // chat.js:2004-2020 — dismiss on outside click / Escape while open.
  useEffect(() => {
    if (!open && !actionsOpen) return
    const onDocClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) dismiss()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        const restoreTarget = actionsOpen ? getActionsTrigger : getSessionTrigger
        focusBeforeDismiss(restoreTarget)
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
  }, [open, actionsOpen, dismiss, focusBeforeDismiss, getActionsTrigger, getSessionTrigger])

  const onActionsKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      const items = Array.from(
        actionsMenuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [],
      )
      if (!items.length) return

      if (event.key === 'Tab') {
        event.preventDefault()
        const trigger = actionsTriggerRef.current
        const contextButtons = Array.from(
          trigger
            ?.closest('[data-chat-session-context]')
            ?.querySelectorAll<HTMLButtonElement>('button:not([disabled])') ?? [],
        ).filter((button) => !actionsMenuRef.current?.contains(button))
        const triggerIndex = trigger ? contextButtons.indexOf(trigger) : -1
        const target = event.shiftKey ? trigger : contextButtons[triggerIndex + 1] || trigger
        focusBeforeDismiss(() => target)
        dismiss()
        return
      }

      const current = items.indexOf(document.activeElement as HTMLButtonElement)
      let next = current
      if (event.key === 'ArrowDown') next = (current + 1) % items.length
      else if (event.key === 'ArrowUp') next = (current - 1 + items.length) % items.length
      else if (event.key === 'Home') next = 0
      else if (event.key === 'End') next = items.length - 1
      else return

      event.preventDefault()
      items[next]?.focus()
    },
    [dismiss, focusBeforeDismiss],
  )

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
        id="chat-session-switcher-trigger"
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
        <ChevronDown className="chat-session-chip-caret" aria-hidden="true" />
      </button>
      <span
        id="chat-run-status"
        className={`chip chat-session-run-status ${runStatusChipClass(runState.status)}`.trim()}
        title={[
          runState.label,
          runState.task?.task_id,
          runState.task?.queue_position ? `queue #${runState.task.queue_position}` : '',
          runState.task?.terminal_reason || runState.task?.terminalReason,
        ]
          .filter(Boolean)
          .join(' - ')}
        data-status={runState.status}
      >
        <span className="chat-session-run-status__full">{runState.label}</span>
        <span className="chat-session-run-status__compact" aria-hidden="true">
          {COMPACT_RUN_LABEL[runState.status]}
        </span>
      </span>
      <div className="chat-session-actions">
        <button
          id="chat-session-actions-trigger"
          ref={actionsTriggerRef}
          type="button"
          className="chat-session-actions-trigger"
          title="Chat actions"
          aria-label="Chat actions"
          aria-haspopup="menu"
          aria-expanded={actionsOpen}
          onClick={toggleActions}
        >
          <MoreHorizontal aria-hidden="true" />
        </button>

        {actionsOpen && (
          <div
            ref={actionsMenuRef}
            className="chat-session-actions-menu"
            role="menu"
            aria-label="Chat actions"
            onKeyDown={onActionsKeyDown}
          >
            <button
              type="button"
              className="chat-session-actions-menu__item"
              role="menuitem"
              tabIndex={-1}
              aria-label="Copy session key"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => runHeaderAction(copy)}
            >
              <Copy aria-hidden="true" />
              <span>Copy session key</span>
            </button>
            <button
              type="button"
              className="chat-session-actions-menu__item"
              role="menuitem"
              tabIndex={-1}
              aria-label="Reset session"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => runHeaderAction(onReset)}
            >
              <RotateCcw aria-hidden="true" />
              <span>Reset session</span>
            </button>
            {onExport ? (
              <button
                type="button"
                className="chat-session-actions-menu__item"
                role="menuitem"
                tabIndex={-1}
                aria-label="Export chat as Markdown"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => runHeaderAction(onExport)}
              >
                <FileDown aria-hidden="true" />
                <span>Export Markdown</span>
              </button>
            ) : null}
          </div>
        )}
      </div>

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
                  onMouseDown={(event) => event.preventDefault()}
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
                            onMouseDown={(event) => event.preventDefault()}
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
