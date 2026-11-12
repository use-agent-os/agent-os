import {
  Brain,
  ChevronRight,
  CircleAlert,
  Copy,
  PencilLine,
  RotateCcw,
  ShieldAlert,
  Square,
} from 'lucide-react'
import { memo, useContext, useState } from 'react'
import { toast } from 'sonner'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webChat'
import { cn } from '@/lib/utils'
import { modelDisplayName } from '@/views/chat/transcript/routerFx'
import { toolIconName } from '@/views/chat/transcript/tools'
import { Button } from '@/web/components/ui/button'
import '@/web/styles/chat-thread.css'
import { AgentAvatar } from '../AgentAvatar'
import { CardItem } from '../cards/CoreCards'
import { Markdown } from '../markdown'
import type { Item, Turn } from '../model/types'
import { AgentNameContext, useChatActions } from './context'
import { foldTurn, formatWorkDuration } from './fold'
import { ThinkingBlock } from './ThinkingBlock'
import { TOOL_ICONS, ToolRow } from './ToolRow'

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

/** Copy and time in a small pill that surfaces on hover or focus. */
function MessageActions({
  text,
  at,
  side,
}: {
  text: string
  at: number | null
  side: 'user' | 'reply'
}) {
  return (
    <div className="chat-msg-actions" data-side={side}>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={t('webChat.copyMessage')}
        onClick={() => void copy(text)}
        className="size-7 rounded-full text-text-muted hover:text-text"
      >
        <Copy />
      </Button>
      {at !== null ? (
        <time
          dateTime={new Date(at).toISOString()}
          title={new Date(at).toLocaleString()}
          className="pr-2.5 pl-0.5 text-small whitespace-nowrap text-text-muted tabular-nums"
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
    <div data-role="user" className={cn('chat-user items-end', arrived && 'chat-enter-user')}>
      <div className="chat-user-bubble">
        <Markdown text={message.text} />
      </div>
      {message.delivery === 'not_sent' ? null : (
        <MessageActions text={message.text} at={message.timestamp} side="user" />
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
        <div className={cn('chat-text', streaming && 'chat-streaming')}>
          <Markdown text={item.text} />
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

/** Consecutive reasoning and tool calls read as one activity block. */
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

/** Up to three distinct icons of the folded work, for the summary line. */
function workIcons(work: Item[]): React.ComponentType[] {
  const seen = new Set<string>()
  const icons: React.ComponentType[] = []
  for (const item of work) {
    const name =
      item.kind === 'toolCall'
        ? toolIconName(item.name)
        : item.kind === 'thinkingRef'
          ? 'brain'
          : ''
    if (!name || seen.has(name)) continue
    seen.add(name)
    icons.push(name === 'brain' ? Brain : TOOL_ICONS[name as keyof typeof TOOL_ICONS])
    if (icons.length === 3) break
  }
  return icons
}

function WorkedFor({
  work,
  toolCount,
  durationMs,
  settled = false,
}: {
  work: Item[]
  toolCount: number
  durationMs: number | null
  /** The turn just finished on screen: the live work folds away, not snaps. */
  settled?: boolean
}) {
  const [open, setOpen] = useState(false)
  // Mounted on first open, then kept so closing animates too. A turn that
  // just settled mounts it closed, starting from open (chat-thread.css), so
  // the work it was showing collapses into the summary line.
  const [mounted, setMounted] = useState(settled)
  const duration = formatWorkDuration(durationMs)
  const label =
    toolCount > 0
      ? tPlural('webChat.workedForTools', toolCount, { duration })
      : t('webChat.workedFor', { duration })
  const icons = workIcons(work)
  return (
    <div className="chat-fold">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setMounted(true)
          setOpen((v) => !v)
        }}
        className="chat-fold-summary target"
      >
        {icons.length ? (
          <span className="chat-fold-icons" aria-hidden>
            {icons.map((Icon, i) => (
              <span key={i} className="chat-node">
                <Icon />
              </span>
            ))}
          </span>
        ) : null}
        {label}
        <ChevronRight className="chat-chevron" aria-hidden />
      </button>
      {mounted ? (
        <div className={cn('collapsible chat-collapse', settled && 'chat-settle')} data-open={open}>
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
    <div role="alert" className="chat-turn-error">
      <span className="chat-turn-error-icon" aria-hidden>
        <CircleAlert data-status-icon />
      </span>
      <p className="min-w-0 flex-1 self-center text-ui text-pretty">
        {notSent
          ? t('webChat.notSent')
          : reason
            ? t('webChat.turnFailed', { reason })
            : t('webChat.turnFailedUnknown')}
      </p>
      {turn.userMessage ? (
        <div className="chat-turn-error-actions">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => edit(turn)}
            className="text-text-muted hover:text-text"
          >
            <PencilLine />
            {t('webChat.editMessage')}
          </Button>
          <Button size="sm" variant="outline" onClick={() => retry(turn)} className="chat-retry">
            <RotateCcw />
            {notSent ? t('webChat.retrySend') : t('webChat.tryAgain')}
          </Button>
        </div>
      ) : null}
    </div>
  )
}

/**
 * Nothing to show yet but the run is on: a thinking indicator (send → first
 * output), or the stop that is pending.
 */
function LiveStatus({ turn, visible }: { turn: Turn; visible: Item[] }) {
  const stopping = Boolean(turn.abortRequested)
  const quiet =
    !visible.some((i) => i.kind === 'text' && i.text.trim()) &&
    !visible.some((i) => i.kind === 'toolCall' && i.state === 'running') &&
    !visible.some((i) => i.kind === 'thinkingRef')
  if (!stopping && !quiet) return null
  return (
    <p role="status" className="chat-thinking chat-enter">
      <span className="chat-dots" aria-hidden>
        <span className="motion-status" />
        <span className="motion-status" />
        <span className="motion-status" />
      </span>
      <span className="chat-shimmer">
        {stopping ? t('webChat.stopping') : t('webChat.working')}
      </span>
    </p>
  )
}

/** Item kinds that are the agent speaking (they get the identity line). */
const REPLY_KINDS = new Set<Item['kind']>([
  'text',
  'toolCall',
  'thinkingRef',
  'plan',
  'ask',
  'acp',
  'artifact',
  'card',
  'subagentCompletion',
  'cronResult',
])

/** The model that answered: the router's last pick, else the usage record. */
function turnModel(turn: Turn): { model: string; tier: string | null } | null {
  const routed = turn.items.filter((i) => i.kind === 'routerDecision').at(-1)
  if (routed?.kind === 'routerDecision' && routed.model) {
    return { model: modelDisplayName(routed.model), tier: routed.tier }
  }
  return turn.usage?.model ? { model: modelDisplayName(turn.usage.model), tier: null } : null
}

/** Avatar, agent name and (when known) the model, above the reply. */
function ReplyIdentity({ turn, live }: { turn: Turn; live: boolean }) {
  const agent = useContext(AgentNameContext)
  const arrived = useArrived(live)
  const model = turnModel(turn)
  return (
    <div className={cn('chat-ident', arrived && 'chat-enter')}>
      <AgentAvatar size={24} />
      <span className="truncate font-semibold">{agent || t('webChat.agentFallback')}</span>
      {model ? (
        <span
          className="chat-ident-model"
          title={t('webChat.routerDecision', { model: model.model })}
        >
          {model.model}
          {model.tier ? ` · ${model.tier}` : null}
        </span>
      ) : null}
    </div>
  )
}

/** One turn: the user bubble, then the assistant's work and answer. */
export const TurnView = memo(function TurnView({
  turn,
  answered,
  waitingApproval,
  last = false,
  settled = false,
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
  /** It finished while on screen (live → done), not loaded from history. */
  settled?: boolean
}) {
  const fold = foldTurn(turn)
  const live = turn.status === 'live'
  // The router's pick shows on the identity line, not as a row of its own.
  const visible = fold.visible.filter((i) => i.kind !== 'routerDecision')
  const tail = visible.at(-1)
  const streamingId = live && tail?.kind === 'text' ? tail.id : null
  // The agent's side of the turn gets its identity line; a message that was
  // never delivered, or a turn of local notes, does not.
  const reply =
    turn.userMessage?.delivery !== 'not_sent' &&
    (live ||
      turn.status === 'failed' ||
      turn.status === 'aborted' ||
      turn.items.some((i) => REPLY_KINDS.has(i.kind)))
  const answer = visible
    .filter((i): i is Extract<Item, { kind: 'text' }> => i.kind === 'text')
    .map((i) => i.text)
    .join('\n\n')
    .trim()

  const body = (
    <>
      {fold.folded ? (
        <WorkedFor
          work={fold.work}
          toolCount={fold.toolCount}
          durationMs={fold.durationMs}
          settled={settled}
        />
      ) : null}
      <Items
        items={visible}
        live={live}
        answered={answered}
        streamingId={waitingApproval ? null : streamingId}
      />
    </>
  )

  return (
    <article
      data-turn-status={turn.status}
      className={cn(
        'chat-turn',
        !live && !last && '[content-visibility:auto] [contain-intrinsic-size:auto_200px]',
      )}
    >
      {turn.userMessage ? <UserBubble turn={turn} live={live} /> : null}
      {turn.textTruncated ? (
        <p className="text-small text-text-muted">{t('webChat.textTruncated')}</p>
      ) : null}
      {reply || turn.status === 'failed' ? (
        <div data-role="assistant" data-last={last || undefined} className="chat-reply">
          {reply ? <ReplyIdentity turn={turn} live={live} /> : null}
          {body}
          {live && (!waitingApproval || turn.abortRequested) ? (
            <LiveStatus turn={turn} visible={visible} />
          ) : null}
          {live && waitingApproval ? (
            <p role="status" className="chat-waiting chat-enter">
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
          {!live && answer ? <MessageActions text={answer} at={turn.endedAt} side="reply" /> : null}
        </div>
      ) : (
        body
      )}
    </article>
  )
})
