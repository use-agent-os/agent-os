import './chat.css'
import './chat-unified.css'
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { toast } from 'sonner'
import { SquarePen, Terminal, X } from 'lucide-react'
import { AnimatePresence } from 'motion/react'
import { useRpc } from '@/app/providers'
import { ShellHeaderPortal, ShellPrimaryActionPortal } from '@/app/ShellHeaderSlot'
import { ModalShell } from '@/components/ModalShell'
import { Attachments, useAttachments } from './Attachments'
import { Composer, type ComposerHandle } from './Composer'
import {
  ACTIVE_SESSION_STORAGE_KEY,
  agentIdFromSessionKey,
  canonicalSessionKey,
  exportMarkdownDocument,
  hasPendingAttachmentWork,
  readAgentFromUrl,
  readSessionFromUrl,
  webchatSessionKey,
  type ExportMessage,
  type PendingAttachment,
} from './logic'
import { PendingQueue } from './PendingQueue'
import { resetSession as requestSessionReset } from './resetSession'
import { SessionChip } from './SessionChip'
import { SlashMenu, type SlashMenuHandle } from './SlashMenu'
import { Toolbar } from './Toolbar'
import { useApprovalPending } from './useApprovalPending'
import { usePendingQueue, type PendingComposerBridge } from './usePendingQueue'
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
 * Collect the export source from the rendered thread (chat.js:8396 iterates
 * `_messages`). The React view renders the transcript imperatively into the DOM;
 * each `.msg` row carries `data-history-role` + `data-history-raw-text`, and any
 * artifact cards carry `data-artifact-name` / `-download` / `-id`. Reading them
 * back at export time reconstructs the same `{role, text, artifacts}` rows legacy
 * mirrored into `_messages`, without a parallel reactive message store. Rows
 * without a role (separators, scope rows, router-fx sliders) are skipped.
 */
