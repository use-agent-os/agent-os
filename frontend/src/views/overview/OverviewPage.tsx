import './overview.css'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ActivityIcon,
  BotIcon,
  ChevronRightIcon,
  ClockIcon,
  CoinsIcon,
  MessageSquareIcon,
  RefreshCwIcon,
  StethoscopeIcon,
} from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { useBootstrap, useRpc } from '@/app/providers'
import {
  formatCost,
  formatEventPayload,
  formatEventTs,
  formatTokens,
  formatUptime,
  readinessStatusLabel,
  sessionStatusClass,
  sessionStatusLabel,
  relTime,
  sortRecentSessions,
  type OverviewSession,
} from './logic'

const WS_URL_KEY = 'agentos.wsUrl'
const WS_TOKEN_KEY = 'agentos.wsToken'
const POLL_MS = 30_000 // overview.js:173 — the 30s card-refresh cadence.
const EVENT_LOG_CAP = 30 // overview.js:328

// overview.js:232-248 — the status payload the Uptime/Provider tiles read.
interface StatusPayload {
  uptime_ms?: number | null
  version?: string
  provider?: string
}
// overview.js:250-258 — doctor.status (shallow) for the Health tile.
interface DoctorPayload {
  status?: string
  summary?: string
}
// overview.js:260-270 — usage totals for the token/cost/session tiles.
interface UsagePayload {
  totalSessions?: number | null
  totalTokens?: number | null
  totalCostUsd?: number | null
}
interface SessionsPayload {
  sessions?: OverviewSession[]
}

interface EventEntry {
  id: number
  ts: string
  eventName: string
  payloadStr: string
}

// One stat tile. `to` makes it a nav button; omit it for the static Uptime tile.
// `tone` maps a status to the --tone gutter primitive (Health count-tile
// posture) — status color never hardcoded.
function StatTile({
  label,
  icon,
  to,
  tone,
  ariaLabel,
  variant,
  loading = false,
  children,
}: {
  label: string
  icon: React.ReactNode
  to?: string
  tone?: 'ok' | 'warn' | 'err' | 'off'
  ariaLabel?: string
  variant?: 'hero'
  loading?: boolean
  children: React.ReactNode
}) {
  const navigate = useNavigate()
  const toneClass = tone ? ` tone-${tone === 'err' ? 'danger' : tone === 'off' ? 'dim' : tone}` : ''
  const className = `ov-stat${to ? '' : ' ov-stat--static'}${toneClass}${variant ? ` ov-stat--${variant}` : ''}${loading ? ' is-loading' : ''}`
  const content = (
    <>
      <span className="ov-stat__icon" aria-hidden="true">
        {icon}
      </span>
      <span className="ov-stat__label t-label">{label}</span>
      {children}
      {to ? (
        <span className="ov-stat__arrow" aria-hidden="true">
          <ChevronRightIcon />
        </span>
      ) : null}
    </>
  )
  if (!to) {
    return (
      <div className={className} aria-label={ariaLabel ?? label} aria-busy={loading}>
        {content}
      </div>
    )
  }
  return (
    <button
      type="button"
      className={className}
      aria-label={ariaLabel ?? label}
      aria-busy={loading}
      onClick={() => navigate(to)}
    >
      {content}
    </button>
  )
}

