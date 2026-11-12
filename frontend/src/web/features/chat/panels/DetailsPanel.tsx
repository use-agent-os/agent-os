import {
  Activity,
  Bot,
  CalendarPlus,
  Copy,
  FileText,
  Folder,
  GitBranch,
  History,
  Inbox,
  ListChecks,
  Radio,
  Route,
  X,
} from 'lucide-react'
import { Link } from 'react-router'
import { toast } from 'sonner'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { copyWithFallback } from '@/lib/clipboard'
import { cn } from '@/lib/utils'
import { artifactName } from '@/views/chat/transcript/artifacts'
import { modelDisplayName } from '@/views/chat/transcript/routerFx'
import { sessionRunStatus } from '@/views/sessions/logic'
import { Badge } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { SimpleTooltip } from '@/web/components/ui/tooltip'
import { useAgents, useProjectNames, useSessionsList } from '@/web/shell/session-data'
import {
  sessionAgent,
  sessionChannel,
  sessionParent,
  sessionProject,
  sessionTitle,
  sessionUpdatedMs,
  type SessionRow,
} from '@/web/shell/session-list'
import { sessionKeyToPath } from '@/web/shell/session-url'
import '@/web/styles/chat-thread.css'
import { usePlanMode, useSessionUsage, useUnpinnedRoute } from '../composer/capabilities'
import type { TranscriptState } from '../model/types'
import { useChatActions } from '../transcript/context'

type Tone = 'neutral' | 'accent' | 'ok' | 'warn' | 'danger'
type RunState = 'running' | 'queued' | 'completed' | 'failed' | 'stopped' | 'idle'

const RUN_STATE: Record<RunState, { tone: Tone; live?: boolean; label: () => string }> = {
  running: { tone: 'accent', live: true, label: () => t('webChat.inspectorRunning') },
  queued: { tone: 'accent', live: true, label: () => t('webChat.inspectorQueued') },
  completed: { tone: 'ok', label: () => t('webChat.inspectorCompleted') },
  failed: { tone: 'danger', label: () => t('webChat.inspectorFailed') },
  stopped: { tone: 'neutral', label: () => t('webChat.inspectorStopped') },
  idle: { tone: 'neutral', label: () => t('webChat.inspectorIdle') },
}

/** The session's state: the live transcript first, then the row's task. */
function runState(row: SessionRow | undefined, transcript: TranscriptState): RunState {
  const last = transcript.turns.at(-1)
  if (last?.status === 'live') return 'running'
  const task = row ? sessionRunStatus(row) : 'idle'
  if (task === 'running' || task === 'queued') return task
  if (task === 'failed' || task === 'timeout') return 'failed'
  if (task === 'cancelled' || task === 'interrupted') return 'stopped'
  if (last?.status === 'failed') return 'failed'
  if (last?.status === 'aborted') return 'stopped'
  if (last?.status === 'done') return 'completed'
  return 'idle'
}

function StateBadge({ state }: { state: RunState }) {
  const s = RUN_STATE[state]
  return (
    <Badge tone={s.tone} dot={!s.live} live={s.live}>
      {s.label()}
    </Badge>
  )
}

