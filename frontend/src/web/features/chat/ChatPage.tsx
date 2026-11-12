import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { useKeyboardShortcut } from '@/components/KeyboardShortcuts'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { useApprovals } from '@/services/approval-monitor'
import { readPromptFromUrl } from '@/views/chat/logic'
import { computeHasPendingForSession } from '@/views/chat/useApprovalPending'
import { SESSIONS_QUERY_KEY, useSessionsList } from '@/web/shell/session-data'
import { pathToSessionKey, sessionKeyToPath, defaultSessionKey } from '@/web/shell/session-url'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { PHONE_QUERY, useMediaQuery } from '@/web/shell/use-media-query'
import '@/web/styles/chat.css'
import '@/web/styles/chat-stage.css'
import { ApprovalDock } from './ApprovalDock'
import { ChatHeader } from './ChatHeader'
import { Composer } from './composer/Composer'
import { enqueueMessage } from './composer/queue'
import { isRunning } from './model/reducer'
import type { Turn } from './model/types'
import { usePanels } from './panels/panel-store'
import { SidePanel } from './panels/SidePanel'
import { ChatActionsContext, type ChatActions } from './transcript/context'
import { TranscriptView } from './transcript/TranscriptView'
import { useChatSession } from './useChatSession'

export const LAST_SESSION_KEY = 'agentos.ui.lastSession'

