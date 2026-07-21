import './chat.css'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { Attachments, useAttachments } from './Attachments'
import { Composer, type ComposerHandle } from './Composer'
import {
  ACTIVE_SESSION_STORAGE_KEY,
  agentIdFromSessionKey,
  canonicalSessionKey,
  hasPendingAttachmentWork,
  readAgentFromUrl,
  readSessionFromUrl,
  webchatSessionKey,
} from './logic'
import { SessionChip } from './SessionChip'
import { SlashMenu, type SlashMenuHandle } from './SlashMenu'
import { Toolbar } from './Toolbar'
import { useSlashCommands } from './useSlashCommands'
import { useTranscript } from './useTranscript'

// chat.js:1155-1157 `_genKey` — a fresh webchat key in the CURRENT agent, with a
// random suffix, so `/new` (and the new-chat button) start an empty session.
function genSessionKey(currentKey: string): string {
  return webchatSessionKey(
    agentIdFromSessionKey(currentKey),
    Math.random().toString(36).slice(2, 10),
  )
}

// chat.js:1211-1214 — the initial session key priority: URL `?session=` >
// `?agent=` (→ its webchat key) > localStorage > the canonical webchat default.
function resolveInitialSessionKey(search: string): string {
  const urlSession = readSessionFromUrl(search) ?? ''
  const urlAgent = readAgentFromUrl(search) ?? ''
  let stored = ''
  try {
    stored = localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY) || ''
  } catch {
    stored = ''
  }
  return canonicalSessionKey(urlSession || (urlAgent ? webchatSessionKey(urlAgent) : stored))
}

/**
 * Chat view — full-bleed shell.
 *
 * Mounts the scroll thread region (owned by the transcript controller) above a
 * pinned composer row. Task 9 adds the attachment surface: the pending buffer +
 * tray (`useAttachments` / `<Attachments>`), drag-and-drop + image-paste onto
 * the thread (chat.js:2543-2572), and the normalize-then-send path that threads
 * attachments into `chat.send` (chat.js:6078/6157).
 */