function toMs(raw: unknown): number | null {
  if (typeof raw === 'number' && Number.isFinite(raw) && raw > 0)
    return raw < 1e10 ? raw * 1000 : raw
  if (typeof raw === 'string' && raw) {
    const n = Number(raw)
    if (Number.isFinite(n) && n > 0) return n < 1e10 ? n * 1000 : n
    const parsed = Date.parse(raw)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

const RELATIVE_STEPS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['second', 60],
  ['minute', 60],
  ['hour', 24],
  ['day', 7],
  ['week', 4.35],
  ['month', 12],
  ['year', Infinity],
]

/** "2 minutes ago", in the reader's locale. */
export function relativeTime(ms: number, now = Date.now()): string {
  let value = (ms - now) / 1000
  const format = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto', style: 'long' })
  for (const [unit, size] of RELATIVE_STEPS) {
    if (Math.abs(value) < size) return format.format(Math.round(value), unit)
    value /= size
  }
  return format.format(Math.round(value), 'year')
}

/** A faint dash for a value that is not there. */
function Empty() {
  return (
    <span className="chat-insp-empty" aria-label={t('webChat.detailsNone')}>
      —
    </span>
  )
}

/** A date as "2 minutes ago", the full date and time on hover and focus. */
function When({ ms }: { ms: number | null }) {
  if (!ms) return <Empty />
  const absolute = new Date(ms).toLocaleString()
  return (
    <SimpleTooltip label={absolute} side="left">
      <time
        dateTime={new Date(ms).toISOString()}
        title={absolute}
        tabIndex={0}
        className="rounded-sm whitespace-nowrap tabular-nums"
      >
        {relativeTime(ms)}
      </time>
    </SimpleTooltip>
  )
}

/** One fact: a muted icon and label on the left, the value on the right. */
function Row({
  icon,
  label,
  children,
  className,
}: {
  icon: React.ReactNode
  label: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <div className="chat-insp-row">
      <dt>
        <span className="chat-insp-icon" aria-hidden>
          {icon}
        </span>
        {label}
      </dt>
      <dd className={className}>{children}</dd>
    </div>
  )
}

/** A titled group of rows: an overline, then hairline-separated rows. */
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="chat-insp-section">
      <h3 className="chat-overline">{title}</h3>
      <dl>{children}</dl>
    </section>
  )
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="chat-insp-stat">
      <span className="chat-insp-stat-value">{value}</span>
      <span className="text-small text-text-muted">{label}</span>
    </div>
  )
}

function compactNumber(n: number): string {
  return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(
    n,
  )
}

/** The headline: the session's name, else its first message, never a raw key. */
function headline(row: SessionRow | undefined, transcript: TranscriptState): string {
  const named = row
    ? String(row.display_name || row.displayName || row.derived_title || row.derivedTitle || '')
    : ''
  if (named.trim()) return named.trim()
  const first = transcript.turns.find((turn) => turn.userMessage?.text.trim())
  const text = first?.userMessage?.text.trim().replace(/\s+/g, ' ') ?? ''
  if (text) return text.length > 64 ? `${text.slice(0, 63)}…` : text
  return t('webChat.inspectorUntitled')
}

