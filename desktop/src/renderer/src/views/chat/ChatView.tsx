import './chat.css'
import './acp.css'
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
  normalizeOutgoingComposerPayload,
  webchatSessionKey,
  type ExportMessage,
  type PendingAttachment,
} from '@/views/chat/logic'
import { PendingQueue } from '@/views/chat/PendingQueue'
import { resetSession as requestSessionReset } from '@/views/chat/resetSession'
import { RoutePicker } from '@/views/chat/RoutePicker'
import { SlashMenu, type SlashMenuHandle } from '@/views/chat/SlashMenu'
import { Toolbar as RunModes } from '@/views/chat/Toolbar'
import { useApprovalPending } from '@/views/chat/useApprovalPending'
import { useNextSuggestion } from '@/views/chat/useNextSuggestion'
import { usePendingQueue, type PendingComposerBridge } from '@/views/chat/usePendingQueue'
import { useRoutePin } from '@/views/chat/useRoutePin'
import { useSlashCommands } from '@/views/chat/useSlashCommands'
import type { LpActions } from '@/views/chat/transcript/lp'
import type { MarketsSwapHandler } from '@/views/chat/transcript/markets'
import { useTranscript } from '@/views/chat/useTranscript'
import { t as tw } from '@/i18n'
import '@/i18n/en/chat'
import { Composer, type ComposerHandle } from '~/components/composer/Composer'
import { Button } from '~/components/ui/button'
import { sessionPath } from '~/components/sidebar/SessionList'
import { t } from '~/i18n'
import { rememberLastSession } from '~/lib/last-session'
import { useQuickAskSend } from '~/lib/use-quick-ask'
import { ease, spring } from '~/lib/motion'
import { useGateway } from '~/stores/gateway'
import { useTradingUi } from '~/stores/trading-ui'
import { useLive } from '~/stores/live'
import { useQuickAsk } from '~/stores/quick-ask'
import { useSettings } from '~/stores/settings'
import { useUi } from '~/stores/ui'
import { configuredProvider } from '@/views/setup/logic'
import { useConfigSnapshot } from '~/views/settings/use-snapshot'
import { AcpSessionBar } from './AcpSessionBar'
import { ProjectChip } from './ProjectChip'
import { useAcpSession } from './useAcpSession'
import { useDeskInstruments, type DeskProps } from '~/views/trading/desk/useDeskInstruments'
import { useTradeLedger } from '~/views/trading/desk/useTradeLedger'
import type { Token } from '~/views/trading/types'
import {
  requireBracketTouchId,
  requireMandateTouchId,
  requireTriggerTouchId,
} from '~/views/trading/touch-id'

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

