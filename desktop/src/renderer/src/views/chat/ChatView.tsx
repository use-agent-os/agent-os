import './chat.css'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate, useParams } from 'react-router'
import { toast } from 'sonner'
import { ArrowDown, Download, RotateCcw, SquarePen, Terminal, X } from 'lucide-react'
import { useRpc } from '@/app/providers'
import { formatCombo, useKeyboardShortcut } from '@/components/KeyboardShortcuts'
import { ModalShell } from '@/components/ModalShell'
import { Attachments, useAttachments } from '@/views/chat/Attachments'
import {
  agentIdFromSessionKey,
  canonicalSessionKey,
  exportMarkdownDocument,
  hasPendingAttachmentWork,
  webchatSessionKey,
  type ExportMessage,
  type PendingAttachment,
} from '@/views/chat/logic'
import { PendingQueue } from '@/views/chat/PendingQueue'
import { resetSession as requestSessionReset } from '@/views/chat/resetSession'
import { RoutePicker } from '@/views/chat/RoutePicker'
import { SlashMenu, type SlashMenuHandle } from '@/views/chat/SlashMenu'
import { useApprovalPending } from '@/views/chat/useApprovalPending'
import { usePendingQueue, type PendingComposerBridge } from '@/views/chat/usePendingQueue'
import { useRoutePin } from '@/views/chat/useRoutePin'
import { useSlashCommands } from '@/views/chat/useSlashCommands'
import { useTranscript } from '@/views/chat/useTranscript'
import { t as tw } from '@/i18n'
import '@/i18n/en/chat'
import { Composer, type ComposerHandle } from '~/components/composer/Composer'
import { Button } from '~/components/ui/button'
import { sessionPath } from '~/components/sidebar/SessionList'
import { t } from '~/i18n'
import { rememberLastSession } from '~/lib/last-session'
import { ease, spring } from '~/lib/motion'
import { useGateway } from '~/stores/gateway'
import { useLive } from '~/stores/live'
import { useSettings } from '~/stores/settings'
import { useUi } from '~/stores/ui'
import { configuredProvider } from '@/views/setup/logic'
import { useConfigSnapshot } from '~/views/settings/use-snapshot'
import { ProjectChip } from './ProjectChip'
import { useDeskInstruments, type DeskProps } from '~/views/trading/desk/useDeskInstruments'
import { useTradeLedger } from '~/views/trading/desk/useTradeLedger'

const NEW_CHAT_COMBO = 'mod+shift+o'
const DEFAULT_AGENT_KEY = webchatSessionKey('main')

/** chat.js:1155 `_genKey` — a fresh webchat key in the given agent. */
function genSessionKey(currentKey: string): string {
  const suffix = Math.random().toString(36).slice(2, 10)
  return webchatSessionKey(agentIdFromSessionKey(currentKey) || 'main', suffix)
}

