import { useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import {
  Activity,
  ArrowRight,
  ArrowUpRight,
  CalendarClock,
  ChevronRight,
  Clock,
  Coins,
  Cpu,
  MessageSquare,
  Plus,
  RadioTower,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  ShieldQuestion,
  ShieldX,
} from 'lucide-react'
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { Link } from 'react-router'
import { useRpc } from '@/app/providers'
import { formatNumber, t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesOverview'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  isUpcomingRun,
  jobSchedule,
  nextRunAbs,
  nextRunText,
  type RawJob,
} from '@/views/cron/logic'
import { findingGroupKind, type Finding } from '@/views/health/logic'
import {
  formatEventPayload,
  formatEventTs,
  formatUptime,
  readinessStatusLabel,
} from '@/views/overview/logic'
import {
  activeSinceCount,
  formatCost,
  startOfLocalDay,
  usageMetrics,
  type UsageRow,
} from '@/views/usage/logic'
import { Badge, StatusDot } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Card } from '@/web/components/ui/card'
import { PageHeader } from '@/web/components/ui/page-header'
import { ErrorState, LoadingRegion, Skeleton, StaleBadge } from '@/web/components/ui/states'
import { useCountUp } from '@/web/components/ui/use-count-up'
import { useSessionsTotal } from '@/web/features/sessions/sessions-total'
import { useDocumentVisible, useSessionsList } from '@/web/shell/session-data'
import {
  sessionChannel,
  sessionIsRunning,
  sessionParent,
  sessionUpdatedMs,
  type SessionRow,
} from '@/web/shell/session-list'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { Countdown, Ring, UsageBars, usageColumns } from './home-widgets'
import { DetailsLink, IconTile, PAGE_CLASS, staggerStyle, type OverviewTone } from './overview-ui'

// Home (shell spec "Home page"): a bento dashboard of the runtime. Every
// figure is its own query, so one that fails shows its own error and Retry
// while the others keep rendering, and each links to the page with the
// detail.
//
// Layout (page-home.css): a large readiness lead with provider and uptime as
// compact cells beside it; usage (with a column chart) and the latest
// sessions; the next automation as a countdown ring beside a tall console of
// live gateway events. Things that tick (uptime, the countdown) are their own
// components, so a tick never re-runs an entrance or a count-up.

const POLL_MS = 30_000
const EVENT_LOG_CAP = 30

interface StatusPayload {
  uptime_ms?: number | null
  version?: string
  provider?: string | null
}

interface DoctorPayload {
  status?: string
  summary?: string
  findings?: Finding[]
}

interface RouteConfig {
  llm?: { model?: string }
  agentos_router?: { enabled?: boolean }
}

interface UsagePayload {
  sessions?: UsageRow[]
}

interface EventEntry {
  id: number
  ts: string
  name: string
  payload: string
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? '')
}

/**
 * The Usage card's figures. Usage rows are per-session lifetime totals and the
 * gateway keeps no per-day ledger, so the card sums all time and counts the
 * sessions active since local midnight rather than claiming "today's" spend.
 */
export function homeUsageFigures(rows: UsageRow[], now: number = Date.now()) {
  const all = usageMetrics(rows)
  return {
    tokens: all.totalTokens,
    cost: all.cost,
    activeToday: activeSinceCount(rows, startOfLocalDay(now)),
  }
}

/** "Good morning" / "Good afternoon" / "Good evening" for the local hour. */
export function homeGreeting(hour: number): string {
  if (hour >= 5 && hour < 12) return t('webPagesOverview.homeMorning')
  if (hour >= 12 && hour < 18) return t('webPagesOverview.homeAfternoon')
  return t('webPagesOverview.homeEvening')
}

/** The enabled job whose next run comes first, or null when none is scheduled. */
function nextScheduledJob(jobs: RawJob[]): RawJob | null {
  const upcoming = jobs.filter((job) => isUpcomingRun(job))
  upcoming.sort(
    (a, b) =>
      new Date(a.next_run as string | number).getTime() -
      new Date(b.next_run as string | number).getTime(),
  )
  return upcoming[0] ?? null
}

function readinessTone(status?: string): OverviewTone {
  switch (String(status || '').toLowerCase()) {
    case 'ready':
      return 'ok'
    case 'degraded':
      return 'warn'
    case 'action_required':
    case 'unavailable':
      return 'danger'
    default:
      return 'neutral'
  }
}

function statusPillLabel(status: string | undefined, pending: boolean): string {
  if (pending) return t('webPagesOverview.homeStatusChecking')
  switch (String(status || '').toLowerCase()) {
    case 'ready':
      return t('webPagesOverview.homeStatusReady')
    case 'degraded':
      return t('webPagesOverview.homeStatusDegraded')
    case 'action_required':
      return t('webPagesOverview.homeStatusAction')
    case 'unavailable':
      return t('webPagesOverview.homeStatusUnavailable')
    default:
      return readinessStatusLabel(status)
  }
}