function collectExportMessages(thread: HTMLElement | null): ExportMessage[] {
  if (!thread) return []
  const out: ExportMessage[] = []
  thread.querySelectorAll<HTMLElement>('.msg[data-history-role]').forEach((row) => {
    const role = row.getAttribute('data-history-role') || ''
    if (!role) return
    // Prefer the stamped raw text; fall back to the rendered body text.
    const text =
      row.getAttribute('data-history-raw-text') ??
      (row.querySelector('.msg-body')?.textContent || '')
    // chat.js:8398 — the export emits `### role _(ts.toLocaleString())_` when the
    // message carries a ts (legacy `msg.timestamp || msg.ts`, stamped as
    // `data-history-ts`). Absent → undefined so the suffix is dropped, matching
    // legacy's `msg.ts ? … : ''` branch.
    const ts = row.getAttribute('data-history-ts') || undefined
    const artifacts = Array.from(row.querySelectorAll<HTMLElement>('[data-artifact-name]')).map(
      (card) => ({
        id: card.getAttribute('data-artifact-id') || undefined,
        name: card.getAttribute('data-artifact-name') || undefined,
        // chat.js:8425 — audio cards stamp `data-artifact-download` on the child
        // Download anchor (the card itself lacks it); fall back to that anchor so
        // audio artifacts export a real download URL like image/file cards.
        download_url:
          card.getAttribute('data-artifact-download') ||
          card.querySelector('[data-artifact-download]')?.getAttribute('data-artifact-download') ||
          undefined,
      }),
    )
    out.push({ role, text, ts, artifacts })
  })
  return out
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
  const [toolResultModal, setToolResultModal] = useState<{
    title: string
    content: string
  } | null>(null)

  // transcript/tools.ts preserves the legacy UI.modal seam and hands us a
  // small escaped <pre>. Decode it to text and let React render the content;
  // never inject tool output as HTML.
  const openToolResultModal = useCallback((title: string, html: string) => {
    const template = document.createElement('template')
    template.innerHTML = html
    setToolResultModal({ title, content: template.content.textContent || '' })
  }, [])

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

  // Message hover actions are owned by the imperative transcript builder, but
  // their backing edit/regenerate operations land in the React composer/send
  // path. These refs bridge the boundary without reactifying message rows.
  const [composerValue, setComposerValue] = useState('')
  const slashHandleRef = useRef<SlashMenuHandle>(null)
  const slashListboxId = useId()
  const [slashActiveDescendant, setSlashActiveDescendant] = useState<string>()
  const composerHandleRef = useRef<ComposerHandle>(null)
  const regenerateMessageRef = useRef<(text: string) => void>(() => {})
  const editMessage = useCallback((text: string) => {
    composerHandleRef.current?.setValue(text)
    composerHandleRef.current?.focus()
    setComposerValue(text)
  }, [])
  const regenerateMessage = useCallback((text: string) => {
    regenerateMessageRef.current(text)
  }, [])

  const {
    containerRef,
    routerFxDockRef,
    send,
    abort,
    busy,
    routerFxEnabled,
    setRouterFxEnabled,
    history,
    runState,
    isCompactInFlightForCurrentSession,
    setStreamIdlePausedForApproval,
    setPendingDelegates,
  } = useTranscript({
    sessionKey,
    openModal: openToolResultModal,
    onEditMessage: editMessage,
    onRegenerateMessage: regenerateMessage,
    onSessionKeyResolved: switchToSession,
  })
  const attachments = useAttachments()

  // The per-send session intent (chat.js:335 `_pendingSessionIntent`) — rides on
  // the next send (e.g. 'new_chat'), and is carried through the pending queue
  // (chat.js:8523/8547/8612). A ref: it is not rendered, only read at send time.
  const pendingIntentRef = useRef<string | null>(null)

  // chat.js:6091-6110 — the pending QUEUE (queued sends while streaming/compacting).
  // The bridge lets the queue write back into the composer + attachments + intent
  // on drain/recover, and read the live stream/compaction state for the debounce
  // re-check. `sendDrainedHead` is bound below to the normalize-then-send path.
  const sendDrainedHeadRef = useRef<
    (text: string, atts: PendingAttachment[], intent: string | null) => void
  >(() => {})
  const bridge: PendingComposerBridge = {
    getComposerText: () => composerHandleRef.current?.getValue() ?? '',
    setComposerText: (text) => {
      composerHandleRef.current?.setValue(text)
      setComposerValue(text)
    },
    getAttachments: () => attachments.attachments,
    setAttachments: (next) => attachments.setAll(next),
    getIntent: () => pendingIntentRef.current,
    setIntent: (intent) => {
      pendingIntentRef.current = intent
    },
    sendDrainedHead: (text, atts, intent) => sendDrainedHeadRef.current(text, atts, intent),
    isStreaming: () => busy,
    isCompactInFlight: () => isCompactInFlightForCurrentSession(),
  }
  const pending = usePendingQueue(bridge)

  // chat.js:4685-4693 + 6216-6233 — the inline-approval gate. When an approval is
  // pending for THIS session (shared `useApprovals` store — no divergent state),
  // pause the stream-idle timer + flip the run-status chip to "Waiting for
  // approval"; clearing it (resolve in the approvals view → monitor re-poll)
  // resumes 'running'. NOTE: the approve/deny BUTTONS are NOT a chat-view surface
  // (they live in the migrated approvals view); the chat thread never rendered an
  // inline approve/deny card (confirmed against chat.js). This hook is that whole
  // surface for the chat view.
  useApprovalPending(sessionKey, setStreamIdlePausedForApproval)

  // Install the pending-queue drain/recover delegates on the transcript
  // controller (chat.js:8681/8685 — the compaction-settle drives the drain; the
  // terminal-event backstop calls popAll/scheduleDrain). Kept current in an
  // effect so the controller always calls the latest closures.
  useEffect(() => {
    setPendingDelegates({
      schedulePendingDrainAfterTerminal: pending.scheduleDrainAfterTerminal,
      popAllPendingIntoComposer: pending.popAllIntoComposer,
      pendingQueueLength: () => pending.length,
    })
  }, [
    setPendingDelegates,
    pending.scheduleDrainAfterTerminal,
    pending.popAllIntoComposer,
    pending.length,
  ])

  // chat.js:8439-8450 `_onStop` — abort the turn AND recover any pending queue
  // into the composer (chat.js:8448), so a user who stops mid-turn keeps their
  // queued messages for editing rather than losing them. The composer's Abort
  // button + its ESC-while-busy rung both call this (not the bare `abort`), and
  // the doc-level ESC does the same for the from-anywhere case.
  const abortAndRecover = useCallback(
    (source = 'webui_stop_button') => {
      abort(source)
      const recovered = pending.popAllIntoComposer()
      toast.warning(recovered ? 'Stopped — pending recovered to input' : 'Stopped', {
        duration: 1800,
      })
    },
    [abort, pending],
  )

  // chat.js:2692-2715 `new_chat` — start a fresh session in the current agent.
  // The session action owns the full legacy transition: generate a new key
  // (chat.js:2696 `_genKey`) and switch to it. `switchToSession` re-points
  // `useTranscript`, which parks the outgoing session's stream, unsubscribes,
  // re-subscribes the new (empty) session, and reloads its (empty) history —
  // exactly the unsubscribe → park → new key → reset → subscribe sequence legacy
  // did inline (chat.js:2694-2712). Pending work is cleared and the next-send
  // intent is stamped here; the slash-command caller has already cleared its
  // command text, while the header action preserves any draft for the new chat.
  const onSessionAction = useCallback(
    (action: string) => {
      if (action === 'new_chat') {
        const key = genSessionKey(sessionKey)
        pending.clearAll()
        pendingIntentRef.current = 'new_chat'
        switchToSession(key)
        toast.info('New chat session in the current agent: ' + key)
      }
      // `compact_context` stays delegated to the compaction controller (Task 7)
      // via the hook's own RPC fallback — not a session-swap concern.
    },
    [pending, sessionKey, switchToSession],
  )

  const startNewChat = useCallback(() => {
    onSessionAction('new_chat')
    composerHandleRef.current?.focus()
  }, [onSessionAction])

  // chat.js:2723 `sessions.reset` — the chip's reset control + the `/reset` slash
  // command both reset the CURRENT session in place (no key change). Fire the RPC
  // and let the terminal `sessions.changed` resync history (useTranscript owns
  // that path); toast the outcome.
  const resetSession = useCallback(() => {
    void requestSessionReset(rpc, sessionKey)
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

      // chat.js:6078-6082 — normalize with the resolved slash flag (a real slash
      // command bypasses paste/page-dump normalization).
      const normalized = await attachments.normalizeForSend(text, isSlashCommand)
      if (!normalized) return // over the text hard cap; the helper already toasted.
      const outText = normalized.text
      const busyOrCompacting = busy || isCompactInFlightForCurrentSession()

      // chat.js:6091-6110 — while a turn is streaming OR a compaction is in flight,
      // Send ENQUEUES instead of sending. A slash command while busy is rejected
      // (you can't queue a command); an empty payload while busy is a no-op.
      if (busyOrCompacting) {
        if (!isLiteralSlash && outText.startsWith('/')) {
          const waitReason = isCompactInFlightForCurrentSession()
            ? 'context compaction'
            : 'the current response'
          toast.warning(`Wait for ${waitReason} before running ${outText.split(/\s+/, 1)[0]}.`, {
            duration: 2500,
          })
          return
        }
        const hasPayload = Boolean(outText.trim()) || normalized.attachments.length > 0
        if (!hasPayload) return // empty + busy = no-op (chat.js:6099)
        const compacting = isCompactInFlightForCurrentSession()
        const queued = pending.enqueue(
          {
            text: outText,
            attachments: normalized.attachments,
            intent: pendingIntentRef.current,
          },
          {
            toastMessage: compacting ? 'Message queued until compaction finishes' : undefined,
            waitReason: compacting ? 'context compaction' : 'the current response',
          },
        )
        if (queued) {
          // The enqueue cleared the composer/attachments/intent via the bridge.
          setComposerValue('')
          attachments.clear()
        }
        return
      }

      // chat.js:6113-6116 — NOT busy: intercept + execute a slash command; a
      // handled command never sends as text.
      if (isSlashCommand) {
        setComposerValue('')
        if (await executeSlash(text)) return
      }

      // chat.js:6150-6205 — the real send.
      setComposerValue('')
      const intent = pendingIntentRef.current
      pendingIntentRef.current = null
      send(outText, normalized.attachments, intent)
      attachments.clear()
    },
    [attachments, send, executeSlash, busy, isCompactInFlightForCurrentSession, pending],
  )

  useEffect(() => {
    regenerateMessageRef.current = (text: string) => {
      void onComposerSend(text)
    }
  }, [onComposerSend])

  // The drained-queue-head send (chat.js:8549). Fires the send path a live send
  // would. A drained head is already-normalized text + attachments (normalized at
  // enqueue time), so it sends directly rather than re-normalizing. Installed in
  // an effect (never a ref write during render) so the debounce-drain timer's
  // late-bound callback always reaches the latest `send` closure.
  useEffect(() => {
    sendDrainedHeadRef.current = (text, atts, intent) => {
      send(text, atts, intent)
    }
  }, [send])

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

  // chat.js:8474 Alt+↓ enqueue-current: queue the composer text as a pending item
  // (chat.js:2464-2467 → `_enqueueCurrentInput`). Reuses the same enqueue path.
  const onEnqueueCurrent = useCallback(() => {
    const text = composerHandleRef.current?.getValue() ?? ''
    if (!text && attachments.attachments.length === 0) return
    const queued = pending.enqueue({
      text,
      attachments: attachments.attachments,
      intent: pendingIntentRef.current,
    })
    if (queued) {
      setComposerValue('')
      attachments.clear()
    }
  }, [attachments, pending])

  // chat.js:8389-8409 `_exportMarkdown` — build the document from the transcript
  // and trigger a Blob download. The export source is read from the rendered
  // thread (the DOM `.msg` rows carry `data-history-role` + `data-history-raw-text`
  // + artifact cards) — the same content legacy mirrored into `_messages`.
  const onExportMarkdown = useCallback(() => {
    const messages = collectExportMessages(containerRef.current)
    const md = exportMarkdownDocument(messages, sessionKey)
    if (md === null) {
      toast.warning('No messages to export')
      return
    }
    const blob = new Blob([md], { type: 'text/markdown' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `chat-${sessionKey}.md`
    a.click()
    URL.revokeObjectURL(a.href)
    toast.info('Exported as Markdown')
  }, [containerRef, sessionKey])

  // chat.js:2518-2539 `_onDocKeydown` — the from-anywhere ESC priority chain:
  //   1. streaming        → abort the turn (which recovers pending).
  //   2. pending non-empty → recover the whole queue into the composer.
  // Visible overlays own their ESC, as do other editable targets (an ESC inside
  // a different input is theirs). The composer's
  // own ESC (Composer.tsx) handles the focused-composer case + the clear rung; a
  // guard here skips when the composer is the target so it isn't double-handled.
  useEffect(() => {
    const onDocKeydown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (e.defaultPrevented) return
      // A visible overlay's own dismiss handler takes priority (chat.js:8583-8588).
      if (
        document.querySelector('.modal-backdrop, .chat-session-popover, .chat-session-actions-menu')
      )
        return
      const target = e.target as HTMLElement | null
      const isEditable =
        !!target &&
        (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
      // An ESC inside ANY editable (incl. the composer) is handled by that
      // element's own handler — the composer runs the full chain there.
      if (isEditable) return
      if (busy) {
        e.preventDefault()
        // chat.js:8448 — the stop path also recovers pending into the composer.
        abortAndRecover('webui_escape')
        return
      }
      if (pending.length > 0) {
        e.preventDefault()
        pending.popAllIntoComposer()
      }
    }
    document.addEventListener('keydown', onDocKeydown)
    return () => document.removeEventListener('keydown', onDocKeydown)
  }, [busy, abortAndRecover, pending])

  return (
    <div className="chat-stage" onDrop={onDrop} onDragOver={onDragOver} onPaste={onPaste}>
      <h1 className="sr-only">Chat</h1>
      {/* New chat is a conversation-level action, so it lives in the floating
          Chat workspace header instead of competing with Send. */}
      <ShellPrimaryActionPortal>
        <button
          type="button"
          className="chat-new-button"
          title="New chat"
          aria-label="New chat"
          onClick={startNewChat}
        >
          <span className="chat-new-button__icon" aria-hidden="true">
            <SquarePen />
          </span>
          <span className="chat-new-button__label">New chat</span>
        </button>
      </ShellPrimaryActionPortal>
      {/* Session context is portalled into the detached Chat header so the
          switch/reset/export workflow stays close to conversation identity. */}
      <ShellHeaderPortal>
        <div className="chat-session-bar" role="group" aria-label="Chat session controls">
          <SessionChip
            sessionKey={sessionKey}
            runState={runState}
            onSwitch={switchToSession}
            onReset={resetSession}
            onExport={onExportMarkdown}
          />
        </div>
      </ShellHeaderPortal>
      <div className="chat-thread" ref={containerRef} data-history-ready="false" />
      <div className="chat-history-loading" role="status" aria-live="polite">
        <span className="chat-history-loading__dot" aria-hidden="true" />
        <span>Opening conversation…</span>
      </div>
      <PendingQueue queue={pending.queue} onRemove={pending.remove} onClearAll={pending.clearAll} />
      <Composer
        onSend={onComposerSend}
        onValueChange={setComposerValue}
        onSlashKeyDown={onSlashKeyDown}
        composerRef={composerHandleRef}
        slashListboxId={slashListboxId}
        slashActiveDescendant={slashActiveDescendant}
        slashMenu={
          <SlashMenu
            value={composerValue}
            commands={commands}
            onExecute={onMenuExecute}
            handleRef={slashHandleRef}
            listboxId={slashListboxId}
            onActiveDescendantChange={setSlashActiveDescendant}
          />
        }
        onAbort={abortAndRecover}
        busy={busy}
        history={history}
        pendingCount={pending.length}
        onRecoverPending={pending.popAllIntoComposer}
        onPopPendingTail={pending.popTail}
        onEnqueueCurrent={onEnqueueCurrent}
        pendingCompaction={isCompactInFlightForCurrentSession()}
        hasPendingAttachments={attachments.attachments.length > 0}
        hasPendingWork={hasPendingAttachmentWork(attachments.attachments)}
        onAttachFiles={attachments.addFiles}
        tray={<Attachments api={attachments} />}
        routerFxDock={
          <div id="chat-routerfx-dock" className="chat-routerfx-dock" ref={routerFxDockRef} />
        }
        toolbar={
          <Toolbar
            sessionKey={sessionKey}
            routerFxEnabled={routerFxEnabled}
            onRouterFxToggle={setRouterFxEnabled}
          />
        }
      />
      <AnimatePresence>
        {toolResultModal ? (
          <ModalShell
            role="dialog"
            labelledBy="chat-tool-result-modal-title"
            describedBy="chat-tool-result-modal-content"
            overlayClassName="chat-output-modal-overlay"
            className="chat-output-modal"
            onClose={() => setToolResultModal(null)}
          >
            <header className="chat-output-modal__header">
              <div className="chat-output-modal__identity">
                <span className="chat-output-modal__icon" aria-hidden="true">
                  <Terminal />
                </span>
                <div>
                  <div className="chat-output-modal__eyebrow">Tool output</div>
                  <h2 id="chat-tool-result-modal-title">{toolResultModal.title}</h2>
                </div>
              </div>
              <button
                type="button"
                className="chat-output-modal__close"
                aria-label="Close"
                title="Close tool output"
                onClick={() => setToolResultModal(null)}
              >
                <X aria-hidden="true" />
              </button>
            </header>
            <div className="chat-output-modal__meta">
              <span>Full result</span>
              <span>{toolResultModal.content.length.toLocaleString()} characters</span>
            </div>
            <pre id="chat-tool-result-modal-content" className="chat-tool-result-full">
              {toolResultModal.content}
            </pre>
          </ModalShell>
        ) : null}
      </AnimatePresence>
    </div>
  )
}
