import {
  ChevronRight,
  Copy,
  Loader2,
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

function timeLabel(ms: number | null): string {
  if (ms === null) return ''
  try {
    return new Date(ms).toLocaleString()
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

function MessageActions({ text, at }: { text: string; at: number | null }) {
  return (
    <div className="flex items-center gap-1 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100 touch:opacity-100">
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={t('webChat.copyMessage')}
        onClick={() => void copy(text)}
      >
        <Copy />
      </Button>
      {at !== null ? (
        <time dateTime={new Date(at).toISOString()} className="text-small text-text-muted">
          {timeLabel(at)}
        </time>
      ) : null}
    </div>
  )
}

export function UserBubble({ turn }: { turn: Turn }) {
  const message = turn.userMessage!
  return (
    <div data-role="user" className="group flex flex-col items-end gap-1">
      <div className="max-w-[85%] rounded-2xl rounded-br-md bg-surface-3 px-4 py-2">
        <Markdown text={message.text} />
      </div>
      {message.delivery === 'not_sent' ? null : (
        <MessageActions text={message.text} at={message.timestamp} />
      )}
    </div>
  )
}

function ItemView({ item, live, answered }: { item: Item; live: boolean; answered: boolean }) {
  switch (item.kind) {
    case 'text':
      return (
        <div data-role="assistant" className="group flex flex-col gap-1">
          <Markdown text={item.text} />
          {live ? null : <MessageActions text={item.text} at={null} />}
        </div>
      )
    case 'toolCall':
      return <ToolRow item={item} />
    case 'thinkingRef':
      return <ThinkingBlock item={item} live={live} />
    default:
      return <CardItem item={item} answered={answered} />
  }
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
        onClick={() => setOpen((v) => !v)}
        className="target flex items-center gap-1.5 rounded px-1 text-ui text-text-muted hover:text-text"
      >
        <ChevronRight
          className={cn('size-3.5 transition-transform', open && 'rotate-90')}
          aria-hidden
        />
        {label}
      </button>
      {open ? (
        <div className="mt-2 flex flex-col gap-2 border-l-2 border-border pl-3">
          {work.map((item) => (
            <ItemView key={item.id} item={item} live={false} answered={false} />
          ))}
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
    <div
      role="alert"
      className="flex flex-col gap-2 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2"
    >
      <p className="flex items-center gap-1.5">
        <TriangleAlert className="size-4 text-danger" aria-hidden data-status-icon />
        {notSent
          ? t('webChat.notSent')
          : reason
            ? t('webChat.turnFailed', { reason })
            : t('webChat.turnFailedUnknown')}
      </p>
      {turn.userMessage ? (
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => retry(turn)}>
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
    <p role="status" className="flex items-center gap-1.5 text-small text-text-muted">
      <Loader2 className="motion-status size-3.5 animate-spin" aria-hidden data-status-icon />
      {stopping ? t('webChat.stopping') : t('webChat.working')}
    </p>
  )
}

/** One turn: the user bubble, then the assistant's work and answer. */
export const TurnView = memo(function TurnView({
  turn,
  answered,
  waitingApproval,
}: {
  turn: Turn
  /** A later user message exists (ask/plan cards lock). */
  answered: boolean
  waitingApproval: boolean
}) {
  const fold = foldTurn(turn)
  const live = turn.status === 'live'
  return (
    <article
      data-turn-status={turn.status}
      className={cn(
        'flex flex-col gap-3',
        !live && '[content-visibility:auto] [contain-intrinsic-size:auto_200px]',
      )}
    >
      {turn.userMessage ? <UserBubble turn={turn} /> : null}
      {turn.textTruncated ? (
        <p className="text-small text-text-muted">{t('webChat.textTruncated')}</p>
      ) : null}
      {fold.folded ? (
        <WorkedFor work={fold.work} toolCount={fold.toolCount} durationMs={fold.durationMs} />
      ) : null}
      {fold.visible.map((item) => (
        <ItemView key={item.id} item={item} live={live} answered={answered} />
      ))}
      {live && (!waitingApproval || turn.abortRequested) ? (
        <LiveStatus turn={turn} visible={fold.visible} />
      ) : null}
      {live && waitingApproval ? (
        <p role="status" className="flex items-center gap-1.5 text-small text-warn">
          <ShieldAlert className="size-4" aria-hidden data-status-icon />
          {t('webChat.turnWaitingApproval')}
        </p>
      ) : null}
      {turn.status === 'aborted' ? (
        <p className="flex items-center gap-1.5 text-small text-text-muted">
          <Square className="size-3.5" aria-hidden data-status-icon />
          {t('webChat.turnAborted')}
        </p>
      ) : null}
      {turn.status === 'failed' ? <FailedTurn turn={turn} /> : null}
    </article>
  )
})