const TONE_TEXT: Record<OverviewTone, string> = {
  neutral: 'text-text',
  accent: 'text-accent',
  ok: 'text-ok',
  warn: 'text-warn',
  danger: 'text-danger',
  info: 'text-info',
}

const READINESS_ICON: Record<OverviewTone, ReactNode> = {
  neutral: <ShieldQuestion />,
  accent: <ShieldQuestion />,
  info: <ShieldQuestion />,
  ok: <ShieldCheck />,
  warn: <ShieldAlert />,
  danger: <ShieldX />,
}

type FigureQuery = Pick<
  UseQueryResult,
  'data' | 'isPending' | 'isError' | 'error' | 'isFetching'
> & {
  refetch: () => unknown
}

/**
 * One block of the bento, named by its title, with a link to its page.
 * While its query is pending it holds a skeleton; a failure shows the error
 * and Retry inside the block and keeps a value it already had.
 *
 * `card` is a raised card with a "Details" link; `cell` is a compact, quieter
 * cell the whole of which follows the link (an arrow marks it).
 */
function Tile({
  area,
  index,
  title,
  icon,
  tone = 'neutral',
  to,
  linkLabel,
  query,
  variant = 'card',
  className,
  bodyClassName,
  headerExtra,
  children,
}: {
  area: string
  index: number
  title: string
  icon: ReactNode
  tone?: OverviewTone
  to: string
  linkLabel: string
  query: FigureQuery
  variant?: 'card' | 'cell'
  className?: string
  bodyClassName?: string
  headerExtra?: ReactNode
  children: ReactNode
}) {
  const cell = variant === 'cell'
  const body = query.isPending ? (
    <LoadingRegion
      label={t('webPagesOverview.homeFigureLoading', { figure: title })}
      className="flex flex-col gap-2 pt-0.5"
    >
      <Skeleton className={cell ? 'h-6 w-24' : 'h-8 w-32'} />
      <Skeleton className="h-4 w-36" />
    </LoadingRegion>
  ) : (
    <div className={cn('anim-fade-in flex min-w-0 flex-col', cell && 'mt-auto', bodyClassName)}>
      {children}
    </div>
  )
  const header = cell ? (
    <header className="flex min-w-0 items-center gap-2 text-text-muted">
      <span aria-hidden className="flex shrink-0 [&_svg]:size-3.5">
        {icon}
      </span>
      <h2 className="line-clamp-2 min-w-0 flex-1 text-label">{title}</h2>
      <Link
        to={to}
        viewTransition
        aria-label={linkLabel}
        title={linkLabel}
        className="target -mr-1 inline-flex size-6 shrink-0 items-center justify-center rounded-full text-text-muted transition-colors duration-[var(--duration-fast)] group-hover:text-accent after:absolute after:inset-0 after:rounded-[inherit] after:content-[''] hover:bg-accent-tint"
      >
        <ArrowUpRight
          aria-hidden
          className="size-3.5 transition-transform duration-[var(--duration-fast)] ease-[var(--ease-out)] group-hover:translate-x-px group-hover:-translate-y-px"
        />
      </Link>
    </header>
  ) : (
    <header className="flex min-w-0 items-center gap-2.5">
      <IconTile tone={tone}>{icon}</IconTile>
      <h2 className="min-w-0 truncate text-label text-text-muted">{title}</h2>
      {headerExtra}
      <span className="flex-1" />
      <DetailsLink to={to} label={linkLabel}>
        {t('webPagesOverview.homeDetails')}
      </DetailsLink>
    </header>
  )
  const content = (
    <>
      {header}
      {query.isError ? (
        // Above a cell's stretched link, so Retry stays clickable.
        <ErrorState
          className="relative z-10"
          title={t('webPagesOverview.homeFigureFailed')}
          message={errorText(query.error)}
          onRetry={() => void query.refetch()}
          retrying={query.isFetching}
        >
          {query.data === undefined ? null : body}
        </ErrorState>
      ) : (
        body
      )}
    </>
  )
  const style = staggerStyle(index)
  if (cell) {
    return (
      <section
        aria-label={title}
        data-area={area}
        className={cn('hm-cell hm-rise group flex flex-col gap-3 px-4 py-3.5', className)}
        style={style}
      >
        {content}
      </section>
    )
  }
  return (
    <Card asChild padding="none">
      <section
        aria-label={title}
        data-area={area}
        className={cn('hm-rise flex flex-col gap-4 p-4 sm:p-5', className)}
        style={style}
      >
        {content}
      </section>
    </Card>
  )
}

/** A whole number that counts up on arrival and on change. */
function Counted({ value, render }: { value: number; render: (n: number) => string }) {
  return <>{render(useCountUp(value))}</>
}