/** The open session's context (chat spec "Details column"), as an inspector. */
export function DetailsPanel({
  sessionKey,
  row,
  transcript,
  onClose,
}: {
  sessionKey: string
  row: SessionRow | undefined
  transcript: TranscriptState
  onClose(): void
}) {
  const usage = useSessionUsage(sessionKey)
  const unpinned = useUnpinnedRoute()
  const plan = usePlanMode(sessionKey)
  const projects = useProjectNames()
  const agents = useAgents()
  const sessions = useSessionsList()
  const { openPanel } = useChatActions()
  const children = sessions.rows.filter((r) => sessionParent(r) === sessionKey)
  const artifacts = transcript.turns.flatMap((turn) =>
    turn.items.filter(
      (i): i is Extract<typeof i, { kind: 'artifact' | 'card' }> =>
        i.kind === 'artifact' || i.kind === 'card',
    ),
  )
  const project = row ? sessionProject(row) : null
  const route = transcript.turns
    .flatMap((turn) => turn.items)
    .filter((i) => i.kind === 'routerDecision')
    .at(-1)
  const state = runState(row, transcript)
  const agentId = row ? sessionAgent(row) : ''
  const agentName = agents.data?.find((a) => a.id === agentId)?.name ?? agentId
  const turns = transcript.turns.filter((turn) => turn.userMessage).length
  const tokens = usage.data ? usage.data.input + usage.data.output : null
  const cost = usage.data?.cost ?? null
  const channel = row ? sessionChannel(row) : ''

  const copyKey = () =>
    void copyWithFallback(sessionKey)
      .then(() => toast.success(t('webChat.copied')))
      .catch(() => toast.error(t('webChat.copyFailed')))

  return (
    <aside
      aria-label={t('webChat.detailsTitle')}
      className="chat-inspector flex h-full w-full flex-col"
    >
      <div className="flex min-h-12 items-center gap-2 px-4 pt-2">
        <h2 className="chat-overline flex-1">{t('webChat.detailsTitle')}</h2>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label={t('webChat.detailsClose')}
          onClick={onClose}
          className="-mr-1.5 text-text-muted hover:text-text"
        >
          <X />
        </Button>
      </div>
      <div className="scroll-contained chat-insp-body anim-stagger flex-1">
        <header className="flex flex-col items-start gap-2.5">
          <p className="chat-insp-title">{headline(row, transcript)}</p>
          <div className="flex max-w-full flex-wrap items-center gap-2">
            <StateBadge state={state} />
            <span className="chat-id-chip min-w-0">
              <span className="truncate">{sessionKey}</span>
              <button
                type="button"
                onClick={copyKey}
                aria-label={t('webChat.inspectorCopyId')}
                className="chat-id-copy target"
              >
                <Copy aria-hidden />
              </button>
            </span>
          </div>
        </header>

        <div className="chat-insp-stats">
          <Stat
            label={t('webChat.inspectorTokens')}
            value={tokens !== null ? compactNumber(tokens) : <Empty />}
          />
          <Stat
            label={t('webChat.inspectorCost')}
            value={cost !== null ? `$${cost < 1 ? cost.toFixed(4) : cost.toFixed(2)}` : <Empty />}
          />
          <Stat label={t('webChat.inspectorTurns')} value={turns} />
        </div>

        <Section title={t('webChat.inspectorSession')}>
          <Row icon={<Bot />} label={t('webChat.detailsAgent')}>
            {agentName || <Empty />}
          </Row>
          <Row icon={<Folder />} label={t('webChat.detailsProject')}>
            {project ? (projects.data?.get(project) ?? project) : <Empty />}
          </Row>
          <Row icon={<Radio />} label={t('webChat.detailsChannel')} className="capitalize">
            {channel || <Empty />}
          </Row>
          <Row icon={<CalendarPlus />} label={t('webChat.detailsCreated')}>
            <When ms={toMs(row?.created_at ?? row?.createdAt)} />
          </Row>
          <Row icon={<History />} label={t('webChat.detailsUpdated')}>
            <When ms={row ? sessionUpdatedMs(row) || null : null} />
          </Row>
        </Section>

        <Section title={t('webChat.inspectorRuntime')}>
          <Row icon={<Route />} label={t('webChat.detailsRoute')}>
            <span className="chat-id-chip max-w-full">
              <span className="truncate">
                {route && route.kind === 'routerDecision'
                  ? `${modelDisplayName(route.model)}${route.tier ? ` · ${route.tier}` : ''}`
                  : row?.model
                    ? modelDisplayName(String(row.model))
                    : unpinned}
              </span>
            </span>
          </Row>
          <Row icon={<ListChecks />} label={t('webChat.detailsPlan')}>
            <Badge tone={plan.on ? 'accent' : 'neutral'}>
              {plan.on ? t('webChat.planOn') : t('webChat.planOff')}
            </Badge>
          </Row>
        </Section>

        <Section title={t('webChat.inspectorActivity')}>
          <Row icon={<Activity />} label={t('webChat.detailsTask')}>
            <StateBadge state={state} />
          </Row>
          <Row
            icon={<Inbox />}
            label={t('webChat.detailsQueued')}
            className={cn('tabular-nums', !transcript.queued && 'text-text-muted')}
          >
            {transcript.queued}
          </Row>
          <Row icon={<GitBranch />} label={t('webChat.detailsSubagents')}>
            {children.length ? (
              <ul className="flex flex-col items-end gap-1">
                {children.map((child) => (
                  <li key={child.key} className="max-w-full truncate">
                    <Link
                      to={sessionKeyToPath(child.key)}
                      viewTransition
                      className="text-accent underline-offset-2 hover:underline"
                    >
                      {sessionTitle(child)}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty />
            )}
          </Row>
          <Row icon={<FileText />} label={t('webChat.detailsArtifacts')} className="tabular-nums">
            {artifacts.length || <Empty />}
          </Row>
        </Section>

        {artifacts.length ? (
          <section className="chat-insp-section">
            <h3 className="chat-overline">{t('webChat.detailsArtifacts')}</h3>
            <ul className="flex flex-col gap-1">
              {artifacts.map((item) => {
                const name = artifactName(item.artifact)
                return (
                  <li key={item.id}>
                    <button
                      type="button"
                      className="chat-insp-file press target"
                      onClick={(e) =>
                        openPanel({ kind: 'artifact', id: item.id, title: name }, e.currentTarget)
                      }
                    >
                      <span className="chat-insp-icon" aria-hidden>
                        <FileText />
                      </span>
                      <span className="min-w-0 flex-1 truncate">{name}</span>
                    </button>
                  </li>
                )
              })}
            </ul>
          </section>
        ) : null}
      </div>
    </aside>
  )
}
