import { useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import {
  Activity,
  ArrowRight,
  CalendarClock,
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
import { isUpcomingRun, nextRunAbs, nextRunText, type RawJob } from '@/views/cron/logic'
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
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import {
  DetailsLink,
  IconTile,
  PAGE_CLASS,
  STAT_FIGURE,
  STAT_LABEL,
  staggerStyle,
  type OverviewTone,
} from './overview-ui'

// Home (shell spec "Home page"): a summary of the runtime. Every figure is its
// own query, so one that fails shows its own error and Retry while the others
// keep rendering, and each figure links to the page with the detail.
//
// Layout: a lead panel (readiness, provider and route, uptime) whose tone
// tells the state at a glance and, when something needs fixing, leads to the
// fix; then the activity figures as clickable tiles; then the live feed.

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
 * One summary figure, named by its title, with a "Details" link to its page.
 * While its query is pending it holds a skeleton of the value's size; a
 * failure shows the error and Retry inside the figure and keeps a value it
 * already had. `tile` renders a raised card the whole of which follows the
 * link (and lifts on hover); `pane` renders a section of the lead panel.
 */
function Figure({
  title,
  icon,
  tone = 'neutral',
  to,
  linkLabel,
  query,
  variant = 'tile',
  className,
  bodyClassName,
  style,
  children,
}: {
  title: string
  icon: ReactNode
  tone?: OverviewTone
  to: string
  linkLabel: string
  query: FigureQuery
  variant?: 'tile' | 'pane'
  className?: string
  bodyClassName?: string
  style?: CSSProperties
  children: ReactNode
}) {
  const body = query.isPending ? (
    <LoadingRegion
      label={t('webPagesOverview.homeFigureLoading', { figure: title })}
      className="flex flex-col gap-2 pt-0.5"
    >
      <Skeleton className="h-8 w-32" />
      <Skeleton className="h-4 w-40" />
    </LoadingRegion>
  ) : (
    <div className={cn('anim-fade-in flex min-w-0 flex-col gap-1', bodyClassName)}>{children}</div>
  )
  const content = (
    <>
      <header className="flex min-w-0 items-center gap-2.5">
        <IconTile tone={tone}>{icon}</IconTile>
        <h2 className={STAT_LABEL}>{title}</h2>
        <DetailsLink to={to} label={linkLabel} stretched={variant === 'tile'}>
          {t('webPagesOverview.homeDetails')}
        </DetailsLink>
      </header>
      {query.isError ? (
        // Above the stretched link, so Retry stays clickable.
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
  if (variant === 'pane') {
    return (
      <section
        aria-label={title}
        className={cn('relative flex min-w-0 flex-col gap-2 p-4 sm:p-5', className)}
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
        className={cn('group lift relative flex flex-col gap-2 p-4 sm:p-5', className)}
        style={style}
      >
        {content}
      </section>
    </Card>
  )
}

function Value({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <p
      title={typeof children === 'string' ? children : undefined}
      // Not cn(): tailwind-merge reads text-stat as a colour and would drop it
      // in favour of a tone class.
      className={`${STAT_FIGURE} ${className ?? ''}`}
    >
      {children}
    </p>
  )
}

/** Wraps to two lines (a one-line ellipsis hid most of the hint on a 1280 px card). */
function Hint({ children }: { children: ReactNode }) {
  return (
    <p
      title={typeof children === 'string' ? children : undefined}
      className="line-clamp-2 text-small break-words text-text-muted"
    >
      {children}
    </p>
  )
}

/** A whole number that counts up on arrival and on change. */
function Counted({ value, render }: { value: number; render: (n: number) => string }) {
  return <>{render(useCountUp(value))}</>
}

/**
 * Uptime, ticking once a second from the last status reply while connected.
 * Its own component so the tick re-renders only this text: no card, entrance
 * or count-up above it re-runs.
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
  return <Value>{formatUptime(shown)}</Value>
}

/** Most findings the lead lists before it says how many more there are. */
const FINDINGS_SHOWN = 3

/**
 * The findings that keep the runtime from ready, in the report's order. A
 * lone finding also shows its detail, which usually names the fix.
 */
function FindingList({ findings }: { findings: Finding[] }) {
  const shown = findings.slice(0, FINDINGS_SHOWN)
  const more = findings.length - shown.length
  const single = findings.length === 1
  return (
    <ul
      role="list"
      aria-label={t('webPagesOverview.homeReadinessFindings')}
      className="mt-2 flex flex-col overflow-hidden rounded-control border border-border bg-surface-inset text-small shadow-inset"
    >
      {shown.map((finding, i) => (
        <li
          key={finding.id || i}
          className="flex min-w-0 items-start gap-2.5 border-t border-border px-3 py-2 first:border-t-0"
        >
          <StatusDot
            tone={findingGroupKind(finding) === 'action' ? 'danger' : 'warn'}
            className="mt-1 size-2 shrink-0"
          />
          <span className="min-w-0 flex-1">
            <span className="block truncate" title={finding.title || finding.id}>
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
            <span className="shrink-0 text-text-muted">{finding.surface}</span>
          ) : null}
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

const EVENT_TONE: readonly [RegExp, string][] = [
  [/error|fail/i, 'bg-danger/10 text-danger'],
  [/approval|exec/i, 'bg-warn/10 text-warn'],
  [/^(chat|agent)/i, 'bg-accent-tint text-accent'],
  [/^(tick|health|presence|heartbeat)/i, 'bg-surface-3 text-text-muted'],
]

function eventChip(name: string): string {
  return EVENT_TONE.find(([re]) => re.test(name))?.[1] ?? 'bg-info/10 text-info'
}

/** The live feed of gateway events (every frame, newest first, capped at 30). */
function EventFeed({ live, style }: { live: boolean; style?: CSSProperties }) {
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
        className="ov-rise flex min-w-0 flex-col overflow-hidden"
        style={style}
      >
        <header className="flex min-w-0 items-center gap-2.5 px-5 py-4">
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
          <DetailsLink to="/settings/debug" label={t('webPagesOverview.homeEventsLink')}>
            {t('webPagesOverview.homeDetails')}
          </DetailsLink>
        </header>
        <div
          role="log"
          aria-live="polite"
          aria-label={t('webPagesOverview.homeEvents')}
          tabIndex={0}
          className="scroll-contained h-72 overflow-y-auto border-t border-border bg-surface-inset font-mono text-small shadow-inset"
        >
          {events.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center font-sans">
              <span className="relative inline-flex">
                <IconTile tone="accent" size="lg">
                  <RadioTower />
                </IconTile>
                {live ? (
                  <StatusDot
                    tone="accent"
                    live
                    className="absolute -top-0.5 -right-0.5 size-2.5 ring-2 ring-[var(--surface-inset)]"
                  />
                ) : null}
              </span>
              <div>
                <p className="font-medium">{t('webPagesOverview.homeEventsEmpty')}</p>
                <p className="mt-0.5 text-small text-text-muted">
                  {t('webPagesOverview.homeEventsEmptyHint')}
                </p>
              </div>
            </div>
          ) : (
            <ol role="list" className="py-1.5">
              {events.map((event) => (
                <li
                  key={event.id}
                  className="ov-feed-row grid min-w-0 grid-cols-[auto_auto_minmax(0,1fr)] items-center gap-3 px-5 py-1 transition-colors duration-[var(--duration-fast)] hover:bg-surface-2"
                >
                  <span className="text-text-muted tabular-nums">{event.ts}</span>
                  <span
                    className={cn(
                      'max-w-48 truncate rounded px-1.5 py-px font-medium',
                      eventChip(event.name),
                    )}
                  >
                    {event.name}
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

  const usageFigures = usage.data ? homeUsageFigures(usage.data.sessions ?? []) : null
  const totals = useSessionsTotal(sessions.data?.length ?? 0)
  const nextJob = cron.data ? nextScheduledJob(cron.data) : null
  const routeLabel = route.data
    ? route.data.agentos_router?.enabled === true
      ? t('webPagesOverview.homeRoutePilot')
      : route.data.llm?.model || t('webPagesOverview.homeRouteUnset')
    : null
  const readiness = readinessTone(doctor.data?.status)
  const needsFix = readiness === 'warn' || readiness === 'danger'
  // What makes the runtime not ready, straight from the same report: listed
  // in the lead so it says what to fix, not only that something is wrong.
  const attention = needsFix
    ? (doctor.data?.findings ?? []).filter((f) => {
        const kind = findingGroupKind(f)
        return kind === 'action' || kind === 'degraded'
      })
    : []
  // With findings to list the readiness pane takes the left column and the
  // other two figures stack beside it; without, all three share one row.
  const listed = attention.length > 0

  return (
    <div className="bg-atmosphere min-h-full">
      <div className={PAGE_CLASS}>
        <PageHeader
          title={t('webPagesOverview.homeTitle')}
          badge={offline && anyData ? <StaleBadge /> : null}
          description={t('webPagesOverview.homeSubtitle')}
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

        {/* The lead: is the runtime ready, on what, and for how long. */}
        <Card
          padding="none"
          data-ov-tone={readiness}
          className={cn(
            'ov-lead ov-rise grid grid-cols-1',
            listed ? 'md:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]' : 'md:grid-cols-3',
            readiness === 'danger' && 'border-danger/30',
            readiness === 'warn' && 'border-warn/30',
          )}
          style={staggerStyle(0)}
        >
          <Figure
            variant="pane"
            title={t('webPagesOverview.homeReadiness')}
            icon={READINESS_ICON[readiness]}
            tone={readiness}
            to="/settings/health"
            linkLabel={t('webPagesOverview.homeReadinessLink')}
            query={doctor}
            bodyClassName="flex-1"
          >
            <div className="flex min-w-0 items-center gap-2.5">
              {readiness === 'ok' ? <StatusDot tone="ok" live className="size-2.5" /> : null}
              <Value className={TONE_TEXT[readiness]}>
                {readinessStatusLabel(doctor.data?.status)}
              </Value>
            </div>
            <Hint>{doctor.data?.summary || t('webPagesOverview.homeReadinessHint')}</Hint>
            {listed ? <FindingList findings={attention} /> : null}
            {needsFix ? (
              <div className="mt-auto pt-2">
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
          </Figure>

          <div
            className={cn(
              'flex min-w-0 flex-col border-t border-border md:border-t-0',
              listed ? 'md:border-l' : 'md:contents',
            )}
          >
            <Figure
              variant="pane"
              className={cn('flex-1', !listed && 'md:border-l md:border-border')}
              title={t('webPagesOverview.homeProvider')}
              icon={<Cpu />}
              to="/settings/models"
              linkLabel={t('webPagesOverview.homeProviderLink')}
              query={status}
            >
              <Value>{status.data?.provider || t('webPagesOverview.homeProviderNone')}</Value>
              <Hint>
                {routeLabel
                  ? t('webPagesOverview.homeRoute', { route: routeLabel })
                  : route.isError
                    ? t('webPagesOverview.homeRouteFailed')
                    : t('webPagesOverview.homeRouteLoading')}
              </Hint>
            </Figure>

            <Figure
              variant="pane"
              className={cn(
                'flex-1 border-t border-border',
                !listed && 'md:border-t-0 md:border-l',
              )}
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
              <Hint>
                {status.data?.version
                  ? t('webPagesOverview.homeVersion', { version: status.data.version })
                  : t('webPagesOverview.homeVersionUnknown')}
              </Hint>
            </Figure>
          </div>
        </Card>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-4 xl:grid-cols-3">
          <Figure
            className="ov-rise"
            style={staggerStyle(1)}
            title={t('webPagesOverview.homeSessions')}
            icon={<MessageSquare />}
            to="/sessions"
            linkLabel={t('webPagesOverview.homeSessionsLink')}
            query={sessions}
          >
            <Value>
              {totals.total !== null ? (
                <Counted value={totals.total} render={formatNumber} />
              ) : (
                t('webPagesOverview.homeSessionsAtLeast', { count: formatNumber(totals.listed) })
              )}
            </Value>
            <Hint>
              {totals.truncated && totals.total === null
                ? t('webPagesOverview.homeSessionsPartialHint', {
                    count: formatNumber(totals.listed),
                  })
                : t('webPagesOverview.homeSessionsHint')}
            </Hint>
          </Figure>

          <Figure
            className="ov-rise"
            style={staggerStyle(2)}
            title={t('webPagesOverview.homeUsage')}
            icon={<Coins />}
            to="/usage"
            linkLabel={t('webPagesOverview.homeUsageLink')}
            query={usage}
          >
            <Value>
              <Counted
                value={usageFigures?.tokens ?? 0}
                render={(tokens) => t('webPagesOverview.homeTokens', { tokens })}
              />
            </Value>
            <Hint>
              {t('webPagesOverview.homeCost', {
                cost: formatCost(usageFigures?.cost ?? 0, { decimals: 2 }),
              })}
              {' · '}
              {tPlural('webPagesOverview.homeActiveToday', usageFigures?.activeToday ?? 0)}
            </Hint>
          </Figure>

          <Figure
            className="ov-rise sm:col-span-2 xl:col-span-1"
            style={staggerStyle(3)}
            title={t('webPagesOverview.homeNextRun')}
            icon={<CalendarClock />}
            to="/automations"
            linkLabel={t('webPagesOverview.homeNextRunLink')}
            query={cron}
          >
            {nextJob ? (
              <>
                <Value>{nextRunText(nextJob)}</Value>
                <Hint>
                  {t('webPagesOverview.homeNextRunJob', {
                    name: nextJob.name || nextJob.id || '',
                    when: nextRunAbs(nextJob),
                  })}
                </Hint>
              </>
            ) : (
              <>
                <Value className="text-text-muted">{t('webPagesOverview.homeNextRunNone')}</Value>
                <Hint>{t('webPagesOverview.homeNextRunNoneHint')}</Hint>
              </>
            )}
          </Figure>
        </div>

        <EventFeed live={!offline} style={staggerStyle(4)} />
      </div>
    </div>
  )
}