/** Wraps to two lines (a one-line ellipsis hid most of a hint). */
function Hint({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <p
      title={typeof children === 'string' ? children : undefined}
      className={cn('line-clamp-2 text-small break-words text-text-muted', className)}
    >
      {children}
    </p>
  )
}

/** A compact cell's value: one line, section-sized. */
function CellValue({ children }: { children: ReactNode }) {
  return (
    <p
      title={typeof children === 'string' ? children : undefined}
      className="hm-cell-value line-clamp-2 font-semibold break-words tabular-nums"
    >
      {children}
    </p>
  )
}

/**
 * Uptime, ticking once a second from the last status reply while connected.
 * Its own component so the tick re-renders only this text.
 */
function UptimeValue({
  ms,
  at,
  live,
}: {
  ms: number | null | undefined
  at: number
  live: boolean
}) {
  const [now, setNow] = useState(() => Date.now())
  const ticking = live && ms != null
  useEffect(() => {
    if (!ticking) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [ticking])
  const shown = ticking ? ms + Math.max(0, now - at) : ms
  return <CellValue>{formatUptime(shown)}</CellValue>
}

/** Most findings the lead lists before it says how many more there are. */
const FINDINGS_SHOWN = 3

/**
 * The findings that keep the runtime from ready, in the report's order, each
 * a row that leads to the fix. A lone finding also shows its detail, which
 * usually names the fix.
 */
function FindingList({ findings }: { findings: Finding[] }) {
  const shown = findings.slice(0, FINDINGS_SHOWN)
  const more = findings.length - shown.length
  const single = findings.length === 1
  return (
    <ul
      role="list"
      aria-label={t('webPagesOverview.homeReadinessFindings')}
      className="flex flex-col overflow-hidden rounded-control border border-border bg-surface-inset text-small shadow-inset"
    >
      {shown.map((finding, i) => (
        <li key={finding.id || i} className="border-t border-border first:border-t-0">
          <Link
            to="/settings/health"
            viewTransition
            className="hm-finding group/finding flex min-w-0 items-start gap-2.5 px-3 py-2.5"
          >
            <StatusDot
              tone={findingGroupKind(finding) === 'action' ? 'danger' : 'warn'}
              className="mt-1.5 size-2 shrink-0"
            />
            <span className="min-w-0 flex-1">
              <span className="block truncate font-medium" title={finding.title || finding.id}>
                {finding.title || finding.id}
              </span>
              {single && finding.detail ? (
                <span
                  title={finding.detail}
                  className="mt-0.5 line-clamp-2 break-words text-text-muted"
                >
                  {finding.detail}
                </span>
              ) : null}
            </span>
            {finding.surface ? (
              <span className="mt-px shrink-0 rounded-full border border-border px-1.5 font-mono text-text-muted">
                {finding.surface}
              </span>
            ) : null}
            <ChevronRight
              aria-hidden
              className="mt-0.5 size-3.5 shrink-0 text-text-muted transition-transform duration-[var(--duration-fast)] ease-[var(--ease-out)] group-hover/finding:translate-x-0.5"
            />
          </Link>
        </li>
      ))}
      {more > 0 ? (
        <li className="border-t border-border px-3 py-2 text-text-muted">
          {tPlural('webPagesOverview.homeReadinessMore', more)}
        </li>
      ) : null}
    </ul>
  )
}

/** The surfaces the doctor checked and found fine: a calm row of chips. */
function PassingSurfaces({ findings }: { findings: Finding[] }) {
  const surfaces = [
    ...new Set(
      findings
        .filter((f) => findingGroupKind(f) === 'ready')
        .map((f) => f.surface)
        .filter((s): s is string => !!s),
    ),
  ].slice(0, 8)
  if (surfaces.length === 0) return null
  return (
    <div className="flex flex-wrap gap-1.5">
      {surfaces.map((surface) => (
        <span
          key={surface}
          className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface-2/60 px-2 py-0.5 text-small text-text-muted"
        >
          <StatusDot tone="ok" className="size-1.5" />
          {surface}
        </span>
      ))}
    </div>
  )
}

function ReadinessLead({
  doctor,
  index,
}: {
  doctor: FigureQuery & { data?: DoctorPayload }
  index: number
}) {
  const data = doctor.data
  const tone = readinessTone(data?.status)
  const needsFix = tone === 'warn' || tone === 'danger'
  const findings = data?.findings ?? []
  // What makes the runtime not ready, straight from the same report: listed
  // so the lead says what to fix, not only that something is wrong.
  const attention = needsFix
    ? findings.filter((f) => {
        const kind = findingGroupKind(f)
        return kind === 'action' || kind === 'degraded'
      })
    : []
  const total = findings.length
  const passed = findings.filter((f) => {
    const kind = findingGroupKind(f)
    return kind === 'ready' || kind === 'optional'
  }).length
  const fraction = total > 0 ? passed / total : tone === 'ok' ? 1 : 0

  return (
    <Card asChild padding="none">
      <section
        aria-label={t('webPagesOverview.homeReadiness')}
        data-area="lead"
        data-ov-tone={tone}
        className={cn(
          'hm-lead hm-rise flex flex-col gap-5 p-5 sm:p-6',
          tone === 'danger' && 'border-danger/30',
          tone === 'warn' && 'border-warn/30',
        )}
        style={staggerStyle(index)}
      >
        <header className="flex min-w-0 items-center gap-2.5">
          <h2 className="min-w-0 flex-1 truncate text-label text-text-muted">
            {t('webPagesOverview.homeReadiness')}
          </h2>
          <DetailsLink to="/settings/health" label={t('webPagesOverview.homeReadinessLink')}>
            {t('webPagesOverview.homeDetails')}
          </DetailsLink>
        </header>
        {doctor.isPending ? (
          <LoadingRegion
            label={t('webPagesOverview.homeFigureLoading', {
              figure: t('webPagesOverview.homeReadiness'),
            })}
            className="flex items-center gap-5"
          >
            <Skeleton className="size-[5.5rem] rounded-full" />
            <div className="flex flex-col gap-2">
              <Skeleton className="h-9 w-48" />
              <Skeleton className="h-4 w-40" />
            </div>
          </LoadingRegion>
        ) : doctor.isError && data === undefined ? (
          <ErrorState
            title={t('webPagesOverview.homeFigureFailed')}
            message={errorText(doctor.error)}
            onRetry={() => void doctor.refetch()}
            retrying={doctor.isFetching}
          />
        ) : (
          <div className="anim-fade-in flex min-w-0 flex-1 flex-col gap-5">
            {doctor.isError ? (
              <ErrorState
                title={t('webPagesOverview.homeFigureFailed')}
                message={errorText(doctor.error)}
                onRetry={() => void doctor.refetch()}
                retrying={doctor.isFetching}
              />
            ) : null}
            <div className="flex min-w-0 items-center gap-5">
              {/* Passing checks in green; the rest of the ring in the state's tone. */}
              <Ring
                value={fraction}
                tone={total > 0 || tone === 'ok' ? 'ok' : tone}
                restTone={needsFix ? tone : undefined}
              >
                <span className={`flex ${TONE_TEXT[tone]}`}>{READINESS_ICON[tone]}</span>
              </Ring>
              <div className="flex min-w-0 flex-col gap-1">
                <p
                  // Not cn(): tailwind-merge reads text-stat as a colour.
                  className={`text-[2rem] leading-[1.1] font-semibold tracking-[-0.03em] text-balance ${TONE_TEXT[tone]}`}
                >
                  {readinessStatusLabel(data?.status)}
                </p>
                <Hint>{data?.summary || t('webPagesOverview.homeReadinessHint')}</Hint>
                {total > 0 ? (
                  <p className="text-small text-text-muted tabular-nums">
                    {t('webPagesOverview.homeChecks', {
                      passed: formatNumber(passed),
                      total: formatNumber(total),
                    })}
                  </p>
                ) : null}
              </div>
            </div>
            {attention.length > 0 ? <FindingList findings={attention} /> : null}
            {tone === 'ok' ? (
              <div className="flex flex-col gap-3">
                <p className="text-small text-text-muted">{t('webPagesOverview.homeAllClear')}</p>
                <PassingSurfaces findings={findings} />
              </div>
            ) : null}
            {needsFix ? (
              <div className="mt-auto">
                <Button asChild size="sm" variant="outline" className="group/fix">
                  <Link to="/settings/health" viewTransition>
                    {t('webPagesOverview.homeReadinessFix')}
                    <ArrowRight
                      aria-hidden
                      className="transition-transform duration-[var(--duration-fast)] ease-[var(--ease-out)] group-hover/fix:translate-x-0.5"
                    />
                  </Link>
                </Button>
              </div>
            ) : null}
          </div>
        )}
      </section>
    </Card>
  )
}

const DAY_MS = 86_400_000
/** Channels the split names before it folds the rest into "other". */
const CHANNELS_SHOWN = 4

/**
 * The sessions at a glance, without repeating the rail's list: how many,
 * how many run now, the split by channel and when each was last active over
 * the past day. Titles stay in the rail (and the Sessions page).
 */
export function sessionsPulse(rows: readonly SessionRow[], now: number = Date.now()) {
  const top = rows.filter((row) => !sessionParent(row))
  const counts = new Map<string, number>()
  for (const row of top) {
    const channel = sessionChannel(row)
    counts.set(channel, (counts.get(channel) ?? 0) + 1)
  }
  const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  const shown = ranked.slice(0, CHANNELS_SHOWN)
  const rest = ranked.slice(CHANNELS_SHOWN).reduce((n, [, c]) => n + c, 0)
  return {
    running: top.filter(sessionIsRunning).length,
    channels:
      rest > 0 ? [...shown, [t('webPagesOverview.homeSessionsOther'), rest] as const] : shown,
    // Position on the past-24-hours strip, 0 (a day ago) to 100 (now).
    day: top
      .map((row) => sessionUpdatedMs(row))
      .filter((ms) => ms > now - DAY_MS && ms <= now)
      .map((ms) => ((ms - (now - DAY_MS)) / DAY_MS) * 100),
  }
}

function SessionsPulse({ rows, figure }: { rows: SessionRow[]; figure: ReactNode }) {
  const pulse = sessionsPulse(rows)
  const total = pulse.channels.reduce((n, [, c]) => n + c, 0)
  if (total === 0) {
    // What the card will hold: a faint split and strip, then the way to start.
    return (
      <div className="flex flex-1 flex-col gap-4">
        <p className="text-stat text-text-muted tabular-nums">{figure}</p>
        <div aria-hidden className="flex flex-col gap-3">
          <span className="hm-split" data-ghost />
          <span className="hm-day" data-ghost />
        </div>
        <div className="mt-auto flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
          <div className="min-w-0">
            <p className="font-medium">{t('webPagesOverview.homeSessionsEmpty')}</p>
            <p className="text-small text-text-muted">
              {t('webPagesOverview.homeSessionsEmptyHint')}
            </p>
          </div>
          <Button asChild size="sm" variant="outline" className="group/start">
            <Link to="/new" viewTransition>
              {t('webPagesOverview.homeSessionsStart')}
              <ArrowRight
                aria-hidden
                className="transition-transform duration-[var(--duration-fast)] ease-[var(--ease-out)] group-hover/start:translate-x-0.5"
              />
            </Link>
          </Button>
        </div>
      </div>
    )
  }
  return (
    <div className="flex flex-1 flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <p className="text-stat tabular-nums">
          {figure}{' '}
          <span className="text-ui font-normal tracking-normal text-text-muted">
            {t('webPagesOverview.homeSessionsUnit')}
          </span>
        </p>
        {pulse.running > 0 ? (
          <Badge tone="accent" live className="mb-1">
            {tPlural('webPagesOverview.homeSessionsRunning', pulse.running)}
          </Badge>
        ) : null}
      </div>
      <div className="flex flex-col gap-2">
        <span aria-hidden className="hm-split">
          {pulse.channels.map(([channel, count], i) => (
            <span key={channel} data-c={i} style={{ flexGrow: count, '--i': i } as CSSProperties} />
          ))}
        </span>
        <ul
          role="list"
          aria-label={t('webPagesOverview.homeSessionsSplit')}
          className="flex flex-wrap gap-x-3.5 gap-y-1 text-small text-text-muted"
        >
          {pulse.channels.map(([channel, count], i) => (
            <li key={channel} className="inline-flex items-center gap-1.5">
              <span aria-hidden className="hm-key" data-c={i} />
              {channel}
              <span className="text-text tabular-nums">{formatNumber(count)}</span>
            </li>
          ))}
        </ul>
      </div>
      <div className="mt-auto flex flex-col gap-1.5">
        <span aria-hidden className="hm-day">
          {pulse.day.map((pct, i) => (
            <span key={i} style={{ '--x': pct, '--i': Math.min(i, 13) } as CSSProperties} />
          ))}
        </span>
        <span className="flex justify-between text-small text-text-muted">
          <span>{t('webPagesOverview.homeSessionsDay')}</span>
          <span>{t('webPagesOverview.homeSessionsDayEnd')}</span>
        </span>
      </div>
    </div>
  )
}

const EVENT_TONE: readonly [RegExp, OverviewTone][] = [
  [/error|fail/i, 'danger'],
  [/approval|exec/i, 'warn'],
  [/^(chat|agent)/i, 'accent'],
  [/^(tick|health|presence|heartbeat)/i, 'neutral'],
]

function eventTone(name: string): OverviewTone {
  return EVENT_TONE.find(([re]) => re.test(name))?.[1] ?? 'info'
}

const EVENT_TEXT: Record<OverviewTone, string> = {
  neutral: 'text-text-muted',
  accent: 'text-accent',
  ok: 'text-ok',
  warn: 'text-warn',
  danger: 'text-danger',
  info: 'text-info',
}

/** The live feed of gateway events (every frame, newest first, capped at 30). */
function EventConsole({ live, index }: { live: boolean; index: number }) {
  const rpc = useRpc()
  const [events, setEvents] = useState<EventEntry[]>([])
  const seq = useRef(0)

  useEffect(
    () =>
      rpc.on('*', (name: unknown, payload: unknown) => {
        const entry: EventEntry = {
          id: seq.current++,
          ts: formatEventTs(new Date()),
          name: String(name),
          payload: formatEventPayload(payload),
        }
        setEvents((prev) => [entry, ...prev].slice(0, EVENT_LOG_CAP))
      }),
    [rpc],
  )

  return (
    <Card asChild padding="none">
      <section
        aria-label={t('webPagesOverview.homeEvents')}
        data-area="events"
        className="hm-rise flex min-h-[21rem] min-w-0 flex-col overflow-hidden"
        style={staggerStyle(index)}
      >
        <header className="flex min-w-0 items-center gap-2.5 px-4 py-3.5 sm:px-5">
          <IconTile tone="accent">
            <Activity />
          </IconTile>
          <h2 className="min-w-0 truncate text-label text-text-muted">
            {t('webPagesOverview.homeEvents')}
          </h2>
          <Badge tone={live ? 'accent' : 'neutral'} live={live} dot={!live}>
            {live ? t('webPagesOverview.homeEventsLive') : t('webPagesOverview.homeEventsPaused')}
          </Badge>
          <span className="flex-1" />
          {events.length > 0 ? (
            <span className="hidden text-small text-text-muted tabular-nums sm:inline">
              {tPlural('webPagesOverview.homeEventsCount', events.length)}
            </span>
          ) : null}
          <DetailsLink to="/settings/debug" label={t('webPagesOverview.homeEventsLink')}>
            {t('webPagesOverview.homeDetails')}
          </DetailsLink>
        </header>
        <div
          role="log"
          aria-live="polite"
          aria-label={t('webPagesOverview.homeEvents')}
          tabIndex={0}
          className={cn(
            'scroll-contained relative min-h-0 flex-1 basis-64 overflow-y-auto border-t border-border bg-surface-inset font-mono text-small shadow-inset',
            events.length > 0 && 'hm-console',
          )}
        >
          {events.length === 0 ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center font-sans">
              <span className="hm-radar">
                <RadioTower aria-hidden />
                {live ? (
                  <StatusDot tone="accent" live className="absolute top-2 right-2 size-2" />
                ) : null}
              </span>
              <div>
                <p className="font-medium">{t('webPagesOverview.homeEventsEmpty')}</p>
                <p className="mt-0.5 max-w-xs text-small text-text-muted">
                  {t('webPagesOverview.homeEventsEmptyHint')}
                </p>
              </div>
            </div>
          ) : (
            <ol role="list" className="py-2">
              {events.map((event) => (
                <li
                  key={event.id}
                  className="hm-log-row grid min-w-0 grid-cols-[4.75rem_7.5rem_minmax(0,1fr)] sm:grid-cols-[4.75rem_11rem_minmax(0,1fr)] items-baseline gap-x-5 px-4 py-[3px] leading-5 hover:bg-surface-2 sm:px-5"
                >
                  <span className="text-text-muted tabular-nums">{event.ts}</span>
                  <span
                    className={cn(
                      'inline-flex min-w-0 items-center gap-1.5 font-medium',
                      EVENT_TEXT[eventTone(event.name)],
                    )}
                  >
                    <StatusDot className="size-1.5 shrink-0" />
                    <span className="truncate">{event.name}</span>
                  </span>
                  <span className="min-w-0 truncate text-text-muted">{event.payload}</span>
                </li>
              ))}
            </ol>
          )}
        </div>
      </section>
    </Card>
  )
}

