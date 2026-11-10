import { useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import {
  Activity,
  CalendarClock,
  ChevronRight,
  Clock,
  Coins,
  Cpu,
  MessageSquare,
  Plus,
  RefreshCw,
  Stethoscope,
} from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import { useRpc } from '@/app/providers'
import { formatNumber, t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesOverview'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import { isUpcomingRun, nextRunAbs, nextRunText, type RawJob } from '@/views/cron/logic'
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
import { Button } from '@/web/components/ui/button'
import { PageHeader } from '@/web/components/ui/page-header'
import { ErrorState, LoadingRegion, Skeleton, StaleBadge } from '@/web/components/ui/states'
import { useSessionsTotal } from '@/web/features/sessions/sessions-total'
import { useDocumentVisible, useSessionsList } from '@/web/shell/session-data'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'

// Home (shell spec "Home page"): a summary of the runtime. Every figure is its
// own query, so one that fails shows its own error and Retry while the others
// keep rendering, and each figure links to the page with the detail.

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

function readinessTone(status?: string): string {
  switch (String(status || '').toLowerCase()) {
    case 'ready':
      return 'text-ok'
    case 'degraded':
      return 'text-warn'
    case 'action_required':
    case 'unavailable':
      return 'text-danger'
    default:
      return 'text-text'
  }
}

type FigureQuery = Pick<
  UseQueryResult,
  'data' | 'isPending' | 'isError' | 'error' | 'isFetching'
> & {
  refetch: () => unknown
}

/**
 * One summary figure: a titled card linking to its detail page. While its
 * query is pending it holds a skeleton of the value's size; a failure shows
 * the error and Retry inside the card and keeps a value it already had.
 */
function Figure({
  title,
  icon,
  to,
  linkLabel,
  query,
  children,
}: {
  title: string
  icon: ReactNode
  to: string
  linkLabel: string
  query: FigureQuery
  children: ReactNode
}) {
  const body = query.isPending ? (
    <LoadingRegion label={t('webPagesOverview.homeFigureLoading', { figure: title })}>
      <Skeleton className="h-7 w-32" />
      <Skeleton className="mt-1.5 h-4 w-40" />
    </LoadingRegion>
  ) : (
    children
  )
  return (
    <section
      aria-label={title}
      className="flex min-w-0 flex-col gap-2 rounded-lg border border-border bg-surface-1 p-4"
    >
      <header className="flex items-center gap-2">
        <span className="text-text-muted [&_svg]:size-4" aria-hidden>
          {icon}
        </span>
        <h2 className="flex-1 truncate text-small font-medium text-text-muted">{title}</h2>
        <Link
          to={to}
          aria-label={linkLabel}
          className="inline-flex items-center gap-0.5 text-small text-accent underline-offset-2 hover:underline"
        >
          {t('webPagesOverview.homeDetails')}
          <ChevronRight className="size-3.5" aria-hidden />
        </Link>
      </header>
      {query.isError ? (
        <ErrorState
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
    </section>
  )
}

function Value({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <p
      title={typeof children === 'string' ? children : undefined}
      className={cn('truncate text-xl font-semibold tabular-nums', className)}
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

/** The live feed of gateway events (every frame, newest first, capped at 30). */
function EventFeed() {
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
    <section
      aria-label={t('webPagesOverview.homeEvents')}
      className="flex min-w-0 flex-col gap-2 rounded-lg border border-border bg-surface-1 p-4"
    >
      <header className="flex items-center gap-2">
        <Activity className="size-4 text-text-muted" aria-hidden />
        <h2 className="flex-1 text-small font-medium text-text-muted">
          {t('webPagesOverview.homeEvents')}
        </h2>
        <Link
          to="/settings/debug"
          aria-label={t('webPagesOverview.homeEventsLink')}
          className="inline-flex items-center gap-0.5 text-small text-accent underline-offset-2 hover:underline"
        >
          {t('webPagesOverview.homeDetails')}
          <ChevronRight className="size-3.5" aria-hidden />
        </Link>
      </header>
      <div
        role="log"
        aria-live="polite"
        aria-label={t('webPagesOverview.homeEvents')}
        tabIndex={0}
        className="scroll-contained h-72 overflow-y-auto rounded-md bg-surface-0 font-mono text-small"
      >
        {events.length === 0 ? (
          <p className="p-3 text-text-muted">{t('webPagesOverview.homeEventsEmpty')}</p>
        ) : (
          <ul role="list">
            {events.map((event) => (
              <li key={event.id} className="flex min-w-0 gap-2 px-3 py-1 hover:bg-surface-2">
                <span className="shrink-0 text-text-muted">{event.ts}</span>
                <span className="shrink-0 font-semibold">{event.name}</span>
                <span className="min-w-0 truncate text-text-muted">{event.payload}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
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

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 md:p-8">
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

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <Figure
          title={t('webPagesOverview.homeReadiness')}
          icon={<Stethoscope />}
          to="/settings/health"
          linkLabel={t('webPagesOverview.homeReadinessLink')}
          query={doctor}
        >
          <Value className={readinessTone(doctor.data?.status)}>
            {readinessStatusLabel(doctor.data?.status)}
          </Value>
          <Hint>{doctor.data?.summary || t('webPagesOverview.homeReadinessHint')}</Hint>
        </Figure>

        <Figure
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
          title={t('webPagesOverview.homeUptime')}
          icon={<Clock />}
          to="/settings/gateway"
          linkLabel={t('webPagesOverview.homeUptimeLink')}
          query={status}
        >
          <Value>{formatUptime(status.data?.uptime_ms)}</Value>
          <Hint>
            {status.data?.version
              ? t('webPagesOverview.homeVersion', { version: status.data.version })
              : t('webPagesOverview.homeVersionUnknown')}
          </Hint>
        </Figure>

        <Figure
          title={t('webPagesOverview.homeSessions')}
          icon={<MessageSquare />}
          to="/sessions"
          linkLabel={t('webPagesOverview.homeSessionsLink')}
          query={sessions}
        >
          <Value>
            {totals.total !== null
              ? formatNumber(totals.total)
              : t('webPagesOverview.homeSessionsAtLeast', { count: formatNumber(totals.listed) })}
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
          title={t('webPagesOverview.homeUsage')}
          icon={<Coins />}
          to="/usage"
          linkLabel={t('webPagesOverview.homeUsageLink')}
          query={usage}
        >
          <Value>{t('webPagesOverview.homeTokens', { tokens: usageFigures?.tokens ?? 0 })}</Value>
          <Hint>
            {t('webPagesOverview.homeCost', {
              cost: formatCost(usageFigures?.cost ?? 0, { decimals: 2 }),
            })}
            {' · '}
            {tPlural('webPagesOverview.homeActiveToday', usageFigures?.activeToday ?? 0)}
          </Hint>
        </Figure>

        <Figure
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

      <EventFeed />
    </div>
  )
}