export function ChatPage() {
  const rpc = useRpc()
  const [searchParams, setSearchParams] = useSearchParams()

  // The active session key is REACTIVE state (legacy `_sessionKey`, chat.js:1170)
  // — changing it re-points `useTranscript`, which parks the old session's stream,
  // re-subscribes, and reloads history (the React equivalent of legacy's
  // imperative `_switchToSession`, chat.js:1809). Seeded once from the URL /
  // stored key priority (chat.js:1211-1214), reading the URL through react-router
  // (`useSearchParams`) so it works under a MemoryRouter / basename, not just the
  // raw `window.location`.
  const [sessionKey, setSessionKey] = useState(() =>
    resolveInitialSessionKey('?' + searchParams.toString()),
  )

  // chat.js:1167-1180 `_persistSession` — mirror the canonical key into
  // localStorage + the URL `?session=` (dropping `?agent=`) so a reload / shared
  // link reopens the same session. Kept as a ref-free callback; the URL write
  // goes through react-router's `setSearchParams` (replace, no history entry).
  const persistSession = useCallback(
    (key: string) => {
      try {
        localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, key)
      } catch {
        /* storage unavailable — non-fatal */
      }
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          next.set('session', key)
          next.delete('agent')
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  // chat.js:1809 `_switchToSession` — canonicalize, then re-point the transcript
  // (state change) + persist. A no-op when the key is unchanged (chat.js:1810).
  const switchToSession = useCallback(
    (rawKey: string) => {
      const key = canonicalSessionKey(rawKey)
      if (!key || key === sessionKey) return
      setSessionKey(key)
      persistSession(key)
    },
    [sessionKey, persistSession],
  )

  // Persist the initial resolved key on mount (legacy calls `_persistSession`
  // immediately after resolving it — chat.js:1215) so the URL/storage reflect the
  // canonical key even when the tab opened with a bare `?agent=` or nothing.
  const persistedInitialRef = useRef(false)
  useEffect(() => {
    if (persistedInitialRef.current) return
    persistedInitialRef.current = true
    persistSession(sessionKey)
    // Only on mount: subsequent persists ride through switchToSession / new_chat.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const { containerRef, send, abort, busy, history } = useTranscript({ sessionKey })
  const attachments = useAttachments()

  // The composer value mirror (chat.js:2639 `_textarea.value`) — drives the slash
  // menu's open/filter state. Owned here so the menu + composer share one value.
  const [composerValue, setComposerValue] = useState('')
  const slashHandleRef = useRef<SlashMenuHandle>(null)
  const composerHandleRef = useRef<ComposerHandle>(null)

  // chat.js:2692-2715 `new_chat` — start a fresh session in the current agent.
  // Task 10 left `onSessionAction('new_chat', …)` UNWIRED (the session-swap
  // primitives were a later task — THIS one). We now own them: generate a new
  // key (chat.js:2696 `_genKey`) and switch to it. `switchToSession` re-points
  // `useTranscript`, which parks the outgoing session's stream, unsubscribes,
  // re-subscribes the new (empty) session, and reloads its (empty) history —
  // exactly the unsubscribe → park → new key → reset → subscribe sequence legacy
  // did inline (chat.js:2694-2712). The composer clear + empty-state repaint are
  // handled by the composer / transcript on the key change.
  const onSessionAction = useCallback(
    (action: string) => {
      if (action === 'new_chat') {
        const key = genSessionKey(sessionKey)
        switchToSession(key)
        toast.info('New chat session in the current agent: ' + key)
      }
      // `compact_context` stays delegated to the compaction controller (Task 7)
      // via the hook's own RPC fallback — not a session-swap concern.
    },
    [sessionKey, switchToSession],
  )

  // chat.js:2723 `sessions.reset` — the chip's reset control + the `/reset` slash
  // command both reset the CURRENT session in place (no key change). Fire the RPC
  // and let the terminal `sessions.changed` resync history (useTranscript owns
  // that path); toast the outcome.
  const resetSession = useCallback(() => {
    rpc
      .call('sessions.reset', { key: sessionKey })
      .then(() => toast.info('Session reset'))
      .catch((err: unknown) =>
        toast.error('Reset failed: ' + (err instanceof Error ? err.message : String(err))),
      )
  }, [rpc, sessionKey])

  // Slash catalog + execution (chat.js:2615/2842). `new_chat` is now WIRED through
  // `onSessionAction` (this task owns the session-swap primitives); every
  // RPC-backed command (reset/usage/model/router.hold) already worked.
  const { commands, execute: executeSlash } = useSlashCommands({ sessionKey, onSessionAction })

  useEffect(() => {
    document.title = 'Chat - AgentOS Control'
  }, [])

  // The composer's send. Resolves the Task-9 `//` literal-slash escape + the
  // slash-command interception, verbatim from `_onSend` (chat.js:6062-6118):
  //   1. `//…`  → strip ONE leading `/`, send as a LITERAL message (not a command).
  //   2. `/cmd` → intercept + execute the slash command; do NOT send as text.
  //   3. else   → normalize (large-paste / page-dump → generated .txt) + chat.send.
  const onComposerSend = useCallback(
    async (rawText: string) => {
      let text = rawText
      let isLiteralSlash = false
      // chat.js:6072-6076 — `//` escape: strip one slash, mark literal.
      if (text.startsWith('//')) {
        isLiteralSlash = true
        text = text.slice(1)
      }
      // chat.js:6077 — a real (non-escaped) `/`-prefixed line is a slash command.
      const isSlashCommand = !isLiteralSlash && text.startsWith('/')

      // chat.js:6113-6116 — intercept + execute; a handled command never sends as
      // text. (The streaming-enqueue branch at chat.js:6091 is a Task-13 seam; a
      // send while busy is currently a no-op in useTranscript.send.)
      if (isSlashCommand) {
        setComposerValue('')
        if (await executeSlash(text)) return
      }

      // chat.js:6078-6082 — normalize with the resolved slash flag (a real slash
      // command bypasses paste/page-dump normalization; here it already returned).
      const normalized = await attachments.normalizeForSend(text, isSlashCommand)
      if (!normalized) return // over the text hard cap; the helper already toasted.
      setComposerValue('')
      send(normalized.text, normalized.attachments)
      attachments.clear()
    },
    [attachments, send, executeSlash],
  )

  // The composer's slash-key intercept — consult the menu handle before the
  // composer runs its own history/send/ESC handling (chat.js:2654-2662/2675).
  const onSlashKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>): boolean =>
      slashHandleRef.current?.handleKeyDown(e) ?? false,
    [],
  )

  // A menu selection (Enter/click) executes the command AND clears the composer
  // textarea (chat.js:2685-2687 `_selectSlashCmd` closes + clears then runs). The
  // keyboard-Enter path already clears via the composer's doSend, but a mouse
  // click bypasses it, so clear imperatively here for both.
  const onMenuExecute = useCallback(
    (text: string) => {
      composerHandleRef.current?.clear()
      setComposerValue('')
      void onComposerSend(text)
    },
    [onComposerSend],
  )

  // chat.js:2543-2555 — drag-and-drop files onto the thread stage the files.
  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      if (e.dataTransfer?.files?.length) attachments.addFiles(e.dataTransfer.files)
    },
    [attachments],
  )
  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
  }, [])

  // chat.js:2557-2571 — clipboard image paste stages the image(s) as attachments.
  const onPaste = useCallback(
    (e: React.ClipboardEvent) => {
      const items = e.clipboardData?.items
      if (!items) return
      const files: File[] = []
      for (let i = 0; i < items.length; i++) {
        const item = items[i]
        if (item && item.type.startsWith('image/')) {
          const file = item.getAsFile()
          if (file) files.push(file)
        }
      }
      if (files.length > 0) {
        attachments.addFiles(files)
        e.preventDefault()
      }
    },
    [attachments],
  )

  return (
    <div className="chat-stage" onDrop={onDrop} onDragOver={onDragOver} onPaste={onPaste}>
      {/* Session chip + switcher (chat.js:1219-1229 topbar-center). The React
          view owns its own header row (no shared topbar-center slot); the chip
          drives switch / copy / reset over the reactive session key. */}
      <header className="chat-session-bar">
        <SessionChip sessionKey={sessionKey} onSwitch={switchToSession} onReset={resetSession} />
      </header>
      <div className="chat-thread" ref={containerRef} />
      <Composer
        onSend={onComposerSend}
        onValueChange={setComposerValue}
        onSlashKeyDown={onSlashKeyDown}
        composerRef={composerHandleRef}
        slashMenu={
          <SlashMenu
            value={composerValue}
            commands={commands}
            onExecute={onMenuExecute}
            handleRef={slashHandleRef}
          />
        }
        onAbort={abort}
        busy={busy}
        history={history}
        hasPendingAttachments={attachments.attachments.length > 0}
        hasPendingWork={hasPendingAttachmentWork(attachments.attachments)}
        onAttachFiles={attachments.addFiles}
        tray={<Attachments api={attachments} />}
        toolbar={<Toolbar sessionKey={sessionKey} />}
      />
    </div>
  )
}