/** Export source read back from the rendered thread (see ChatPage.tsx). */
function collectExportMessages(thread: HTMLElement | null): ExportMessage[] {
  if (!thread) return []
  const out: ExportMessage[] = []
  thread.querySelectorAll<HTMLElement>('.msg[data-history-role]').forEach((row) => {
    const role = row.getAttribute('data-history-role') || ''
    if (!role) return
    const text =
      row.getAttribute('data-history-raw-text') ??
      (row.querySelector('.msg-body')?.textContent || '')
    const ts = row.getAttribute('data-history-ts') || undefined
    const artifacts = Array.from(row.querySelectorAll<HTMLElement>('[data-artifact-name]')).map(
      (card) => ({
        id: card.getAttribute('data-artifact-id') || undefined,
        name: card.getAttribute('data-artifact-name') || undefined,
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

function runTone(status: string): 'ok' | 'warn' | 'danger' | 'dim' {
  if (status === 'running' || status === 'queued') return 'ok'
  if (status === 'approval_pending' || status === 'interrupted') return 'warn'
  if (status === 'failed' || status === 'timeout' || status === 'cancelled') return 'danger'
  return 'dim'
}

/**
 * The conversation. This is the console's ChatPage with the desktop's chrome:
 * the session comes from the route (`/sessions/:key`, keyless = a fresh
 * session), there is no title (the sidebar names the chat), and before the
 * first send the composer sits centred under the wordmark and docks to the
 * bottom on send. The session actions go into `actionsSlot` when the route
 * gives one (the mode strip, in Chat mode), or on a row above the chat when it
 * does not (the desk). Everything else — transcript, composer, slash menu,
 * route picker, attachments, pending queue, approvals — is the shared web
 * implementation talking to the same gateway.
 */
export function ChatView({
  desk = null,
  actionsSlot,
}: {
  desk?: DeskProps | null
  /** Where the session actions go: an element, null while that element has
      not mounted yet (render nothing rather than flash them in the chat), or
      undefined for a row of their own. */
  actionsSlot?: HTMLElement | null
} = {}) {
  const gatewayState = useGateway((s) => s.status.state)
  if (gatewayState !== 'running') {
    return (
      <div className="chat-desktop-offline">
        <p className="text-[15px] font-semibold text-foreground">
          {gatewayState === 'starting'
            ? t('chat.waitingGateway')
            : t(`gateway.state.${gatewayState}`)}
        </p>
        <p className="max-w-sm">{t('chat.gatewayDown')}</p>
      </div>
    )
  }
  return <ConnectedChat desk={desk} actionsSlot={actionsSlot} />
}

function ConnectedChat({
  desk,
  actionsSlot,
}: {
  desk: DeskProps | null
  actionsSlot: HTMLElement | null | undefined
}) {
  const rpc = useRpc()
  const navigate = useNavigate()
  const reduce = useReducedMotion()
  // While the desk is powering on the chat stops springing: the hero's blur
  // exit and the composer's centre-to-bottom dock move are Motion layout
  // animations that ran straight over the entrance and read as a lurch. The
  // switch is the desk's moment; the chat just takes its place.
  const snap = Boolean(reduce) || Boolean(desk?.entering)
  const { key: rawParam } = useParams()
  const paramKey = useMemo(
    () => (rawParam ? canonicalSessionKey(decodeURIComponent(rawParam)) : ''),
    [rawParam],
  )

  // The live session key (legacy `_sessionKey`): the route's key, or a fresh
  // one the gateway creates lazily on the first chat.send. Landing back on the
  // keyless route (New session) must mint another fresh key, which is state
  // derived from the previous param: adjusted during render, not in an effect.
  const [freshKey, setFreshKey] = useState(() => genSessionKey(DEFAULT_AGENT_KEY))
  const [prevParamKey, setPrevParamKey] = useState(paramKey)
  if (prevParamKey !== paramKey) {
    setPrevParamKey(paramKey)
    if (!paramKey) setFreshKey((prev) => genSessionKey(prev))
  }
  const sessionKey = paramKey || freshKey
  // The keyless home docks its composer on the first send, before the route
  // catches up; a keyed route is docked from the start.
  const [startedKey, setStartedKey] = useState('')
  const docked = Boolean(paramKey) || startedKey === sessionKey

  // chat.js:1809 `_switchToSession` — the route is the source of truth for the
  // key, so switching is a navigation; the same route element stays mounted.
  const switchToSession = useCallback(
    (rawKey: string) => {
      const key = canonicalSessionKey(rawKey)
      if (!key || key === sessionKey) return
      void navigate(sessionPath(key), { replace: true })
    },
    [navigate, sessionKey],
  )

  const [toolResultModal, setToolResultModal] = useState<{ title: string; content: string } | null>(
    null,
  )
  const openToolResultModal = useCallback((title: string, html: string) => {
    const template = document.createElement('template')
    template.innerHTML = html
    setToolResultModal({ title, content: template.content.textContent || '' })
  }, [])

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

  // Skills → "Use in chat" leaves text in the UI store. Drop it into the
  // composer of whichever chat is showing, once, and forget it. Nothing is
  // sent: the user still presses Return. Two paths in: a prompt written while
  // this chat is up (the subscription), or one written just before it mounted
  // (the deferred first read, after the composer ref has attached).
  useEffect(() => {
    const drain = () => {
      const text = useUi.getState().pendingPrompt
      if (!text) return
      useUi.getState().setPendingPrompt(null)
      editMessage(text)
    }
    const first = window.setTimeout(drain, 0)
    const unsubscribe = useUi.subscribe((s, prev) => {
      if (s.pendingPrompt && s.pendingPrompt !== prev.pendingPrompt) drain()
    })
    return () => {
      window.clearTimeout(first)
      unsubscribe()
    }
  }, [editMessage])

  const route = useRoutePin(rpc, sessionKey)

  // The desk's ledger decorates the same element the transcript renders into;
  // its seams must be handed to the transcript before it mounts. Created for
  // every chat (idle when there is no desk) so the hook order never changes.
  const [focusOrderId, setFocusOrderId] = useState<string | null>(null)
  const onFocusApproval = useCallback((id: string | null) => {
    if (id) setFocusOrderId(id)
  }, [])
  const ledger = useTradeLedger(onFocusApproval, sessionKey)

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
    pinnedToTail,
    scrollToTail,
    isCompactInFlightForCurrentSession,
    setStreamIdlePausedForApproval,
    setPendingDelegates,
  } = useTranscript({
    sessionKey,
    seams: desk ? ledger.seams : undefined,
    openModal: openToolResultModal,
    onEditMessage: editMessage,
    onRegenerateMessage: regenerateMessage,
    onSessionKeyResolved: switchToSession,
    routePinned: route.isPinned,
  })
  const attachments = useAttachments()
  useEffect(() => {
    if (desk) ledger.bind(containerRef.current)
    else ledger.unbind()
  })

  // The router animation strip is a console-only flourish; the desktop shows
  // the route in the composer pill instead.
  useEffect(() => {
    if (routerFxEnabled) setRouterFxEnabled(false)
  }, [routerFxEnabled, setRouterFxEnabled])

  // Enter animations are for rows that arrive one at a time (a send, a reply).
  // The shared renderer also rebuilds the whole thread after a turn settles
  // (history resync); replaying the animation on every row then reads as a
  // flash. Mark bulk inserts so the skin leaves them still.
  const [hasMessages, setHasMessages] = useState(false)
  useEffect(() => {
    const th = containerRef.current
    if (!th) return
    const observer = new MutationObserver((records) => {
      const added: HTMLElement[] = []
      for (const record of records) {
        record.addedNodes.forEach((node) => {
          if (node instanceof HTMLElement && node.classList.contains('msg')) added.push(node)
        })
      }
      if (added.length > 1) for (const el of added) el.dataset.enter = 'none'
      setHasMessages(th.querySelector('.msg') !== null)
    })
    observer.observe(th, { childList: true, subtree: true })
    return () => observer.disconnect()
  }, [containerRef, sessionKey])

  // Sidebar signal light for THIS session while a turn streams.
  const setLive = useLive((s) => s.setLive)
  useEffect(() => {
    setLive(sessionKey, busy)
    return () => setLive(sessionKey, false)
  }, [sessionKey, busy, setLive])

  // "Open at launch: Last session" reads this back (AppShell).
  useEffect(() => {
    if (paramKey) rememberLastSession(paramKey)
  }, [paramKey])

  const enterToSend = useSettings((s) => s.settings.general.enterToSend)

  const pendingIntentRef = useRef<string | null>(null)
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

  useApprovalPending(sessionKey, setStreamIdlePausedForApproval)

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

  const abortAndRecover = useCallback(
    (source = 'desktop_stop_button') => {
      abort(source)
      const recovered = pending.popAllIntoComposer()
      toast.warning(recovered ? 'Stopped — pending recovered to input' : 'Stopped', {
        duration: 1800,
      })
    },
    [abort, pending],
  )

  const startNewChat = useCallback(() => {
    pending.clearAll()
    pendingIntentRef.current = 'new_chat'
    void navigate('/sessions')
  }, [navigate, pending])

  const onSessionAction = useCallback(
    (action: string) => {
      if (action === 'new_chat') startNewChat()
    },
    [startNewChat],
  )

  const resetSession = useCallback(() => {
    void requestSessionReset(rpc, sessionKey)
  }, [rpc, sessionKey])

  const { commands, execute: executeSlash } = useSlashCommands({ sessionKey, onSessionAction })

  const onComposerSend = useCallback(
    async (rawText: string) => {
      let text = rawText
      let isLiteralSlash = false
      if (text.startsWith('//')) {
        isLiteralSlash = true
        text = text.slice(1)
      }
      const isSlashCommand = !isLiteralSlash && text.startsWith('/')
      const normalized = await attachments.normalizeForSend(text, isSlashCommand)
      if (!normalized) return
      const outText = normalized.text
      const busyOrCompacting = busy || isCompactInFlightForCurrentSession()

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
        if (!hasPayload) return
        const compacting = isCompactInFlightForCurrentSession()
        const queued = pending.enqueue(
          { text: outText, attachments: normalized.attachments, intent: pendingIntentRef.current },
          {
            toastMessage: compacting ? 'Message queued until compaction finishes' : undefined,
            waitReason: compacting ? 'context compaction' : 'the current response',
          },
        )
        if (queued) {
          setComposerValue('')
          attachments.clear()
        }
        return
      }

      if (isSlashCommand) {
        setComposerValue('')
        if (await executeSlash(text)) return
      }

      setComposerValue('')
      const intent = pendingIntentRef.current
      pendingIntentRef.current = null
      send(outText, normalized.attachments, intent)
      attachments.clear()
      // First send from the keyless home: dock the composer and give the
      // session its URL. Same route element, so nothing remounts.
      if (!docked) {
        setStartedKey(sessionKey)
        void navigate(sessionPath(sessionKey), { replace: true })
      }
    },
    [
      attachments,
      send,
      executeSlash,
      busy,
      isCompactInFlightForCurrentSession,
      pending,
      docked,
      navigate,
      sessionKey,
    ],
  )

  useEffect(() => {
    regenerateMessageRef.current = (text: string) => {
      void onComposerSend(text)
    }
  }, [onComposerSend])

  useEffect(() => {
    sendDrainedHeadRef.current = (text, atts, intent) => {
      send(text, atts, intent)
    }
  }, [send])

  // The desk's own way in: a rejection reason, a mission prompt. The first
  // send from the desk files its session into the "Trading desk" project.
  const deskSentOnce = useRef(false)
  const deskSendText = useCallback(
    (text: string) => {
      send(text, [], pendingIntentRef.current)
      pendingIntentRef.current = null
      if (desk && !deskSentOnce.current) {
        deskSentOnce.current = true
        desk.onFirstSend()
      }
    },
    [send, desk],
  )
  const deskSubmitText = useCallback((text: string) => void onComposerSend(text), [onComposerSend])
  // While a turn streams a direct send is dropped, so a message the agent
  // must read (a rejection reason) is queued for the next turn instead. The
  // queue clears the composer on enqueue; the user's draft is put back.
  const deskQueueText = useCallback(
    (text: string) => {
      const draft = composerHandleRef.current?.getValue() ?? ''
      const atts = attachments.attachments
      const intent = pendingIntentRef.current
      const queued = pending.enqueue(
        { text, attachments: [], intent: null },
        { toastMessage: t('trading.chat.queuedForAgent') },
      )
      if (queued) {
        composerHandleRef.current?.setValue(draft)
        setComposerValue(draft)
        attachments.setAll(atts)
        pendingIntentRef.current = intent
      }
    },
    [attachments, pending],
  )
  const instruments = useDeskInstruments(desk, {
    sessionKey,
    sendText: deskSendText,
    queueText: deskQueueText,
    submitText: deskSubmitText,
    busy,
    composerValue,
    idle: runState.status === 'idle',
    hasMessages,
    focusOrderId,
    setFocusOrderId,
  })
  const lastSentRef = useRef('')
  useEffect(() => {
    // Any send from the desk's composer counts as its first send too.
    if (!desk || deskSentOnce.current) return
    const last = history[history.length - 1] ?? ''
    if (last && last !== lastSentRef.current) {
      lastSentRef.current = last
      deskSentOnce.current = true
      desk.onFirstSend()
    }
  }, [history, desk])

  const onSlashKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>): boolean =>
      slashHandleRef.current?.handleKeyDown(e) ?? false,
    [],
  )

  const onMenuExecute = useCallback(
    (text: string) => {
      composerHandleRef.current?.clear()
      setComposerValue('')
      void onComposerSend(text)
    },
    [onComposerSend],
  )

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

  useKeyboardShortcut(
    {
      combo: NEW_CHAT_COMBO,
      description: tw('shell.shortcutNewChat'),
      category: tw('shell.shortcutCategoryChat'),
      allowInInputs: true,
    },
    (e) => {
      e.preventDefault()
      startNewChat()
    },
  )
  useKeyboardShortcut(
    {
      combo: 'escape',
      description: tw('shell.shortcutAbortTurn'),
      category: tw('shell.shortcutCategoryChat'),
    },
    (e) => {
      if (busy) {
        e.preventDefault()
        abortAndRecover('desktop_escape')
        return
      }
      if (pending.length > 0) {
        e.preventDefault()
        pending.popAllIntoComposer()
      }
    },
  )

  // Reply notifications for this and every other session come from the
  // shell's session-run watcher (lib/use-notifications), not from here.

  // No title row: the sidebar already names the chat. What the header held
  // besides the title is the session's own actions, placed where the route
  // asks (see `actionsSlot`).
  const actions = docked ? (
    <div className="chat-desktop-actions" role="group" aria-label={tw('chat.sessionControls')}>
      <ProjectChip sessionKey={sessionKey} />
      {runState.status !== 'idle' ? (
        <span
          className="chat-desktop-actions__state"
          data-tone={runTone(runState.status)}
          title={runState.label}
        >
          <span className="chat-desktop-actions__state-text">{runState.label}</span>
        </span>
      ) : null}
      <Button
        variant="ghost"
        size="icon"
        aria-label={desk ? t('trading.chat.fresh') : t('chat.newChat')}
        title={
          desk ? t('trading.chat.fresh') : `${t('chat.newChat')} (${formatCombo(NEW_CHAT_COMBO)})`
        }
        onClick={desk ? desk.onStartFresh : startNewChat}
        data-testid={desk ? 'chat-fresh' : undefined}
      >
        <SquarePen className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        aria-label={t('chat.reset')}
        title={t('chat.reset')}
        onClick={resetSession}
      >
        <RotateCcw className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        aria-label={t('chat.export')}
        title={t('chat.export')}
        onClick={onExportMarkdown}
      >
        <Download className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
      </Button>
    </div>
  ) : null

  return (
    <div
      className="chat-desktop"
      data-docked={docked}
      data-desk={desk ? 'true' : undefined}
      data-still={instruments.still || undefined}
    >
      {actions && actionsSlot === undefined ? (
        <div className="chat-desktop-header">{actions}</div>
      ) : null}
      {actions && actionsSlot ? createPortal(actions, actionsSlot) : null}

      <div className="chat-stage" onDrop={onDrop} onDragOver={onDragOver} onPaste={onPaste}>
        <h1 className="sr-only">{tw('chat.srTitle')}</h1>
        <div className="chat-thread" ref={containerRef} data-history-ready="false" />
        <div className="chat-history-loading" role="status" aria-live="polite">
          <span className="chat-history-loading__dot" aria-hidden="true" />
          <span>{desk ? t('trading.chat.opening') : tw('chat.opening')}</span>
        </div>
        {instruments.emptyHint}

        {instruments.region}

        <AnimatePresence initial={false}>
          {!docked ? (
            <motion.div
              key="hero"
              exit={snap ? undefined : { opacity: 0, y: -28, filter: 'blur(6px)' }}
              transition={ease}
              className="chat-desktop-hero"
            >
              <span className="wordmark">{t('shell.brand')}</span>
              <p className="max-w-md text-[13px] leading-relaxed text-dim">{t('shell.tagline')}</p>
              <NoProviderNotice />
            </motion.div>
          ) : null}
        </AnimatePresence>

        <motion.div
          layout
          transition={snap ? { duration: 0 } : spring}
          className={docked ? 'shrink-0' : 'flex flex-1 flex-col justify-center pt-24'}
        >
          <motion.div layout="position" transition={snap ? { duration: 0 } : spring}>
            {/* Zero-height dock at the top of the composer block: the pill floats
                over the tail of the transcript, clear of the desk toolbar, and
                never takes layout space from either. */}
            <div className="chat-jump-dock" data-visible={pinnedToTail ? 'false' : 'true'}>
              <button
                type="button"
                className="chat-jump-to-latest"
                tabIndex={pinnedToTail ? -1 : 0}
                onClick={scrollToTail}
                title={tw('chat.jumpToLatest')}
              >
                <ArrowDown className="size-3.5" strokeWidth={2} aria-hidden />
                <span>{tw('chat.jumpToLatest')}</span>
              </button>
            </div>
            {instruments.dockAbove}
            <PendingQueue
              queue={pending.queue}
              onRemove={pending.remove}
              onClearAll={pending.clearAll}
            />
            <Composer
              enterToSend={enterToSend}
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
              routePicker={<RoutePicker route={route} />}
              seats={instruments.seats}
              placeholder={instruments.placeholder}
              onFocusChange={instruments.onFocusChange}
            />
          </motion.div>
        </motion.div>
      </div>

      {instruments.modal}

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
                  <div className="chat-output-modal__eyebrow">{tw('chat.toolOutputEyebrow')}</div>
                  <h2 id="chat-tool-result-modal-title">{toolResultModal.title}</h2>
                </div>
              </div>
              <button
                type="button"
                className="chat-output-modal__close"
                aria-label={tw('common.close')}
                title={tw('chat.toolOutputClose')}
                onClick={() => setToolResultModal(null)}
              >
                <X aria-hidden="true" />
              </button>
            </header>
            <div className="chat-output-modal__meta">
              <span>{tw('chat.toolOutputFull')}</span>
              <span>
                {tw('chat.toolOutputChars', {
                  count: toolResultModal.content.length.toLocaleString(),
                })}
              </span>
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

/**
 * Home with a gateway but no provider: the app was just installed (or the
 * provider step was skipped). One line and one button, straight to the
 * Providers section; nothing else to do first.
 */
function NoProviderNotice() {
  const { snapshot } = useConfigSnapshot()
  const openSettings = useUi((s) => s.openSettings)
  if (!snapshot) return null
  if (configuredProvider(snapshot.status ?? {}, snapshot.config ?? {})) return null
  return (
    <div className="chat-noprovider" role="status" data-testid="chat-no-provider">
      <span>{t('chat.noProvider')}</span>
      <Button variant="primary" onClick={() => openSettings('providers')}>
        {t('chat.chooseProvider')}
      </Button>
    </div>
  )
}
