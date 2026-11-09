import { ArrowDown, Loader2 } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { Button } from '@/web/components/ui/button'
import { EmptyState, ErrorState } from '@/web/components/ui/states'
import type { TranscriptState } from '../model/types'
import { useFollowScroll } from './follow'
import { TurnView } from './TurnView'

function contentVersion(state: TranscriptState): string {
  const last = state.turns.at(-1)
  const lastItem = last?.items.at(-1)
  const size = lastItem?.kind === 'text' ? lastItem.text.length : (last?.items.length ?? 0)
  return `${state.turns.length}:${last?.status}:${last?.items.length}:${size}`
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
  const { resume } = follow

  useEffect(() => {
    if (followSignal > 0) resume()
  }, [followSignal, resume])

  const onScroll = () => {
    const el = scroller.current
    if (!el || loadingOlder.current || !state.hasMore || el.scrollTop > 40) return
    loadingOlder.current = true
    const restore = follow.preserveOnPrepend()
    void props
      .onLoadOlder()
      .then(() => requestAnimationFrame(restore))
      .finally(() => {
        loadingOlder.current = false
      })
  }

  const lastUserIndex = state.turns.reduce((acc, turn, i) => (turn.userMessage ? i : acc), -1)

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={(el) => {
          scroller.current = el
          follow.ref(el)
        }}
        onScroll={onScroll}
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
              />
            ))
          )}
        </div>
      </div>
      {!follow.following && follow.hasNew ? (
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
