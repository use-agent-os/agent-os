import {
  ChevronRight,
  Copy,
  PencilLine,
  RotateCcw,
  ShieldAlert,
  Square,
  TriangleAlert,
} from 'lucide-react'
import { memo, useState } from 'react'
import { toast } from 'sonner'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webChat'
import { cn } from '@/lib/utils'
import { Button } from '@/web/components/ui/button'
import { CardItem } from '../cards/CoreCards'
import { Markdown } from '../markdown'
import type { Item, Turn } from '../model/types'
import { useChatActions } from './context'
import { foldTurn, formatWorkDuration } from './fold'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolRow } from './ToolRow'

function sameDay(a: Date, b: Date): boolean {
  return a.toDateString() === b.toDateString()
}

/** Quiet: the time for today's messages, the date and time otherwise. */
function timeLabel(ms: number | null): string {
  if (ms === null) return ''
  try {
    const at = new Date(ms)
    return at.toLocaleString(
      undefined,
      sameDay(at, new Date())
        ? { timeStyle: 'short' }
        : { dateStyle: 'medium', timeStyle: 'short' },
    )
  } catch {
    return ''
  }
}

async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    toast.success(t('webChat.copied'))
  } catch {
    toast.error(t('webChat.copyFailed'))
  }
}

/**
 * True when the component mounted while its turn was live: only content that
 * genuinely arrives animates in. A turn that settles and remounts under its
 * history id, or history loading above, mounts with `live` false.
 */
function useArrived(live: boolean): boolean {
  const [arrived] = useState(live)
  return arrived
}

function MessageActions({
  text,
  at,
  align = 'start',
}: {
  text: string
  at: number | null
  align?: 'start' | 'end'
}) {
  return (
    <div
      className={cn(
        'chat-actions flex items-center gap-1 text-small text-text-muted',
        align === 'end' && 'flex-row-reverse',
      )}
    >
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={t('webChat.copyMessage')}
        onClick={() => void copy(text)}
        className="text-text-muted hover:text-text"
      >
        <Copy />
      </Button>
      {at !== null ? (
        <time
          dateTime={new Date(at).toISOString()}
          title={new Date(at).toLocaleString()}
          className="px-1 tabular-nums"
        >
          {timeLabel(at)}
        </time>
      ) : null}
    </div>
  )
}

export function UserBubble({ turn, live = false }: { turn: Turn; live?: boolean }) {
  const message = turn.userMessage!
  const arrived = useArrived(live || message.delivery === 'sending')
  return (
    <div
      data-role="user"
      className={cn('group flex flex-col items-end gap-1', arrived && 'chat-enter-user')}
    >
      <div className="chat-user-bubble px-4 py-2.5">
        <Markdown text={message.text} />
      </div>
      {message.delivery === 'not_sent' ? null : (
        <MessageActions text={message.text} at={message.timestamp} align="end" />
      )}
    </div>
  )
}

function ItemView({
  item,
  live,
  answered,
  streaming = false,
}: {
  item: Item
  live: boolean
  answered: boolean
  /** The text the reply is streaming into: it carries the caret. */
  streaming?: boolean
}) {
  switch (item.kind) {
    case 'text':
      return (
        <div
          data-role="assistant"
          className={cn('group flex flex-col gap-1', streaming && 'chat-streaming')}
        >
          <Markdown text={item.text} />
          {live ? null : <MessageActions text={item.text} at={null} />}
        </div>
      )
    case 'toolCall':
      return <ToolRow item={item} live={live} />
    case 'thinkingRef':
      return <ThinkingBlock item={item} live={live} />
    default:
      return <CardItem item={item} answered={answered} />
  }
}

type Group = { key: string; activity: boolean; items: Item[] }

/** Consecutive reasoning and tool calls read as one activity thread. */
function groupItems(items: Item[]): Group[] {
  const groups: Group[] = []
  for (const item of items) {
    const activity = item.kind === 'toolCall' || item.kind === 'thinkingRef'
    const last = groups.at(-1)
    if (activity && last?.activity) last.items.push(item)
    else groups.push({ key: item.id, activity, items: [item] })
  }
  return groups
}

/** An item that arrived live rises into place; history renders still. */
function Arrival({
  live,
  className,
  children,
}: {
  live: boolean
  className?: string
  children: React.ReactNode
}) {
  const arrived = useArrived(live)
  return <div className={cn('chat-item', className, arrived && 'chat-enter')}>{children}</div>
}

function Items({
  items,
  live,
  answered,
  streamingId = null,
}: {
  items: Item[]
  live: boolean
  answered: boolean
  streamingId?: string | null
}) {
  return groupItems(items).map((group) =>
    group.activity ? (
      <div key={group.key} className="chat-activity">
        {group.items.map((item) => (
          <Arrival key={item.id} live={live} className="chat-step">
            <ItemView item={item} live={live} answered={answered} />
          </Arrival>
        ))}
      </div>
    ) : (
      <Arrival key={group.key} live={live}>
        <ItemView
          item={group.items[0]!}
          live={live}
          answered={answered}
          streaming={group.items[0]!.id === streamingId}
        />
      </Arrival>
    ),
  )
}

