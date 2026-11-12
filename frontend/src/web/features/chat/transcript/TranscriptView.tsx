import { ArrowDown, Loader2, MessageSquareText } from 'lucide-react'
import { useCallback, useContext, useEffect, useRef, useState } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { Button } from '@/web/components/ui/button'
import { ErrorState } from '@/web/components/ui/states'
import { useAgents } from '@/web/shell/session-data'
import { agentOfSessionKey } from '@/web/shell/session-url'
import type { TranscriptState, Turn } from '../model/types'
import { AgentNameContext, ChatActionsContext } from './context'
import { useFollowScroll } from './follow'
import { TurnView } from './TurnView'

/**
 * Changes whenever the newest turn grows anywhere: text streams in before
 * trailing artifacts and cards, so the last item alone is not enough.
 */
export function contentVersion(state: TranscriptState): string {
  const last = state.turns.at(-1)
  let text = 0
  let settled = 0
  for (const item of last?.items ?? []) {
    if (item.kind === 'text') text += item.text.length
    else if (item.kind === 'thinkingRef') text += item.text?.length ?? 0
    else if (item.kind === 'toolCall' && item.state !== 'running') settled++
  }
  return [
    state.turns.length,
    last?.status,
    last?.items.length,
    text,
    settled,
    state.pendingInputs.length,
  ].join(':')
}

/** A local note (a slash command's output), not a turn of the conversation. */
function isNoteTurn(turn: Turn): boolean {
  return !turn.userMessage && turn.items.every((item) => item.kind === 'note')
}

export interface TranscriptViewProps {
  state: TranscriptState
  loading: boolean
  error: string | null
  waitingApproval: boolean
  onRetryLoad(): void
  onLoadOlder(): Promise<void>
  /** Bumped by the composer on send: follow again. */
  followSignal: number
}