export function HomePage() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const connection = useDisplayedConnection()
  const visible = useDocumentVisible()
  const poll = visible ? POLL_MS : false
  // Read once: the greeting does not flip while the page is open.
  const [greeting] = useState(() => homeGreeting(new Date().getHours()))

  const doctor = useQuery({
    queryKey: ['home', 'doctor.status'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<DoctorPayload>('doctor.status', { agentId: 'main', deep: false })
    },
    refetchInterval: poll,
    refetchOnWindowFocus: false,
  })
  const status = useQuery({
    queryKey: ['home', 'status'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<StatusPayload>('status', {})
    },
    refetchInterval: poll,
    refetchOnWindowFocus: false,
  })
  const route = useQuery({
    queryKey: ['home', 'config.get'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<RouteConfig>('config.get')
    },
    refetchInterval: poll,
    refetchOnWindowFocus: false,
  })
  const usage = useQuery({
    queryKey: ['home', 'usage.status'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<UsagePayload>('usage.status', {})
    },
    refetchInterval: poll,
    refetchOnWindowFocus: false,
  })
  const cron = useQuery({
    queryKey: ['home', 'cron.list'],
    queryFn: async () => {
      await rpc.waitForConnection()
      // cron.list answers with a bare array or `{jobs}`.
      const data = await rpc.call<RawJob[] | { jobs?: RawJob[] }>('cron.list', {})
      return Array.isArray(data) ? data : (data?.jobs ?? [])
    },
    refetchInterval: poll,
    refetchOnWindowFocus: false,
  })
  const sessions = useSessionsList()

  const refreshing =
    doctor.isFetching ||
    status.isFetching ||
    route.isFetching ||
    usage.isFetching ||
    cron.isFetching ||
    sessions.isFetching
  const refresh = gate(requireConnection(connection))
  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ['home'] })
    void sessions.refetch()
  }
  const offline = connection !== 'connected'
  const anyData = Boolean(doctor.data || status.data || usage.data || cron.data || sessions.data)

  const usageRows = usage.data?.sessions ?? []
  const usageFigures = usage.data ? homeUsageFigures(usageRows) : null
  const totals = useSessionsTotal(sessions.data?.length ?? 0)
  const nextJob = cron.data ? nextScheduledJob(cron.data) : null
  const routeLabel = route.data
    ? route.data.agentos_router?.enabled === true
      ? t('webPagesOverview.homeRoutePilot')
      : route.data.llm?.model || t('webPagesOverview.homeRouteUnset')
    : null
  const readiness = readinessTone(doctor.data?.status)
  const columns = usageColumns(usageRows)
  const tokensShown = (n: number) => formatNumber(n)

  // The one-line system summary under the greeting, from figures already
  // loaded (nothing in it ticks).
  const summary = [
    status.data
      ? status.data.provider
        ? t('webPagesOverview.homeSummaryProvider', { provider: status.data.provider })
        : t('webPagesOverview.homeSummaryNoProvider')
      : null,
    sessions.data
      ? tPlural('webPagesOverview.homeSummarySessions', totals.total ?? totals.listed)
      : null,
    usageFigures ? tPlural('webPagesOverview.homeActiveToday', usageFigures.activeToday) : null,
  ].filter(Boolean)

  return (
    <div className={PAGE_CLASS}>
      <PageHeader
        title={greeting}
        badge={
          offline && anyData ? (
            <StaleBadge />
          ) : (
            <Badge
              tone={readiness === 'neutral' ? 'neutral' : readiness}
              live={readiness === 'ok'}
              dot={readiness !== 'ok'}
              className="anim-fade-in"
            >
              {statusPillLabel(doctor.data?.status, doctor.isPending)}
            </Badge>
          )
        }
        description={summary.length > 0 ? summary.join(' · ') : t('webPagesOverview.homeSubtitle')}
        actions={
          <>
            <Button
              variant="outline"
              disabled={refresh.disabled || refreshing}
              title={refresh.reason ?? undefined}
              onClick={refreshAll}
            >
              <RefreshCw className={refreshing ? 'animate-spin' : undefined} aria-hidden />
              {t('webPagesOverview.refresh')}
            </Button>
            <Button variant="primary" asChild>
              <Link to="/new">
                <Plus aria-hidden />
                {t('webPagesOverview.homeNewSession')}
              </Link>
            </Button>
          </>
        }
        footer={
          refresh.reason ? <p className="text-small text-text-muted">{refresh.reason}</p> : null
        }
      />

      <div className="hm-bento">
        <div className="hm-grid">
          <ReadinessLead doctor={doctor} index={0} />

          <Tile
            area="prov"
            index={1}
            variant="cell"
            title={t('webPagesOverview.homeProvider')}
            icon={<Cpu />}
            to="/settings/models"
            linkLabel={t('webPagesOverview.homeProviderLink')}
            query={status}
          >
            <CellValue>{status.data?.provider || t('webPagesOverview.homeProviderNone')}</CellValue>
            <Hint className="line-clamp-1">
              {routeLabel
                ? t('webPagesOverview.homeRoute', { route: routeLabel })
                : route.isError
                  ? t('webPagesOverview.homeRouteFailed')
                  : t('webPagesOverview.homeRouteLoading')}
            </Hint>
          </Tile>

          <Tile
            area="up"
            index={2}
            variant="cell"
            title={t('webPagesOverview.homeUptime')}
            icon={<Clock />}
            to="/settings/gateway"
            linkLabel={t('webPagesOverview.homeUptimeLink')}
            query={status}
          >
            <UptimeValue
              ms={status.data?.uptime_ms}
              at={status.dataUpdatedAt}
              live={!offline && !status.isError}
            />
            <Hint className="line-clamp-1">
              {status.data?.version
                ? t('webPagesOverview.homeVersion', { version: status.data.version })
                : t('webPagesOverview.homeVersionUnknown')}
            </Hint>
          </Tile>

          <Tile
            area="usage"
            index={3}
            title={t('webPagesOverview.homeUsage')}
            icon={<Coins />}
            tone="accent"
            to="/usage"
            linkLabel={t('webPagesOverview.homeUsageLink')}
            query={usage}
            bodyClassName="gap-4"
          >
            <div className="flex flex-wrap items-end gap-x-8 gap-y-2">
              <div className="min-w-0">
                <p className="text-stat tabular-nums">
                  <Counted value={usageFigures?.tokens ?? 0} render={tokensShown} />{' '}
                  <span className="text-ui font-normal tracking-normal text-text-muted">
                    {t('webPagesOverview.homeTokensUnit')}
                  </span>
                </p>
              </div>
              <p className="text-[1.25rem] leading-8 font-semibold tracking-[-0.015em] tabular-nums">
                {formatCost(usageFigures?.cost ?? 0, { decimals: 2 })}{' '}
                <span className="text-ui font-normal tracking-normal text-text-muted">
                  {t('webPagesOverview.homeSpentUnit')}
                </span>
              </p>
            </div>
            <div className="relative flex flex-col gap-2">
              <UsageBars
                columns={columns}
                ghost={columns.length === 0}
                label={(c) =>
                  t('webPagesOverview.homeBarLabel', {
                    session: c.key,
                    input: formatNumber(c.input),
                    output: formatNumber(c.output),
                  })
                }
              />
              {columns.length === 0 ? (
                <div className="hm-chart-empty absolute inset-x-0 top-0 flex h-[5.5rem] flex-col items-center justify-center text-center">
                  <p className="font-medium">{t('webPagesOverview.homeUsageEmpty')}</p>
                  <p className="text-small text-text-muted">
                    {t('webPagesOverview.homeUsageEmptyHint')}
                  </p>
                </div>
              ) : null}
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-small text-text-muted">
                {columns.length > 0 ? (
                  <>
                    <span className="inline-flex items-center gap-1.5">
                      <span aria-hidden className="ov-swatch" />
                      {t('webPagesOverview.homeLegendInput')}
                    </span>
                    <span className="inline-flex items-center gap-1.5">
                      <span aria-hidden className="ov-swatch" data-series="output" />
                      {t('webPagesOverview.homeLegendOutput')}
                    </span>
                    <span className="hidden sm:inline">
                      {t('webPagesOverview.homeBarsCaption')}
                    </span>
                  </>
                ) : null}
                <span className="ml-auto">
                  {tPlural('webPagesOverview.homeActiveToday', usageFigures?.activeToday ?? 0)}
                </span>
              </div>
            </div>
          </Tile>

          <Tile
            area="sess"
            index={4}
            title={t('webPagesOverview.homeSessions')}
            icon={<MessageSquare />}
            to="/sessions"
            linkLabel={t('webPagesOverview.homeSessionsLink')}
            query={sessions}
            bodyClassName="flex-1 gap-3"
          >
            {totals.truncated && totals.total === null ? (
              <Hint>
                {t('webPagesOverview.homeSessionsPartialHint', {
                  count: formatNumber(totals.listed),
                })}
              </Hint>
            ) : null}
            <SessionsPulse
              rows={sessions.rows}
              figure={
                totals.total !== null ? (
                  <Counted value={totals.total} render={formatNumber} />
                ) : (
                  t('webPagesOverview.homeSessionsAtLeast', { count: formatNumber(totals.listed) })
                )
              }
            />
          </Tile>

          <Tile
            area="next"
            index={5}
            title={t('webPagesOverview.homeNextRun')}
            icon={<CalendarClock />}
            to="/automations"
            linkLabel={t('webPagesOverview.homeNextRunLink')}
            query={cron}
            bodyClassName="flex-1 items-center justify-center gap-4 text-center"
          >
            {nextJob ? (
              <>
                <Countdown job={nextJob} awaiting={nextRunText(nextJob)} />
                <p className="sr-only">{nextRunText(nextJob)}</p>
                <div className="flex max-w-full min-w-0 flex-col items-center gap-1">
                  <p
                    title={nextJob.name || nextJob.id || ''}
                    className="line-clamp-2 font-medium break-words"
                  >
                    {nextJob.name || nextJob.id || ''}
                  </p>
                  <p className="text-small text-text-muted">{nextRunAbs(nextJob)}</p>
                  <code className="mt-1 rounded-sm bg-surface-2 px-1.5 py-px font-mono text-small text-text-muted">
                    {jobSchedule(nextJob)}
                  </code>
                </div>
              </>
            ) : (
              <>
                <Ring value={0} dashed size="lg">
                  <CalendarClock className="text-text-muted" />
                </Ring>
                <div className="flex flex-col items-center gap-1">
                  <p className="font-medium">{t('webPagesOverview.homeNextRunNone')}</p>
                  <Hint>{t('webPagesOverview.homeNextRunNoneHint')}</Hint>
                  <Button asChild size="sm" variant="ghost" className="mt-1 text-accent">
                    <Link to="/automations" viewTransition>
                      {t('webPagesOverview.homeNextRunCreate')}
                      <ArrowRight aria-hidden />
                    </Link>
                  </Button>
                </div>
              </>
            )}
          </Tile>

          <EventConsole live={!offline} index={5} />
        </div>
      </div>
    </div>
  )
}