/** One line of a longer text, for a quiet mention of it. */
function excerptLine(text: string, max = 80): string {
  const line = text.replace(/\s+/g, ' ').trim()
  return line.length > max ? `${line.slice(0, max - 1)}…` : line
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
  // A Quick Ask that arrived while the gateway is down waits here, and is
  // sent by the chat once the gateway is back. Say so; never drop it silently.
  const quickAskWaiting = useQuickAsk((s) => s.queue[0]?.text ?? null)
  if (gatewayState !== 'running') {
    return (
      <div className="chat-desktop-offline">
        <p className="text-[15px] font-semibold text-foreground">
          {gatewayState === 'starting'
            ? t('chat.waitingGateway')
            : t(`gateway.state.${gatewayState}`)}
        </p>
        <p className="max-w-sm">{t('chat.gatewayDown')}</p>
        {quickAskWaiting ? (
          <p className="max-w-sm" data-testid="quick-ask-waiting">
            {t('quickAsk.waiting')} <q>{excerptLine(quickAskWaiting)}</q>
          </p>
        ) : null}
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

  // A new session keeps this view, and so the composer, mounted: its
  // focus-on-mount does not run again, and focus stays wherever it was (the
  // transcript, the sidebar) or falls to <body>. Take it back on every fresh
  // session (#3524):
  // - landing on the keyless home, which mints a new `freshKey`. On purpose
  //   that is any way there, also closing or deleting the open chat: the home
  //   is a blank chat, and a freshly mounted one focuses itself too;
  // - a new-session action that asked for it: ⌘N, ⌘⇧O, `/new` and the
  //   sidebar's New session on the home itself, where nothing changes, and
  //   the desk's "Start fresh" once it has minted its new key.
  const composerFocusRequest = useUi((s) => s.composerFocusRequest)
  useEffect(() => {
    composerHandleRef.current?.focus()
  }, [freshKey, composerFocusRequest])

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

  // At the desk an LP card's Collect/Remove calls the write RPC over this
  // (operator) connection; the order parks, and its approval card joins the
  // desk's own asks — it has no session, so the desk is told its id. A plain
  // chat hands the transcript nothing, and the cards carry no buttons. A DCA
  // card (docs/dca.md) takes the same pair: its Approve & start, Pause, Buy
  // now and Stop call `trading.dca.*` here, and a Buy now that parks an order
  // lands on that order's card.
  const [ownOrderIds, setOwnOrderIds] = useState<ReadonlySet<string>>(() => new Set())
  const atDesk = desk !== null
  const lpActions = useMemo<LpActions | null>(
    () =>
      atDesk
        ? {
            // A DCA card's "Approve & start" is an approval like the desk's:
            // Touch ID first when Settings › Security says so.
            call: async (method, params) => {
              if (method === 'trading.dca.approve')
                await requireMandateTouchId(
                  (m, p) => rpc.call(m, p),
                  String(params.mandateId ?? ''),
                )
              // A trigger card's "Approve & arm" (docs/triggers.md) is gated the same way.
              if (method === 'trading.trigger.approve')
                await requireTriggerTouchId(
                  (m, p) => rpc.call(m, p),
                  String(params.triggerId ?? ''),
                )
              // A bracket card's "Approve & arm" (one decision for both legs)
              // and its "Sell now", which trades at once (docs/brackets.md).
              if (method === 'trading.bracket.approve' || method === 'trading.bracket.fire')
                await requireBracketTouchId(
                  (m, p) => rpc.call(m, p),
                  String(params.bracketId ?? ''),
                  method === 'trading.bracket.fire' ? 'fire' : 'approve',
                )
              return rpc.call(method, params)
            },
            onOrder: (orderId) => {
              setOwnOrderIds((prev) => new Set(prev).add(orderId))
              setFocusOrderId(orderId)
            },
          }
        : null,
    [atDesk, rpc],
  )

  // A markets card's Swap (docs/markets.md) fills the desk's ticket: the card
  // names two addresses, the ticket wants tokens, so both are resolved over
  // this connection first. A plain chat has no ticket, so no button there.
  const requestSwap = useTradingUi((s) => s.requestSwap)
  const onMarketsSwap = useMemo<MarketsSwapHandler | null>(
    () =>
      atDesk
        ? (swap) => {
            const resolve = (address: string) =>
              rpc.call<{ token: Token }>('trading.tokens.resolve', {
                chainId: swap.chainId,
                address,
              })
            void Promise.all([resolve(swap.tokenIn), resolve(swap.tokenOut)])
              .then(([tokenIn, tokenOut]) =>
                requestSwap({
                  chainId: swap.chainId,
                  tokenIn: tokenIn.token,
                  tokenOut: tokenOut.token,
                }),
              )
              .catch(() => {
                /* an unknown token: the ticket stays as it is */
              })
          }
        : null,
    [atDesk, rpc, requestSwap],
  )

  // "Open session" on a coding harness's cards: a real navigation (pushed,
  // so Back returns to the parent chat), unlike the replace of a key change.
  const openSession = useCallback(
    (rawKey: string) => {
      const key = canonicalSessionKey(rawKey)
      if (!key || key === sessionKey) return
      void navigate(sessionPath(key))
    },
    [navigate, sessionKey],
  )

  // "Open in Flows" on an Image Flow run card (docs/flows.md): the editor
  // for that flow, pushed so Back returns to this chat.
  const openFlow = useCallback(
    (flowId: string) => {
      if (!flowId) return
      void navigate(`/flows/${encodeURIComponent(flowId)}`)
    },
    [navigate],
  )

  const {
    containerRef,
    routerFxDockRef,
    send,
    abort,
    busy,
    routerFxEnabled,
    setRouterFxEnabled,
    history,
    addSystemMessage,
    runState,
    pinnedToTail,
    scrollToTail,
    isCompactInFlightForCurrentSession,
    compactContext,
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
    lpActions,
    dcaActions: lpActions,
    triggerActions: lpActions,
    onMarketsSwap,
    onOpenSession: openSession,
    onOpenFlow: openFlow,
  })
  const attachments = useAttachments()
  // A coding harness's own chat (`agent:<agent>:acp:<id>`): its bar, and a
  // composer that says so once the session was ended.
  const acp = useAcpSession(
    sessionKey,
    busy || ['queued', 'running', 'approval_pending'].includes(runState.status),
  )
  const acpClosedReason =
    acp.confirmed && acp.state === 'closed'
      ? t('acp.composer.closed').replace('{name}', acp.name)
      : null
  // Ghost-text next prompt in the composer, asked for when a turn ends.
  const nextSuggestion = useNextSuggestion(rpc, sessionKey, busy)
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
  // `new_chat` belongs to the keyless home's fresh key. This view stays
  // mounted across sessions, so landing on a keyed one (a sidebar session, a
  // project folder's New chat, which creates its row first) must drop it, or
  // the first send there is rejected as a session_key conflict (#3612). The
  // home's own first send consumes the intent before it gives the key a URL.
  useEffect(() => {
    if (paramKey) pendingIntentRef.current = null
  }, [paramKey])
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
  const pending = usePendingQueue(bridge, sessionKey)

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

  // The outgoing session keeps its pending queue (usePendingQueue is keyed
  // per session), so a new chat starts with an empty one without clearing it.
  // At the desk a new chat is a fresh desk session: the desk mints its key and
  // stays put. The keyless home is Chat mode, so going there would leave the
  // desk. `/new`, the shortcut and the header button all land here.
  const startNewChat = useCallback(() => {
    if (desk) {
      desk.onStartFresh()
      return
    }
    pendingIntentRef.current = 'new_chat'
    void navigate('/sessions')
    useUi.getState().requestComposerFocus()
  }, [desk, navigate])

  const onSessionAction = useCallback(
    (action: string) => {
      if (action === 'new_chat') startNewChat()
      // chat.js:2738-2763 — `/compact` runs through the compaction controller,
      // which owns the in-flight state and the separator around the RPC.
      if (action === 'compact_context') compactContext()
    },
    [startNewChat, compactContext],
  )

  const resetSession = useCallback(() => {
    void requestSessionReset(rpc, sessionKey)
  }, [rpc, sessionKey])

  // `/model` writes its list into the transcript, which the keyless home keeps
  // hidden until the first send; there the list rides on a toast instead. A
  // route hold set by a command has the composer's route chip re-read it.
  const { commands, execute: executeSlash } = useSlashCommands({
    sessionKey,
    onSessionAction,
    addSystemMessage: docked ? addSystemMessage : undefined,
    onRouteHoldChange: route.reload,
  })

  const onComposerSend = useCallback(
    async (rawText: string) => {
      if (acpClosedReason) return
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
      acpClosedReason,
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

  // Sending anything retires the suggestion, even a send queued behind a turn.
  const clearSuggestion = nextSuggestion.clear
  const onSendClearingSuggestion = useCallback(
    (text: string) => {
      clearSuggestion()
      void onComposerSend(text)
    },
    [clearSuggestion, onComposerSend],
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

  // Quick Ask (the global hotkey's panel) sends here, not just prefills. Its
  // text is the composer's input without the composer: the user's draft and
  // its attachments stay put, a slash command runs, a long paste becomes an
  // attachment, and while a reply streams it queues behind it.
  const sendQuickAsk = useCallback(
    async (rawText: string) => {
      if (acpClosedReason) {
        toast.warning(acpClosedReason)
        return
      }
      let text = rawText
      let isLiteralSlash = false
      if (text.startsWith('//')) {
        isLiteralSlash = true
        text = text.slice(1)
      }
      const isSlashCommand = !isLiteralSlash && text.startsWith('/')
      if (busy || isCompactInFlightForCurrentSession()) {
        const draft = composerHandleRef.current?.getValue() ?? ''
        const atts = attachments.attachments
        const intent = pendingIntentRef.current
        if (pending.enqueue({ text, attachments: [], intent: null })) {
          composerHandleRef.current?.setValue(draft)
          setComposerValue(draft)
          attachments.setAll(atts)
          pendingIntentRef.current = intent
        }
        return
      }
      if (isSlashCommand && (await executeSlash(text))) return
      const normalized = await normalizeOutgoingComposerPayload(text, [], {
        allowSlashCommand: isSlashCommand,
        onToast: (message, level) => {
          if (level === 'warn') toast.warning(message)
          else toast.info(message)
        },
      })
      if (!normalized) return
      send(normalized.text, normalized.attachments, null)
      if (!docked) {
        setStartedKey(sessionKey)
        void navigate(sessionPath(sessionKey), { replace: true })
      }
      useUi.getState().requestComposerFocus()
    },
    [
      acpClosedReason,
      attachments,
      busy,
      docked,
      executeSlash,
      isCompactInFlightForCurrentSession,
      navigate,
      pending,
      send,
      sessionKey,
    ],
  )
  useQuickAskSend({
    paramKey,
    threadRef: containerRef,
    send: (text) => void sendQuickAsk(text),
  })

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
    ownOrderIds,
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
      // An ended harness session takes nothing, a dropped file included.
      if (acpClosedReason) return
      if (e.dataTransfer?.files?.length) attachments.addFiles(e.dataTransfer.files)
    },
    [acpClosedReason, attachments],
  )
  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
  }, [])
  const onPaste = useCallback(
    (e: React.ClipboardEvent) => {
      if (acpClosedReason) return
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
    [acpClosedReason, attachments],
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
        title={`${desk ? t('trading.chat.fresh') : t('chat.newChat')} (${formatCombo(NEW_CHAT_COMBO)})`}
        onClick={startNewChat}
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
        {docked && acp.confirmed ? <AcpSessionBar key={sessionKey} view={acp} /> : null}
        {/* The transcript's own box: the loading line and the desk's empty
            hint are positioned in it, so neither can spill over the desk's
            approvals region or the composer below it. */}
        <div className="chat-transcript">
          <div className="chat-thread" ref={containerRef} data-history-ready="false" />
          <div className="chat-history-loading" role="status" aria-live="polite">
            <span className="chat-history-loading__dot" aria-hidden="true" />
            <span>{desk ? t('trading.chat.opening') : tw('chat.opening')}</span>
          </div>
          {instruments.emptyHint}
        </div>

        {/* Zero-height dock at the foot of the transcript — above the desk's
            approvals region, whose cards (and their notes) it used to cover,
            and above the composer block when there is none. The pill floats
            over the tail of the thread and never takes layout space. */}
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
            {instruments.dockAbove}
            <PendingQueue
              queue={pending.queue}
              onRemove={pending.remove}
              onClearAll={pending.clearAll}
            />
            <Composer
              enterToSend={enterToSend}
              onSend={onSendClearingSuggestion}
              suggestion={nextSuggestion.suggestion}
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
              toolbar={<RunModes sessionKey={sessionKey} showVisualEffects={false} />}
              seats={instruments.seats}
              placeholder={instruments.placeholder}
              onFocusChange={instruments.onFocusChange}
              disabledReason={acpClosedReason}
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