/** The scrolling transcript column with follow-to-latest and paging. */
export function TranscriptView(props: TranscriptViewProps) {
  const { state, loading, error, waitingApproval, followSignal } = props
  const follow = useFollowScroll(contentVersion(state))
  const scroller = useRef<HTMLDivElement | null>(null)
  const loadingOlder = useRef(false)
  const { resume, ref: followRef } = follow
  // Stable, so the scroller is attached once, not detached on every render.
  const attachScroller = useCallback(
    (el: HTMLDivElement | null) => {
      scroller.current = el
      followRef(el)
    },
    [followRef],
  )

  useEffect(() => {
    if (followSignal > 0) resume()
  }, [followSignal, resume])

  const { onLoadOlder } = props
  const { preserveOnPrepend } = follow
  const maybeLoadOlder = useCallback(() => {
    const el = scroller.current
    if (!el || loadingOlder.current || !state.hasMore || el.scrollTop > 40) return
    loadingOlder.current = true
    const restore = preserveOnPrepend()
    void onLoadOlder()
      .then(() => requestAnimationFrame(restore))
      .finally(() => {
        loadingOlder.current = false
      })
  }, [state.hasMore, onLoadOlder, preserveOnPrepend])

  // A window too short to scroll (a page cut by its byte budget) never gets
  // a scroll event: load on, until it fills the view or history runs out.
  const rows = state.rows.length
  useEffect(() => {
    if (loading || !state.hasMore) return
    const el = scroller.current
    if (el && el.scrollHeight <= el.clientHeight + 40) maybeLoadOlder()
  }, [loading, state.hasMore, rows, maybeLoadOlder])

  // The newest turn finishing on screen (live → done): its work folds away
  // instead of snapping shut. Settling swaps the live turn for its history
  // copy (a new id, and for a moment both are listed), so until the next turn
  // goes live the newest turn keeps one key: it mounts once, with the fold
  // starting open. A turn loaded from history is never marked.
  const lastTurn = state.turns.at(-1)
  const lastLive = lastTurn?.status === 'live'
  const [wasLive, setWasLive] = useState(lastLive)
  const [settleKey, setSettleKey] = useState<string | null>(null)
  if (wasLive !== lastLive) {
    setWasLive(lastLive)
    setSettleKey(!lastLive && lastTurn?.status === 'done' ? `settled:${lastTurn.id}` : null)
  }
  const settlingIndex =
    settleKey && lastTurn?.status === 'done' && !isNoteTurn(lastTurn) ? state.turns.length - 1 : -1

  const lastUserIndex = state.turns.reduce((acc, turn, i) => (turn.userMessage ? i : acc), -1)

  const empty = !error && !loading && state.turns.length === 0
  const agentName = useAgentName()

  // The bottom edge fades into the canvas, so the composer reads as floating.
  return (
    <AgentNameContext.Provider value={agentName}>
      <div className="chat-fade relative min-h-0 flex-1">
        <div
          ref={attachScroller}
          onScroll={maybeLoadOlder}
          // The log scrolls on its own: keyboard users scroll it once it has focus.
          tabIndex={0}
          role="log"
          aria-label={t('webChat.transcriptLabel')}
          aria-busy={loading || undefined}
          className="chat-scroller scroll-contained h-full focus-visible:-outline-offset-2"
        >
          <div className="chat-column chat-turns flex min-h-full flex-col">
            {state.hasMore ? (
              <p
                className="flex items-center justify-center gap-1.5 text-small text-text-muted"
                role="status"
              >
                <Loader2 className="motion-status size-3.5 animate-spin" aria-hidden />
                {t('webChat.loadOlder')}
              </p>
            ) : null}
            {error && state.turns.length === 0 ? (
              <ErrorState
                title={t('webChat.historyFailed')}
                message={error}
                onRetry={props.onRetryLoad}
              />
            ) : empty ? (
              <EmptyConversation />
            ) : (
              state.turns.map((turn, i) => (
                <TurnView
                  key={i === settlingIndex ? settleKey! : turn.id}
                  turn={turn}
                  answered={i < lastUserIndex}
                  waitingApproval={waitingApproval}
                  last={i === state.turns.length - 1}
                  settled={i === settlingIndex}
                />
              ))
            )}
            {state.pendingInputs.length ? (
              <section
                aria-label={t('webChat.gatewayQueued')}
                className="flex flex-col items-end gap-1.5"
              >
                {state.pendingInputs.map((input, i) => (
                  <p
                    key={input.task_id ?? i}
                    className="chat-enter chat-user-bubble chat-pending text-text-muted"
                  >
                    {input.steer
                      ? t('webChat.gatewayQueuedSteer', { text: input.text ?? '' })
                      : t('webChat.gatewayQueuedNext', { text: input.text ?? '' })}
                  </p>
                ))}
              </section>
            ) : null}
          </div>
        </div>
        {/* Always mounted, so it can leave as smoothly as it arrives; hidden
          (and out of the tab order) while following. */}
        <div
          className="chat-latest pointer-events-none absolute inset-x-0 bottom-3 z-[1] flex justify-center"
          data-show={follow.showLatest}
          inert={!follow.showLatest}
          aria-hidden={!follow.showLatest || undefined}
        >
          <Button
            variant="secondary"
            size="sm"
            onClick={follow.resume}
            className="pointer-events-auto rounded-full pr-3.5 pl-3 shadow-overlay"
          >
            <ArrowDown />
            {t('webChat.latest')}
          </Button>
        </div>
      </div>
    </AgentNameContext.Provider>
  )
}

/** The answering agent's display name, from the session key and the agent list. */
function useAgentName(): string {
  const sessionKey = useContext(ChatActionsContext)?.sessionKey ?? ''
  const agents = useAgents()
  const id = agentOfSessionKey(sessionKey) ?? ''
  const name = agents.data?.find((a) => a.id === id)?.name
  // Until the list answers (or without one), the id, capitalised.
  return name ?? id.charAt(0).toUpperCase() + id.slice(1)
}

/** The empty conversation: a composed arrival, centred in the column. */
function EmptyConversation() {
  return (
    <div className="anim-stagger my-auto flex flex-col items-center gap-3 py-10 text-center">
      <span className="chat-hero-mark isolate mb-3" aria-hidden>
        <MessageSquareText className="size-6" />
      </span>
      <h2 className="text-title text-balance">{t('webChat.emptyTitle')}</h2>
      <p className="max-w-sm text-ui text-pretty text-text-muted">{t('webChat.emptyBody')}</p>
    </div>
  )
}