export function OverviewPage() {
  const rpc = useRpc()
  const bootstrap = useBootstrap()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  useEffect(() => {
    document.title = 'Overview - AgentOS Control'
  }, [])

  // overview.js:212-311 — four independent reads run in parallel; each tile
  // renders progressively off its own query so a slow call never blocks the
  // others. queryFn awaits the connection first (legacy _loadData did too).
  const statusQuery = useQuery<StatusPayload>({
    queryKey: ['overview', 'status'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<StatusPayload>('status', {})
    },
    // overview.js:245-248 — status is the one read that surfaces its failure to
    // the operator; legacy fired the toast once per failed call (no retries).
    retry: false,
    refetchInterval: POLL_MS,
    refetchOnWindowFocus: false,
  })

  // overview.js:245-248 — only the status read toasts on failure. A stable
  // toast id de-dupes the single visible notification across re-renders.
  useEffect(() => {
    if (statusQuery.isError) {
      const err = statusQuery.error
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to load status: ' + message, { id: 'overview-status-err' })
    }
  }, [statusQuery.isError, statusQuery.error])

  const doctorQuery = useQuery<DoctorPayload>({
    queryKey: ['overview', 'doctor.status'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<DoctorPayload>('doctor.status', { agentId: 'main', deep: false })
    },
    retry: false,
    refetchInterval: POLL_MS,
    refetchOnWindowFocus: false,
  })

  const usageQuery = useQuery<UsagePayload>({
    queryKey: ['overview', 'usage.status'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<UsagePayload>('usage.status', {})
    },
    refetchInterval: POLL_MS,
    refetchOnWindowFocus: false,
  })

  const sessionsQuery = useQuery<SessionsPayload>({
    queryKey: ['overview', 'sessions.list'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<SessionsPayload>('sessions.list', { limit: 5 })
    },
    refetchInterval: POLL_MS,
    refetchOnWindowFocus: false,
  })

  // overview.js:142,171 — Refresh reloads exactly the four card reads.
  const refreshAll = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['overview'] })
  }, [queryClient])

  // overview.js:166-169,317-350 — the wildcard `*` subscription feeds ONLY the
  // live event log (newest first, cap 30). It never invalidates card queries;
  // legacy refreshed cards solely via load + Refresh + the 30s interval.
  const [events, setEvents] = useState<EventEntry[]>([])
  const eventSeq = useRef(0)

  useEffect(() => {
    const unsub = rpc.on('*', (eventName: unknown, payload: unknown) => {
      const entry: EventEntry = {
        id: eventSeq.current++,
        ts: formatEventTs(new Date()),
        eventName: String(eventName),
        payloadStr: formatEventPayload(payload),
      }
      setEvents((prev) => [entry, ...prev].slice(0, EVENT_LOG_CAP))
    })
    return () => {
      unsub()
    }
  }, [rpc])
  // overview.js:159-163 — legacy also subscribed rpc.state to refresh the pill;
  // the console derives the pill from the reactive useConnection store instead,
  // so no per-view rpc.state subscription is needed (and, as above, state
  // changes never trigger a card refetch).

  // Connection panel (overview.js:102-116,147-156). Seed the inputs from stored
  // settings, tolerating blocked storage like legacy loadConnectionSettings.
  const [wsUrl, setWsUrl] = useState(() => readStored(WS_URL_KEY, localStorage) || bootstrap.ws_url)
  const [wsToken, setWsToken] = useState(() => readStored(WS_TOKEN_KEY, sessionStorage))

  function onConnect(): void {
    const url = wsUrl.trim()
    const token = wsToken.trim()
    // app.js:209-214 — persist URL (localStorage) + token (sessionStorage).
    try {
      localStorage.setItem(WS_URL_KEY, url || bootstrap.ws_url)
    } catch {
      /* storage blocked */
    }
    try {
      if (token) sessionStorage.setItem(WS_TOKEN_KEY, token)
      else sessionStorage.removeItem(WS_TOKEN_KEY)
    } catch {
      /* storage blocked */
    }
    rpc.disconnect()
    rpc.connect(url, token || undefined)
  }

  // Derived tile values.
  const status = statusQuery.data
  const usage = usageQuery.data
  const recent = sortRecentSessions(sessionsQuery.data?.sessions ?? [])
  // overview.js:272-310 — sessions.list `.catch(() => {})` left the initial
  // skeleton in place on failure; only a *successful* empty response rendered
  // the "No sessions yet" CTA. Distinguish error from empty so a failed read
  // never masquerades as an empty account.
  const sessionsFailed = sessionsQuery.isError
  const doctor = doctorQuery.data
  const doctorFailed = doctorQuery.isError
  const overviewRefreshing =
    statusQuery.isFetching ||
    doctorQuery.isFetching ||
    usageQuery.isFetching ||
    sessionsQuery.isFetching

  return (
    <div className="ov-stage">
      <header className="ov-stage__header">
        <div className="ov-stage__title-block">
          <span className="t-label">Control · Overview</span>
          <h1 className="t-display">Overview</h1>
          <p className="ov-stage__subtitle">
            Live status, recent sessions, and the gateway event stream.
          </p>
        </div>
        <div className="ov-stage__actions">
          <Button
            variant="outline"
            title="Refresh"
            className="text-xs uppercase tracking-[0.14em]"
            aria-busy={overviewRefreshing}
            disabled={overviewRefreshing}
            onClick={refreshAll}
          >
            <RefreshCwIcon className={overviewRefreshing ? 'ov-refresh-spin' : undefined} />
            <span>Refresh</span>
          </Button>
          <Button
            title="Open chat"
            className="text-xs uppercase tracking-[0.14em]"
            onClick={() => navigate('/chat')}
          >
            <MessageSquareIcon />
            <span>Open chat</span>
          </Button>
        </div>
      </header>

      <section className="ov-command" aria-label="Gateway summary">
        <div className="ov-command__toolbar">
          <div>
            <span className="ov-command__eyebrow">System pulse</span>
            <strong>Gateway summary</strong>
          </div>
          <span className="ov-command__cadence">
            <span aria-hidden="true" />
            Refreshes every 30s
          </span>
        </div>
        <div className="ov-command__body">
          <StatTile
            label="Health"
            icon={<StethoscopeIcon />}
            to="/health"
            tone={doctorFailed ? 'err' : readinessTone(doctor?.status)}
            variant="hero"
            loading={doctorQuery.isFetching}
          >
            <strong className="ov-stat__value ov-stat__value--status t-data">
              {doctorFailed ? 'Unavailable' : readinessStatusLabel(doctor?.status)}
            </strong>
            <span className="ov-stat__hint">
              {doctorFailed ? 'open health' : (doctor?.summary ?? 'view details')}
            </span>
          </StatTile>
          <div className="ov-command__metrics">
            <StatTile
              label="Total tokens"
              icon={<CoinsIcon />}
              to="/usage"
              loading={usageQuery.isFetching}
            >
              <strong className="ov-stat__value t-data">{formatTokens(usage?.totalTokens)}</strong>
              <span className="ov-stat__hint">
                {usage ? formatCost(usage.totalCostUsd) + ' spent' : 'view usage'}
              </span>
            </StatTile>
            <StatTile
              label="Total sessions"
              icon={<ActivityIcon />}
              to="/sessions"
              loading={usageQuery.isFetching}
            >
              {/* overview.js:262 — legacy printed the raw integer (data.totalSessions
                  ?? '—'); no toLocaleString grouping, unlike the token tile. */}
              <strong className="ov-stat__value t-data">{usage?.totalSessions ?? '—'}</strong>
              <span className="ov-stat__hint">view all</span>
            </StatTile>
            <StatTile
              label="Provider"
              icon={<BotIcon />}
              to="/agents"
              loading={statusQuery.isFetching}
            >
              <strong className="ov-stat__value ov-stat__value--mono t-data">
                {status?.provider ?? '—'}
              </strong>
              <span className="ov-stat__hint">manage agents</span>
            </StatTile>
            <StatTile label="Uptime" icon={<ClockIcon />} loading={statusQuery.isFetching}>
              <strong className="ov-stat__value ov-stat__value--mono t-data">
                {formatUptime(status?.uptime_ms)}
              </strong>
              <span className="ov-stat__hint">{status?.version ? `v${status.version}` : '—'}</span>
            </StatTile>
          </div>
        </div>
      </section>

      <div className="ov-grid">
        <section className="panel ov-panel ov-panel--recent">
          <div className="panel__head">
            <div className="ov-panel__heading">
              <MessageSquareIcon aria-hidden="true" />
              <div>
                <span>Recent sessions</span>
                <small>Resume the latest agent work</small>
              </div>
            </div>
            <button
              type="button"
              className="ov-link"
              onClick={() => navigate('/sessions')}
              aria-label="View all sessions"
            >
              View all →
            </button>
          </div>
          <div className="panel__body ov-recent">
            {sessionsFailed ? (
              // overview.js:272-310 — a failed sessions.list is not an empty
              // account: keep a neutral placeholder (legacy left the skeleton),
              // never the "No sessions yet" empty CTA.
              <div className="ov-recent__empty" role="status">
                <span>Recent sessions unavailable.</span>
              </div>
            ) : recent.length === 0 ? (
              <div className="ov-recent__empty">
                <MessageSquareIcon className="ov-recent__empty-icon" aria-hidden="true" />
                <span>No sessions yet — open chat to start your first one.</span>
              </div>
            ) : (
              recent.map((s) => {
                const status = (s.status || 'unknown').toLowerCase()
                const dot = sessionStatusClass(status)
                const label = sessionStatusLabel(status)
                const rel = s.updated_at ? relTime(s.updated_at) : '—'
                const msgs =
                  s.message_count != null ? `${Number(s.message_count).toLocaleString()} msg` : ''
                return (
                  <button
                    key={s.key}
                    type="button"
                    className="ov-recent__row"
                    aria-label={`Open session ${s.key}`}
                    onClick={() => navigate(`/chat?session=${encodeURIComponent(s.key ?? '')}`)}
                  >
                    <span
                      className={`ov-recent__dot tone-${dotTone(dot)}`}
                      title={label}
                      aria-label={label}
                    />
                    <span className="ov-recent__key t-data">{s.key}</span>
                    {s.model ? <span className="ov-recent__model t-data">{s.model}</span> : null}
                    <span className="ov-recent__msgs t-data">{msgs}</span>
                    <span className="ov-recent__time t-data">{rel}</span>
                    <span className="ov-recent__arrow" aria-hidden="true">
                      →
                    </span>
                  </button>
                )
              })
            )}
          </div>
        </section>

        <section className="panel ov-panel ov-panel--events">
          <div className="panel__head">
            <div className="ov-panel__heading">
              <ActivityIcon aria-hidden="true" />
              <div>
                <span>Event stream</span>
                <small>Live gateway activity</small>
              </div>
            </div>
            <span className="ov-panel__meta" data-slot="panel-meta">
              <span className="ov-panel__live" aria-hidden="true" />
              {events.length} event{events.length === 1 ? '' : 's'}
            </span>
          </div>
          <div className="panel__body ov-event-log" role="log" aria-live="polite">
            {events.length === 0 ? (
              <div className="ov-event-log__empty">
                <span className="ov-event-log__pulse" aria-hidden="true" />
                Listening for events…
              </div>
            ) : (
              events.map((e, i) => (
                <div className={`ov-event-log__row${i === 0 ? ' is-fresh' : ''}`} key={e.id}>
                  <span className="ov-event-log__ts t-data">{e.ts}</span>
                  <span className="ov-event-log__name t-data">{e.eventName}</span>
                  <span className="ov-event-log__payload t-data">{e.payloadStr}</span>
                </div>
              ))
            )}
          </div>
        </section>

        <section className="panel ov-panel ov-panel--conn">
          <div className="panel__head">
            <div className="ov-panel__heading">
              <BotIcon aria-hidden="true" />
              <div>
                <span>Gateway connection</span>
                <small>Override the active endpoint for this browser</small>
              </div>
            </div>
          </div>
          <div className="panel__body ov-form">
            <label className="ov-field">
              <span className="ov-field__label t-label">WebSocket URL</span>
              <input
                className="ov-field__input t-data"
                type="text"
                placeholder="ws://…"
                autoComplete="off"
                value={wsUrl}
                onChange={(e) => setWsUrl(e.target.value)}
              />
            </label>
            <label className="ov-field">
              <span className="ov-field__label t-label">
                Token <span className="ov-field__optional">optional</span>
              </span>
              <input
                className="ov-field__input"
                type="password"
                placeholder="—"
                autoComplete="off"
                value={wsToken}
                onChange={(e) => setWsToken(e.target.value)}
              />
            </label>
            <div className="ov-form__actions">
              <Button size="sm" onClick={onConnect}>
                Connect
              </Button>
              <Button size="sm" variant="outline" onClick={() => rpc.disconnect()}>
                Disconnect
              </Button>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}

// overview.js:352-365 — readiness status -> tone gutter (status color via
// --tone only). ready→ok, degraded→warn, action_required/unavailable→err.
function readinessTone(status?: string): 'ok' | 'warn' | 'err' | 'off' {
  switch (String(status || '').toLowerCase()) {
    case 'ready':
      return 'ok'
    case 'degraded':
      return 'warn'
    case 'action_required':
    case 'unavailable':
      return 'err'
    default:
      return 'off'
  }
}

// components.js dot variant ("ok"/"warn"/"err"/"off") -> --tone token name.
function dotTone(dot: string): 'ok' | 'warn' | 'danger' | 'dim' {
  if (dot === 'ok') return 'ok'
  if (dot === 'warn') return 'warn'
  if (dot === 'err') return 'danger'
  return 'dim'
}

function readStored(key: string, store: Storage): string {
  try {
    return store.getItem(key) || ''
  } catch {
    return ''
  }
}
