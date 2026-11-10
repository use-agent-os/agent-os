import { ArrowDown, Loader2 } from 'lucide-react'
import { useCallback, useEffect, useRef } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { Button } from '@/web/components/ui/button'
import { EmptyState, ErrorState } from '@/web/components/ui/states'
import type { TranscriptState } from '../model/types'
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

  const lastUserIndex = state.turns.reduce((acc, turn, i) => (turn.userMessage ? i : acc), -1)

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={attachScroller}
        onScroll={maybeLoadOlder}
        // The log scrolls on its own: keyboard users scroll it once it has focus.
        tabIndex={0}
        role="log"
        aria-label={t('webChat.transcriptLabel')}
        aria-busy={loading || undefined}
        className="scroll-contained h-full"
      >
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-6">
          {state.hasMore ? (
            <p className="text-center text-small text-text-muted" role="status">
              <Loader2 className="motion-status mr-1 inline size-3.5 animate-spin" aria-hidden />
              {t('webChat.loadOlder')}
            </p>
          ) : null}
          {error && state.turns.length === 0 ? (
            <ErrorState
              title={t('webChat.historyFailed')}
              message={error}
              onRetry={props.onRetryLoad}
            />
          ) : !loading && state.turns.length === 0 ? (
            <EmptyState title={t('webChat.emptyTitle')} description={t('webChat.emptyBody')} />
          ) : (
            state.turns.map((turn, i) => (
              <TurnView
                key={turn.id}
                turn={turn}
                answered={i < lastUserIndex}
                waitingApproval={waitingApproval}
                last={i === state.turns.length - 1}
              />
            ))
          )}
          {state.pendingInputs.length ? (
            <section
              aria-label={t('webChat.gatewayQueued')}
              className="flex flex-col items-end gap-1"
            >
              {state.pendingInputs.map((input, i) => (
                <p
                  key={input.task_id ?? i}
                  className="max-w-[85%] rounded-2xl border border-dashed border-border px-4 py-2 text-text-muted"
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
      {follow.showLatest ? (
        <Button
          variant="secondary"
          size="sm"
          onClick={follow.resume}
          className="absolute bottom-3 left-1/2 -translate-x-1/2 shadow-2"
        >
          <ArrowDown />
          {t('webChat.latest')}
        </Button>
      ) : null}
    </div>
  )
}
