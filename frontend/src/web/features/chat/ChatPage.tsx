import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Navigate, useLocation } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { useKeyboardShortcut } from '@/components/KeyboardShortcuts'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { useApprovals } from '@/services/approval-monitor'
import { computeHasPendingForSession } from '@/views/chat/useApprovalPending'
import { Sheet, SheetContent, SheetTitle } from '@/web/components/ui/sheet'
import { useSessionsList } from '@/web/shell/session-data'
import { pathToSessionKey, sessionKeyToPath, defaultSessionKey } from '@/web/shell/session-url'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { PHONE_QUERY, useMediaQuery } from '@/web/shell/use-media-query'
import { ApprovalDock } from './ApprovalDock'
import { ChatHeader } from './ChatHeader'
import { Composer } from './composer/Composer'
import { isRunning } from './model/reducer'
import type { Turn } from './model/types'
import { DetailsPanel } from './panels/DetailsPanel'
import { DETAILS_DOCK_MIN, detailsVisible, usePanels } from './panels/panel-store'
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
  const target = readLastSession() || defaultSessionKey('main')
  return <Navigate replace to={sessionKeyToPath(target)} />
}

/** Width of an element, live (ResizeObserver). */
function useWidth(): [(el: HTMLElement | null) => void, number] {
  const [width, setWidth] = useState(() =>
    typeof window === 'undefined' ? 1280 : window.innerWidth,
  )
  const observer = useRef<ResizeObserver | null>(null)
  const ref = useCallback((el: HTMLElement | null) => {
    observer.current?.disconnect()
    if (!el) return
    setWidth(el.getBoundingClientRect().width || window.innerWidth)
    observer.current = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width
      if (w) setWidth(w)
    })
    observer.current.observe(el)
  }, [])
  return [ref, width]
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
  const [paneRef, paneWidth] = useWidth()
  const canDock = !phone && paneWidth >= DETAILS_DOCK_MIN
  const explicit = usePanels((s) => s.details[sessionKey])
  const setDetails = usePanels((s) => s.setDetails)
  const openPanel = usePanels((s) => s.open)
  const panelTabs = usePanels((s) => s.tabs.length)
  const closeAllPanels = usePanels((s) => s.closeAll)
  const showDetails = detailsVisible(explicit, canDock)
  const [followSignal, setFollowSignal] = useState(0)
  const [editRequest, setEditRequest] = useState<{ text: string; nonce: number } | null>(null)
  const { send, abort, retry } = session

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
    rpc
      .call('sessions.contextCompact', { key: sessionKey })
      .catch((e: unknown) => toast.error(t('webChat.compactFailed', { message: errorMessage(e) })))
  }, [rpc, sessionKey])

  const actions: ChatActions = useMemo(
    () => ({
      sessionKey,
      openPanel,
      retry: (turn: Turn) => void retry(turn),
      edit: (turn: Turn) =>
        setEditRequest({ text: turn.userMessage?.text ?? '', nonce: Date.now() }),
      answer: (text: string) => send({ text }),
      approvePlan: async () => {
        try {
          await rpc.call('plan.mode.set', { key: sessionKey, mode: 'off' })
          await send({ text: t('webChat.planApprovedMessage') })
        } catch (e) {
          toast.error(errorMessage(e))
        } finally {
          void queryClient.invalidateQueries({ queryKey: ['plan.mode.get', sessionKey] })
        }
      },
    }),
    [sessionKey, openPanel, retry, send, rpc, queryClient],
  )

  // Escape in chat: stop the running turn (the composer handles its own
  // Escape, including recovering the queue).
  useKeyboardShortcut(
    { combo: 'escape', description: t('webChat.stop'), category: 'Chat' },
    (e) => {
      if (!running) return
      e.preventDefault()
      void abort()
    },
  )

  const toggleDetails = () => setDetails(sessionKey, !showDetails)
  const details = (
    <DetailsPanel
      sessionKey={sessionKey}
      row={row}
      transcript={session.state}
      onClose={() => setDetails(sessionKey, false)}
    />
  )

  return (
    <ChatActionsContext.Provider value={actions}>
      <div className="flex h-full min-h-0">
        <div ref={paneRef} className="flex min-w-0 flex-1 flex-col">
          <ChatHeader
            sessionKey={sessionKey}
            row={row}
            running={running}
            phone={phone}
            detailsOpen={showDetails}
            onToggleDetails={toggleDetails}
            onTogglePanels={() => (panelTabs ? closeAllPanels() : undefined)}
            onCompact={compact}
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
                blockedReason={blocked.reason}
                onSend={send}
                onAbort={() => void abort()}
                onCompact={compact}
                editRequest={editRequest}
                onSent={() => setFollowSignal((n) => n + 1)}
              />
            </div>
            {showDetails && canDock ? (
              <div className="w-80 shrink-0 border-l border-border">{details}</div>
            ) : null}
          </div>
        </div>
        <SidePanel transcript={session.state} phone={phone} />
      </div>
      {showDetails && !canDock ? (
        <Sheet open onOpenChange={(open) => !open && setDetails(sessionKey, false)}>
          <SheetContent side={phone ? 'bottom' : 'right'} aria-describedby={undefined}>
            <SheetTitle className="sr-only">{t('webChat.detailsTitle')}</SheetTitle>
            {details}
          </SheetContent>
        </Sheet>
      ) : null}
    </ChatActionsContext.Provider>
  )
}