function WorkedFor({
  work,
  toolCount,
  durationMs,
}: {
  work: Item[]
  toolCount: number
  durationMs: number | null
}) {
  const [open, setOpen] = useState(false)
  // Mounted on first open, then kept so closing animates too.
  const [mounted, setMounted] = useState(false)
  const duration = formatWorkDuration(durationMs)
  const label =
    toolCount > 0
      ? tPlural('webChat.workedForTools', toolCount, { duration })
      : t('webChat.workedFor', { duration })
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setMounted(true)
          setOpen((v) => !v)
        }}
        className="target -ml-1.5 flex items-center gap-1.5 rounded-control px-1.5 py-1 text-ui text-text-muted transition-colors duration-[var(--duration-fast)] hover:bg-surface-2 hover:text-text"
      >
        {label}
        <ChevronRight className="chat-chevron" aria-hidden />
      </button>
      {mounted ? (
        <div className="collapsible chat-collapse" data-open={open}>
          <div>
            <div className="chat-fold-body">
              <Items items={work} live={false} answered={false} />
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}

function FailedTurn({ turn }: { turn: Turn }) {
  const { retry, edit } = useChatActions()
  const notSent = turn.userMessage?.delivery === 'not_sent'
  const reason = turn.failure
  return (
    <div role="alert" className="chat-failed flex flex-col gap-3 px-3.5 py-3">
      <p className="flex items-start gap-2.5">
        <span className="chat-tile border-danger/30 bg-danger/10 text-danger">
          <TriangleAlert aria-hidden data-status-icon />
        </span>
        <span className="min-w-0 pt-1">
          {notSent
            ? t('webChat.notSent')
            : reason
              ? t('webChat.turnFailed', { reason })
              : t('webChat.turnFailedUnknown')}
        </span>
      </p>
      {turn.userMessage ? (
        <div className="flex flex-wrap gap-2 pl-10">
          <Button size="sm" variant="secondary" onClick={() => retry(turn)}>
            <RotateCcw />
            {notSent ? t('webChat.retrySend') : t('webChat.tryAgain')}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => edit(turn)}>
            <PencilLine />
            {t('webChat.editMessage')}
          </Button>
        </div>
      ) : null}
    </div>
  )
}

/** Nothing to show yet but the run is on: say so (send → first output, or a stop pending). */
function LiveStatus({ turn, visible }: { turn: Turn; visible: Item[] }) {
  const stopping = Boolean(turn.abortRequested)
  const quiet =
    !visible.some((i) => i.kind === 'text' && i.text.trim()) &&
    !visible.some((i) => i.kind === 'toolCall' && i.state === 'running') &&
    !visible.some((i) => i.kind === 'thinkingRef')
  if (!stopping && !quiet) return null
  return (
    <p role="status" className="chat-enter flex items-center gap-2.5 text-ui">
      <span className="flex size-6 items-center justify-center" aria-hidden>
        <span className="status-dot motion-status size-2 text-accent" data-live data-status-icon />
      </span>
      <span className="chat-shimmer">
        {stopping ? t('webChat.stopping') : t('webChat.working')}
      </span>
    </p>
  )
}

/** One turn: the user bubble, then the assistant's work and answer. */
export const TurnView = memo(function TurnView({
  turn,
  answered,
  waitingApproval,
  last = false,
}: {
  turn: Turn
  /** A later user message exists (ask/plan cards lock). */
  answered: boolean
  waitingApproval: boolean
  /**
   * The newest turn. It is on screen when it settles, and settling remounts
   * it under its history id: with `content-visibility` it would lay out at
   * the placeholder height first and clamp the scroll position.
   */
  last?: boolean
}) {
  const fold = foldTurn(turn)
  const live = turn.status === 'live'
  const tail = fold.visible.at(-1)
  const streamingId = live && tail?.kind === 'text' ? tail.id : null
  return (
    <article
      data-turn-status={turn.status}
      className={cn(
        'flex flex-col gap-4',
        !live && !last && '[content-visibility:auto] [contain-intrinsic-size:auto_200px]',
      )}
    >
      {turn.userMessage ? <UserBubble turn={turn} live={live} /> : null}
      {turn.textTruncated ? (
        <p className="text-small text-text-muted">{t('webChat.textTruncated')}</p>
      ) : null}
      {fold.folded ? (
        <WorkedFor work={fold.work} toolCount={fold.toolCount} durationMs={fold.durationMs} />
      ) : null}
      <Items
        items={fold.visible}
        live={live}
        answered={answered}
        streamingId={waitingApproval ? null : streamingId}
      />
      {live && (!waitingApproval || turn.abortRequested) ? (
        <LiveStatus turn={turn} visible={fold.visible} />
      ) : null}
      {live && waitingApproval ? (
        <p
          role="status"
          className="chat-enter inline-flex items-center gap-2 self-start rounded-full border border-warn/30 bg-warn/10 py-1 pr-3 pl-2 text-small font-medium text-warn"
        >
          <ShieldAlert className="size-4" aria-hidden data-status-icon />
          {t('webChat.turnWaitingApproval')}
        </p>
      ) : null}
      {turn.status === 'aborted' ? (
        <p className="flex items-center gap-2 text-small text-text-muted">
          <Square className="size-3 fill-current" aria-hidden data-status-icon />
          {t('webChat.turnAborted')}
        </p>
      ) : null}
      {turn.status === 'failed' ? <FailedTurn turn={turn} /> : null}
    </article>
  )
})