function readLastSession(): string | null {
  try {
    return localStorage.getItem(LAST_SESSION_KEY)
  } catch {
    return null
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** `/chat`: the last active session, or the default webchat session. */
export function ChatIndex() {
  // `?prompt=` (the Skills page's Use) rides along to prefill the composer.
  const { search } = useLocation()
  const target = readLastSession() || defaultSessionKey('main')
  return <Navigate replace to={`${sessionKeyToPath(target)}${search}`} />
}

/** The turn whose ending decides what the queue does (local notes skip). */
function lastTurnStatus(turns: readonly Turn[]): Turn['status'] | undefined {
  for (let i = turns.length - 1; i >= 0; i--) {
    const turn = turns[i]!
    if (turn.userMessage || turn.items.some((item) => item.kind !== 'note')) return turn.status
  }
  return undefined
}

/** `/chat/<agent>/<rest>`: one session's workspace. */
export function ChatPage() {
  const { pathname } = useLocation()
  const sessionKey = pathToSessionKey(pathname)
  if (!sessionKey) return <Navigate replace to="/chat" />
  return <ChatWorkspace key={sessionKey} sessionKey={sessionKey} />
}

function ChatWorkspace({ sessionKey }: { sessionKey: string }) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const session = useChatSession(sessionKey)
  const sessions = useSessionsList()
  const row = sessions.rows.find((r) => r.key === sessionKey)
  const connection = useDisplayedConnection()
  const phone = useMediaQuery(PHONE_QUERY)
  const pending = useApprovals((s) => s.pending)
  const waitingApproval = computeHasPendingForSession(pending, sessionKey)
  const running = isRunning(session.state)
  const openPanel = usePanels((s) => s.open)
  const panelTabs = usePanels((s) => s.tabs.length)
  const closeAllPanels = usePanels((s) => s.closeAll)
  const [followSignal, setFollowSignal] = useState(0)
  const navigate = useNavigate()
  const { search } = useLocation()
  // `?prompt=` prefills the composer once, then leaves the address.
  const [editRequest, setEditRequest] = useState<{ text: string; nonce: number } | null>(() => {
    const prompt = readPromptFromUrl(search)
    return prompt ? { text: prompt, nonce: 0 } : null
  })
  useEffect(() => {
    if (readPromptFromUrl(search) !== null)
      navigate(sessionKeyToPath(sessionKey), { replace: true })
  }, [search, navigate, sessionKey])
  const [compacting, setCompacting] = useState(false)
  const { send, abort, retry, reload, setQueued, addNote } = session
  const runningRef = useRef(running)
  useEffect(() => {
    runningRef.current = running
  }, [running])

  useEffect(() => {
    try {
      localStorage.setItem(LAST_SESSION_KEY, sessionKey)
    } catch {
      /* storage unavailable */
    }
  }, [sessionKey])

  const blocked = gate(requireConnection(connection))

  const compact = useCallback(() => {
    toast.info(t('webChat.compactStarted'))
    setCompacting(true)
    rpc
      .call('sessions.contextCompact', { key: sessionKey })
      .catch((e: unknown) => toast.error(t('webChat.compactFailed', { message: errorMessage(e) })))
      .finally(() => setCompacting(false))
  }, [rpc, sessionKey])

  const stop = useCallback(() => {
    abort().catch((e: unknown) =>
      toast.error(t('webChat.stopFailed', { message: errorMessage(e) })),
    )
  }, [abort])

  const reset = useCallback(async () => {
    const { resetSession } = await import('@/views/chat/resetSession')
    await resetSession(rpc, sessionKey)
    void reload()
    void queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY })
  }, [rpc, sessionKey, reload, queryClient])

  // A message from a card (an answer, a plan approval, Try again) sent while
  // a turn still runs would split that turn's answer: it waits in the queue.
  const sendOrQueue = useCallback(
    async (text: string): Promise<boolean> => {
      if (runningRef.current) {
        return enqueueMessage(sessionKey, { text, attachments: [], intent: null })
      }
      return send({ text })
    },
    [send, sessionKey],
  )

  const actions: ChatActions = useMemo(
    () => ({
      sessionKey,
      openPanel,
      retry: (turn: Turn) => {
        const message = turn.userMessage
        if (!runningRef.current || !message) return void retry(turn)
        if (message.attachments.length) toast.info(t('webChat.waitForTurn'))
        else enqueueMessage(sessionKey, { text: message.text, attachments: [], intent: null })
      },
      edit: (turn: Turn) =>
        setEditRequest({ text: turn.userMessage?.text ?? '', nonce: Date.now() }),
      answer: (text: string) => sendOrQueue(text),
      approvePlan: async () => {
        try {
          await rpc.call('plan.mode.set', { key: sessionKey, mode: 'off' })
          await sendOrQueue(t('webChat.planApprovedMessage'))
        } catch (e) {
          toast.error(errorMessage(e))
        } finally {
          void queryClient.invalidateQueries({ queryKey: ['plan.mode.get', sessionKey] })
        }
      },
    }),
    [sessionKey, openPanel, retry, sendOrQueue, rpc, queryClient],
  )

  // Escape in chat: stop the running turn (the composer handles its own
  // Escape, including recovering the queue).
  const stopping = Boolean(session.state.liveTurns.at(-1)?.abortRequested)
  const forcing = Boolean(session.state.liveTurns.at(-1)?.forceStop)
  useKeyboardShortcut(
    { combo: 'escape', description: t('webChat.stop'), category: 'Chat' },
    (e) => {
      if (!running || e.isComposing) return
      e.preventDefault()
      // Pressed again while stopping: stop at once.
      if (!forcing) stop()
    },
  )
  const onSent = useCallback(() => setFollowSignal((n) => n + 1), [])

  return (
    <ChatActionsContext.Provider value={actions}>
      <div className="flex h-full min-h-0">
        <div className="chat-pane bg-atmosphere flex min-w-0 flex-1 flex-col">
          <ChatHeader
            sessionKey={sessionKey}
            row={row}
            running={running}
            phone={phone}
            onTogglePanels={() => (panelTabs ? closeAllPanels() : undefined)}
            onCompact={compact}
            onReset={() => void reset()}
            firstMessage={session.state.turns.find((turn) => turn.userMessage)?.userMessage?.text}
          />
          <div className="flex min-h-0 flex-1">
            <div className="flex min-w-0 flex-1 flex-col">
              <TranscriptView
                state={session.state}
                loading={session.loading}
                error={session.error}
                waitingApproval={waitingApproval}
                onRetryLoad={() => void session.reload()}
                onLoadOlder={session.loadOlder}
                followSignal={followSignal}
              />
              <ApprovalDock sessionKey={sessionKey} />
              <Composer
                sessionKey={sessionKey}
                running={running}
                ready={!session.loading}
                stopping={stopping}
                forcing={forcing}
                compacting={compacting}
                lastStatus={lastTurnStatus(session.state.turns)}
                blockedReason={blocked.reason}
                onSend={send}
                onAbort={stop}
                onCompact={compact}
                onQueueChange={setQueued}
                onNote={addNote}
                editRequest={editRequest}
                onSent={onSent}
              />
            </div>
          </div>
        </div>
        <SidePanel transcript={session.state} phone={phone} />
      </div>
    </ChatActionsContext.Provider>
  )
}
