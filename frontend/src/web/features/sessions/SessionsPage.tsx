import { useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  Bot,
  ChevronDown,
  Clock,
  ExternalLink,
  FolderInput,
  FolderKanban,
  Hash,
  Loader2,
  MessageCircle,
  MessageSquare,
  MessagesSquare,
  MoreHorizontal,
  Pencil,
  Plus,
  Search,
  Send,
  Trash2,
  X,
} from 'lucide-react'
import { useId, useMemo, useState, type ReactNode } from 'react'
import { Link, useHref } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { formatNumber, t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { gate, requireConnection, type Gate } from '@/lib/disabled-reason'
import { usePendingQuestions } from '@/lib/pending-questions'
import { cn } from '@/lib/utils'
import { filterSessionsByProject } from '@/views/projects/logic'
import {
  agentSubline,
  buildDeleteParams,
  filterSessions,
  parseBulkDeleteResult,
  parseSingleDeleteResult,
  relTimeLabel,
  runStatusBadge,
  sessionName,
  sessionRunStatus,
  sessionStats,
  sessionStatusChip,
  sessionVisualStatus,
  sortSessions,
  type DeleteResult,
  type SortColumn,
  type Tone,
} from '@/views/sessions/logic'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/web/components/ui/alert-dialog'
import { Badge, StatusDot } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Input } from '@/web/components/ui/input'
import { PageHeader } from '@/web/components/ui/page-header'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import {
  useAgents,
  useProjectNames,
  useSessionsList,
  SESSIONS_QUERY_KEY,
} from '@/web/shell/session-data'
import {
  sessionAgent,
  sessionChannel,
  sessionProject,
  sessionTitle,
  type SessionRow,
} from '@/web/shell/session-list'
import { ChannelIcon } from '@/web/shell/channel-icon'
import { openInNewTab } from '@/web/shell/SessionList'
import { sessionKeyToPath } from '@/web/shell/session-url'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { PAGE_CLASS } from '@/web/features/home/overview-ui'
import { hueOf } from '@/web/features/projects/hue'
import { FilterMenu, type FilterOption } from './FilterMenu'
import { MoveDialog, NewSessionDialog } from './session-dialogs'
import { useSessionsTotal, type SessionsTotal } from './sessions-total'
import { useArrival, useFreshKeys } from './use-arrival'
import '@/web/styles/page-projects.css'
import '@/web/styles/page-sessions.css'

// `/sessions` (task 8.2): every session in one refined list, on
// views/sessions/logic. A one-line summary under the title, a toolbar of
// search and filter chips (agent, project, status), rows with a channel tile,
// inline rename, a row menu, bulk delete and move, a new-session dialog that
// can create an agent, and a flag on rows whose agent is no longer
// registered. Narrow containers turn the rows into cards.

const PAGE_SIZE = 50
const ALL = 'all'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

type Confirm = { keys: string[] } | null
type Notice = { tone: 'ok' | 'warn' | 'danger'; text: string } | null

/** The status filter's buckets, on the row's visual status. */
type StatusFilter = typeof ALL | 'executing' | 'running' | 'done' | 'failed' | 'killed'

function matchesStatus(row: SessionRow, status: StatusFilter): boolean {
  if (status === ALL) return true
  if (status === 'executing') {
    const run = sessionRunStatus(row)
    return run === 'queued' || run === 'running'
  }
  const visual = sessionVisualStatus(row)
  if (status === 'failed') return visual === 'failed' || visual === 'timeout'
  return visual === status
}

export function SessionsPage() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const sessions = useSessionsList()
  const agents = useAgents()
  const projects = useProjectNames()
  const connection = useDisplayedConnection()
  const act = gate(requireConnection(connection))
  const reasonId = useId()

  const [search, setSearch] = useState('')
  const [agentFilter, setAgentFilter] = useState(ALL)
  const [projectFilter, setProjectFilter] = useState(ALL)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>(ALL)
  const [sortCol, setSortCol] = useState<SortColumn>('updated_at')
  const [sortAsc, setSortAsc] = useState(false)
  const [limit, setLimit] = useState(PAGE_SIZE)
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [confirm, setConfirm] = useState<Confirm>(null)
  const [moveKeys, setMoveKeys] = useState<string[] | null>(null)
  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [notice, setNotice] = useState<Notice>(null)

  const rows = sessions.rows
  const agentsById = useMemo(
    () => new Map((agents.data ?? []).map((a) => [a.id, a] as const)),
    [agents.data],
  )
  const projectNames = projects.data

  const filtered = useMemo(() => {
    const byAgent = agentFilter === ALL ? rows : rows.filter((r) => sessionAgent(r) === agentFilter)
    const byProject = filterSessionsByProject(byAgent, projectFilter) as SessionRow[]
    const byStatus = byProject.filter((r) => matchesStatus(r, statusFilter))
    return sortSessions(filterSessions(byStatus, search), sortCol, sortAsc) as SessionRow[]
  }, [rows, agentFilter, projectFilter, statusFilter, search, sortCol, sortAsc])
  const visible = filtered.slice(0, limit)
  const stats = useMemo(() => sessionStats(rows), [rows])
  const totals = useSessionsTotal(sessions.data?.length ?? 0)

  // Selection only ever holds keys that are still listed: a row deleted
  // elsewhere drops out instead of lingering in the bulk count.
  const listedKeys = useMemo(() => new Set(rows.map((r) => r.key)), [rows])
  const chosen = [...selected].filter((k) => listedKeys.has(k))
  const selectable = visible.filter((r) => !r.provisional)
  const allVisibleSelected = selectable.length > 0 && selectable.every((r) => selected.has(r.key))
  const someVisibleSelected = !allVisibleSelected && selectable.some((r) => selected.has(r.key))
  const filtersActive =
    !!search.trim() || agentFilter !== ALL || projectFilter !== ALL || statusFilter !== ALL
  // Rows rise in once, when the list first arrives; never on a refetch. A
  // row that shows up later (created, moved in) rises in on its own.
  const arriving = useArrival(!sessions.isPending)
  const fresh = useFreshKeys(
    useMemo(() => rows.map((r) => r.key), [rows]),
    !arriving,
  )

  const toggle = (key: string, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (on) next.add(key)
      else next.delete(key)
      return next
    })
  const toggleAll = (on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev)
      for (const r of selectable) {
        if (on) next.add(r.key)
        else next.delete(r.key)
      }
      return next
    })
  const resetPage = () => setLimit(PAGE_SIZE)
  const clearFilters = () => {
    setSearch('')
    setAgentFilter(ALL)
    setProjectFilter(ALL)
    setStatusFilter(ALL)
    resetPage()
  }
  const onSort = (col: SortColumn) => {
    if (sortCol === col) setSortAsc((v) => !v)
    else {
      setSortCol(col)
      setSortAsc(col === 'key')
    }
  }
  const refresh = () => queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY })

  const runDelete = async (keys: string[]) => {
    setDeleting(true)
    setNotice(null)
    try {
      const res = await rpc.call<DeleteResult>('sessions.delete', buildDeleteParams(keys))
      // A question asked in a deleted session can no longer be answered there.
      const gone = Array.isArray(res?.deleted) ? res.deleted : []
      for (const key of gone) {
        if (typeof key === 'string') usePendingQuestions.getState().clearSession(key)
      }
      if (keys.length === 1) {
        const outcome = parseSingleDeleteResult(res, keys[0]!)
        if (outcome.ok) toast.success(t('webPagesWork.sessionsDeleted'))
        else
          setNotice({
            tone: 'danger',
            text: t('webPagesWork.sessionsDeleteFailed', { message: outcome.reason }),
          })
      } else {
        const { okCount, errCount } = parseBulkDeleteResult(res, keys.length)
        if (errCount > 0)
          setNotice({
            tone: 'warn',
            text: t('webPagesWork.sessionsDeletePartial', { ok: okCount, failed: errCount }),
          })
        else toast.success(tPlural('webPagesWork.sessionsDeletedMany', okCount))
      }
      setSelected(new Set())
    } catch (e) {
      setNotice({
        tone: 'danger',
        text: t('webPagesWork.sessionsDeleteFailed', { message: errorMessage(e) }),
      })
    } finally {
      setDeleting(false)
      setConfirm(null)
      void refresh()
    }
  }

  const empty = !sessions.isPending && !sessions.isError && rows.length === 0
  const newButton = (primary: boolean) => (
    <Button
      variant={primary ? 'primary' : 'outline'}
      disabled={act.disabled}
      aria-describedby={act.reason ? reasonId : undefined}
      onClick={() => setCreating(true)}
    >
      <Plus />
      {t('webPagesWork.sessionsNew')}
    </Button>
  )

  const header = (
    <PageHeader
      title={t('webPagesWork.sessionsTitle')}
      description={t('webPagesWork.sessionsSubtitle')}
      // The empty state below carries the one primary action.
      actions={newButton(!empty)}
      note={
        act.reason ? (
          <span id={reasonId} className="text-small text-text-muted">
            {act.reason}
          </span>
        ) : null
      }
      footer={rows.length > 0 ? <SessionsSummary stats={stats} totals={totals} /> : null}
    />
  )

  // Filter menus: every option says how many listed sessions it matches.
  const agentOptions = useMemo((): FilterOption[] => {
    const counts = new Map<string, number>()
    for (const id of agentsById.keys()) counts.set(id, 0)
    for (const row of rows) {
      const id = sessionAgent(row)
      if (id) counts.set(id, (counts.get(id) ?? 0) + 1)
    }
    return [
      { value: ALL, label: t('webPagesWork.filterAllAgents'), count: rows.length },
      ...[...counts.keys()].sort().map((id) => ({
        value: id,
        label: agentsById.get(id)?.name ?? id,
        count: counts.get(id),
      })),
    ]
  }, [agentsById, rows])
  const projectOptions = useMemo((): FilterOption[] => {
    const counts = new Map<string, number>()
    let none = 0
    for (const row of rows) {
      const id = sessionProject(row)
      if (id) counts.set(id, (counts.get(id) ?? 0) + 1)
      else none++
    }
    return [
      { value: ALL, label: t('webPagesWork.filterAllProjects'), count: rows.length },
      { value: 'none', label: t('webPagesWork.filterNoProject'), count: none },
      ...[...(projectNames ?? new Map<string, string>())].map(([id, name]) => ({
        value: id,
        label: name,
        count: counts.get(id) ?? 0,
      })),
    ]
  }, [projectNames, rows])
  const statusOptions = useMemo((): FilterOption[] => {
    const option = (value: StatusFilter, label: string, tone?: string): FilterOption => ({
      value,
      label,
      count: rows.filter((r) => matchesStatus(r, value)).length,
      mark: tone ? <StatusDot className={cn('size-2', tone)} /> : undefined,
    })
    return [
      option(ALL, t('webPagesWork.filterAllStatuses')),
      option('executing', t('webPagesWork.filterExecuting'), 'text-accent'),
      option('running', t('webPagesWork.lifecycleOpen'), 'text-ok'),
      option('done', t('webPagesWork.lifecycleDone'), 'text-info'),
      option('failed', t('webPagesWork.summaryFailedTitle'), 'text-danger'),
      option('killed', t('webPagesWork.lifecycleKilled'), 'text-text-muted'),
    ]
  }, [rows])

  let body: ReactNode
  if (sessions.isPending) {
    body = <SessionsSkeleton />
  } else if (rows.length === 0 && sessions.isError) {
    // The error notice above says what failed; there is nothing to show under it.
    body = null
  } else if (rows.length === 0) {
    body = <SessionsEmpty action={newButton(true)} />
  } else {
    body = (
      <>
        <div className="sx-toolbar">
          <Input
            type="search"
            aria-label={t('webPagesWork.sessionsSearch')}
            icon={<Search />}
            wrapperClassName="sx-search"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              resetPage()
            }}
            placeholder={t('webPagesWork.sessionsSearch')}
          />
          <div className="flex flex-wrap items-center gap-1.5">
            <FilterMenu
              label={t('webPagesWork.filterAgent')}
              icon={<Bot />}
              value={agentFilter}
              options={agentOptions}
              onChange={(v) => {
                setAgentFilter(v)
                resetPage()
              }}
            />
            <FilterMenu
              label={t('webPagesWork.filterProject')}
              icon={<FolderKanban />}
              value={projectFilter}
              options={projectOptions}
              onChange={(v) => {
                setProjectFilter(v)
                resetPage()
              }}
            />
            <FilterMenu
              label={t('webPagesWork.filterStatus')}
              icon={<Activity />}
              value={statusFilter}
              options={statusOptions}
              onChange={(v) => {
                setStatusFilter(v as StatusFilter)
                resetPage()
              }}
            />
            {filtersActive ? (
              <Button variant="ghost" size="sm" className="anim-fade-in" onClick={clearFilters}>
                <X />
                {t('webPagesWork.clearFilters')}
              </Button>
            ) : null}
          </div>
          <p className="sx-toolbar-meta">
            <span className="sx-summary-quiet">
              <MessageSquare aria-hidden />
              {tPlural('webPagesWork.summaryMessages', stats.totalMessages, {
                count: formatNumber(stats.totalMessages),
              })}
            </span>
            <span className="sx-summary-quiet">
              <Bot aria-hidden />
              {tPlural('webPagesWork.summaryAgents', stats.agents)}
            </span>
          </p>
        </div>

        {notice ? (
          <p
            role={notice.tone === 'ok' ? 'status' : 'alert'}
            className={cn(
              'anim-fade-in flex items-start gap-2 rounded-card border px-3 py-2',
              notice.tone === 'danger'
                ? 'border-danger/30 bg-danger/10'
                : 'border-warn/30 bg-warn/10',
            )}
          >
            <AlertTriangle
              className={cn(
                'mt-0.5 size-4 shrink-0',
                notice.tone === 'danger' ? 'text-danger' : 'text-warn',
              )}
              aria-hidden
              data-status-icon
            />
            <span className="flex-1">{notice.text}</span>
            <button
              type="button"
              aria-label={t('webPagesWork.dismiss')}
              onClick={() => setNotice(null)}
              className="press target inline-flex items-center justify-center rounded-md text-text-muted hover:text-text"
            >
              <X className="size-4" aria-hidden />
            </button>
          </p>
        ) : null}

        {filtered.length === 0 ? (
          <EmptyState
            size="sm"
            icon={<Search />}
            title={t('webPagesWork.sessionsNoMatch')}
            description={t('webPagesWork.sessionsNoMatchBody')}
            action={
              filtersActive ? (
                <Button variant="outline" onClick={clearFilters}>
                  {t('webPagesWork.clearFilters')}
                </Button>
              ) : undefined
            }
          />
        ) : (
          <div role="region" aria-label={t('webPagesWork.sessionsTable')} className="sx-list">
            <table className="sx-table">
              <thead>
                <tr>
                  <th scope="col" data-col="check">
                    <label className="sx-check-hit">
                      <input
                        type="checkbox"
                        aria-label={t('webPagesWork.selectAll')}
                        checked={allVisibleSelected}
                        ref={(el) => {
                          if (el) el.indeterminate = someVisibleSelected
                        }}
                        onChange={(e) => toggleAll(e.target.checked)}
                        className="sx-check"
                      />
                    </label>
                  </th>
                  <SortHeader
                    col="key"
                    label={t('webPagesWork.colSession')}
                    sortCol={sortCol}
                    asc={sortAsc}
                    onSort={onSort}
                  />
                  <th scope="col" data-col="context">
                    {t('webPagesWork.colContext')}
                  </th>
                  <th scope="col" data-col="status">
                    {t('webPagesWork.colStatus')}
                  </th>
                  <SortHeader
                    col="message_count"
                    label={t('webPagesWork.colMessages')}
                    sortCol={sortCol}
                    asc={sortAsc}
                    onSort={onSort}
                  />
                  <SortHeader
                    col="updated_at"
                    label={t('webPagesWork.colUpdated')}
                    sortCol={sortCol}
                    asc={sortAsc}
                    onSort={onSort}
                  />
                  <th scope="col" data-col="actions">
                    <span className="sr-only">{t('webPagesWork.colActions')}</span>
                  </th>
                </tr>
              </thead>
              <tbody className={cn(arriving && 'anim-stagger')}>
                {visible.map((row) => (
                  <SessionTableRow
                    key={row.key}
                    row={row}
                    fresh={fresh.has(row.key)}
                    selected={selected.has(row.key)}
                    onSelect={(on) => toggle(row.key, on)}
                    agentsById={agentsById}
                    agentsLoaded={agents.isSuccess}
                    projectNames={projectNames}
                    act={act}
                    onMove={() => setMoveKeys([row.key])}
                    onDelete={() => setConfirm({ keys: [row.key] })}
                  />
                ))}
              </tbody>
            </table>
            {filtered.length > visible.length ? (
              <div className="sx-more">
                <span className="text-small text-text-muted tabular-nums">
                  {t('webPagesWork.showingOf', { shown: visible.length, total: filtered.length })}
                </span>
                <Button variant="outline" size="sm" onClick={() => setLimit((n) => n + PAGE_SIZE)}>
                  <ChevronDown />
                  {t('webPagesWork.showMore')}
                </Button>
              </div>
            ) : null}
          </div>
        )}

        {chosen.length > 0 ? (
          <div
            role="region"
            aria-label={t('webPagesWork.bulkRegion')}
            className="sx-bulk anim-enter"
          >
            <span className="flex min-w-0 items-center gap-1 max-sm:w-full">
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={t('webPagesWork.bulkClear')}
                title={t('webPagesWork.bulkClear')}
                onClick={() => setSelected(new Set())}
              >
                <X />
              </Button>
              <span key={chosen.length} aria-hidden className="sx-bulk-count anim-scale-in">
                {chosen.length}
              </span>
              <span className="pr-2 pl-1 font-medium whitespace-nowrap">
                {tPlural('webPagesWork.bulkSelected', chosen.length)}
              </span>
            </span>
            <span className="flex items-center gap-1.5 max-sm:w-full max-sm:[&>*]:flex-1">
              <Button
                size="sm"
                variant="outline"
                disabled={act.disabled}
                title={act.reason ?? undefined}
                onClick={() => setMoveKeys(chosen)}
              >
                <FolderInput />
                {t('webPagesWork.bulkMove')}
              </Button>
              <Button
                size="sm"
                variant="danger"
                disabled={act.disabled || deleting}
                title={act.reason ?? undefined}
                onClick={() => setConfirm({ keys: chosen })}
              >
                <Trash2 />
                {t('webPagesWork.bulkDelete')}
              </Button>
            </span>
          </div>
        ) : null}
      </>
    )
  }

  return (
    <div className={PAGE_CLASS}>
      {header}
      {sessions.isError ? (
        <ErrorState
          title={t('webPagesWork.sessionsLoadFailed')}
          message={errorMessage(sessions.error)}
          onRetry={() => void sessions.refetch()}
          retrying={sessions.isFetching}
        />
      ) : null}
      {totals.truncated && rows.length > 0 ? (
        <p
          role="note"
          className="flex items-start gap-2 rounded-card border border-warn/30 bg-warn/10 px-3 py-2 text-small"
        >
          <AlertTriangle className="mt-px size-4 shrink-0 text-warn" aria-hidden data-status-icon />
          {totals.total !== null
            ? t('webPagesWork.sessionsTruncated', {
                shown: formatNumber(totals.listed),
                total: formatNumber(totals.total),
              })
            : t('webPagesWork.sessionsTruncatedUnknown', { shown: formatNumber(totals.listed) })}
        </p>
      ) : null}
      {body}

      <AlertDialog open={confirm !== null} onOpenChange={(open) => !open && setConfirm(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {tPlural('webPagesWork.deleteSessionsTitle', confirm?.keys.length ?? 1)}
            </AlertDialogTitle>
            <AlertDialogDescription>{t('webPagesWork.deleteSessionsBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webPagesWork.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              destructive
              disabled={deleting}
              onClick={(e) => {
                e.preventDefault()
                if (confirm) void runDelete(confirm.keys)
              }}
            >
              {deleting ? <Loader2 className="animate-spin" /> : null}
              {t('webPagesWork.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {moveKeys ? (
        <MoveDialog
          keys={moveKeys}
          projectNames={projectNames}
          current={
            moveKeys.length === 1
              ? (sessionProject(rows.find((r) => r.key === moveKeys[0]) ?? { key: '' }) ?? '')
              : ''
          }
          act={act}
          onClose={(moved) => {
            setMoveKeys(null)
            if (moved) setSelected(new Set())
          }}
        />
      ) : null}

      {creating ? (
        <NewSessionDialog
          agents={agents.data ?? []}
          projectNames={projectNames}
          connection={connection}
          onClose={() => setCreating(false)}
        />
      ) : null}
    </div>
  )
}

// ── Summary line ─────────────────────────────────────────────────────────────

/** The count of stored sessions: the true total, or "500+" when the gateway did not say. */
function sessionsCountText(totals: SessionsTotal): string {
  if (totals.total !== null) return formatNumber(totals.total)
  return t('webPagesWork.atLeast', { count: formatNumber(totals.listed) })
}

/**
 * One slim line under the title: the total, a proportion bar of the
 * lifecycle mix, a chip per state, what is executing right now (live), then
 * messages and agents, quieter.
 */
function SessionsSummary({
  stats,
  totals,
}: {
  stats: ReturnType<typeof sessionStats>
  totals: SessionsTotal
}) {
  const live = stats.activeRuns > 0
  const parts = [
    {
      key: 'open',
      n: stats.lifecycleOpen,
      tone: 'text-ok',
      text: (count: string) => t('webPagesWork.summaryOpen', { count }),
    },
    {
      key: 'done',
      n: stats.done,
      tone: 'text-info',
      text: (count: string) => t('webPagesWork.summaryCompleted', { count }),
    },
    {
      key: 'failed',
      n: stats.failedOrTimedOut,
      tone: 'text-danger',
      text: (count: string) => t('webPagesWork.summaryFailed', { count }),
    },
    {
      key: 'aborted',
      n: stats.aborted,
      tone: 'text-text-muted',
      text: (count: string) => t('webPagesWork.summaryAborted', { count }),
    },
  ] as const
  const count = totals.total ?? totals.listed
  return (
    <ul aria-label={t('webPagesWork.summaryLabel')} className="sx-summary">
      <li className="sx-summary-total">
        {tPlural('webPagesWork.summarySessions', count, { count: sessionsCountText(totals) })}

        {parts.some((p) => p.n > 0) ? (
          <span aria-hidden className="sx-meter" title={t('webPagesWork.summaryMeter')}>
            {parts
              .filter((p) => p.n > 0)
              .map((p) => (
                <span key={p.key} className={p.tone} style={{ flexGrow: p.n }} />
              ))}
          </span>
        ) : null}
      </li>
      {parts.map((p) => (
        <li
          key={p.key}
          className="sx-summary-chip"
          data-zero={p.n === 0 || undefined}
          title={p.key === 'failed' ? t('webPagesWork.summaryFailedTitle') : undefined}
        >
          <StatusDot className={cn('size-1.5', p.tone)} />
          {p.text(formatNumber(p.n))}
        </li>
      ))}
      <li className="sx-summary-chip" data-live={live || undefined}>
        <StatusDot tone={live ? 'accent' : 'neutral'} live={live} className="size-1.5" />
        <span role="status">
          {live
            ? tPlural('webPagesWork.executingNow', stats.activeRuns)
            : t('webPagesWork.executingNone')}
        </span>
      </li>
    </ul>
  )
}

// ── Empty and loading ────────────────────────────────────────────────────────

/** Ghost rows: what the list becomes, one per kind of source. */
const GHOSTS = [
  { icon: MessageCircle, title: '62%', key: '78%', hue: 'webchat' },
  { icon: Send, title: '48%', key: '64%', hue: 'telegram' },
  { icon: Hash, title: '56%', key: '70%', hue: 'slack' },
  { icon: Clock, title: '40%', key: '58%', hue: 'cron' },
]

function SessionsEmpty({ action }: { action: ReactNode }) {
  const titleId = useId()
  return (
    <section aria-labelledby={titleId} className="sx-empty anim-enter">
      <div aria-hidden className="sx-ghost">
        {GHOSTS.map((g) => (
          <div key={g.hue} className="sx-ghost-row">
            <span className="sx-tile pj-hue" data-tone="soft" data-hue={hueOf(g.hue)}>
              <g.icon />
            </span>
            <span className="sx-ghost-lines">
              <span style={{ width: g.title }} />
              <span style={{ width: g.key }} />
            </span>
            <span className="sx-ghost-chip" />
            <span className="sx-ghost-badge" />
            <span className="sx-ghost-time" />
          </div>
        ))}
      </div>
      <div className="sx-empty-copy">
        <span aria-hidden className="sx-empty-icon">
          <MessagesSquare />
        </span>
        <h2 id={titleId} className="text-section">
          {t('webPagesWork.sessionsEmpty')}
        </h2>
        <p className="max-w-md text-pretty text-text-muted">
          {t('webPagesWork.sessionsEmptyBody')}
        </p>
        <div className="mt-2">{action}</div>
      </div>
    </section>
  )
}

function SessionsSkeleton() {
  return (
    <LoadingRegion label={t('webPagesWork.sessionsLoading')} className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        <Skeleton className="h-9 flex-[1_1_14rem] sm:max-w-xs" />
        <Skeleton className="h-8 w-20 rounded-full" />
        <Skeleton className="h-8 w-24 rounded-full" />
        <Skeleton className="h-8 w-20 rounded-full" />
      </div>
      <div className="flex flex-col">
        {Array.from({ length: 6 }, (_, i) => (
          <div key={i} className="flex items-center gap-3 border-b border-border px-2 py-3">
            <Skeleton className="size-4 shrink-0" />
            <Skeleton className="size-9 shrink-0 rounded-control" />
            <div className="flex min-w-0 flex-1 flex-col gap-1.5">
              <Skeleton className="h-4 w-40 max-w-full" />
              <Skeleton className="h-3 w-56 max-w-full" />
            </div>
            <Skeleton className="h-5 w-24 rounded-full max-sm:hidden" />
            <Skeleton className="h-5 w-20 rounded-full max-sm:hidden" />
            <Skeleton className="h-4 w-12 max-sm:hidden" />
          </div>
        ))}
      </div>
    </LoadingRegion>
  )
}

// ── Table pieces ─────────────────────────────────────────────────────────────

const LIFECYCLE_LABEL: Record<string, () => string> = {
  running: () => t('webPagesWork.lifecycleOpen'),
  done: () => t('webPagesWork.lifecycleDone'),
  failed: () => t('webPagesWork.lifecycleFailed'),
  killed: () => t('webPagesWork.lifecycleKilled'),
  timeout: () => t('webPagesWork.lifecycleTimeout'),
}

const BADGE_TONE: Record<Tone, 'ok' | 'warn' | 'danger' | 'info' | 'neutral'> = {
  ok: 'ok',
  warn: 'warn',
  danger: 'danger',
  info: 'info',
  dim: 'neutral',
}

/** The lifecycle badge (open / completed / failed / aborted / timed out) of a row. */
function LifecycleChip({ row, live }: { row: SessionRow; live: boolean }) {
  const visual = sessionVisualStatus(row)
  const label = LIFECYCLE_LABEL[visual]?.() ?? t('webPagesWork.lifecycleUnknown')
  return (
    <Badge
      data-lifecycle={visual}
      tone={live ? 'accent' : BADGE_TONE[sessionStatusChip(visual)]}
      dot={!live}
      live={live}
    >
      {label}
    </Badge>
  )
}

function SortHeader({
  col,
  label,
  sortCol,
  asc,
  onSort,
}: {
  col: SortColumn
  label: string
  sortCol: SortColumn
  asc: boolean
  onSort: (col: SortColumn) => void
}) {
  const active = sortCol === col
  const dataCol = col === 'key' ? 'session' : col === 'message_count' ? 'messages' : 'updated'
  return (
    <th
      scope="col"
      data-col={dataCol}
      aria-sort={active ? (asc ? 'ascending' : 'descending') : 'none'}
    >
      <button
        type="button"
        onClick={() => onSort(col)}
        data-active={active || undefined}
        className="sx-sort press group/sort target"
      >
        {label}
        <ArrowDown
          aria-hidden
          className={cn(
            'size-3.5 transition-[rotate,opacity] duration-[var(--duration-base)] ease-[var(--ease-out)]',
            asc && active && 'rotate-180',
            active ? 'opacity-100' : 'opacity-0 group-hover/sort:opacity-50',
          )}
        />
      </button>
    </th>
  )
}

/** The channel the session came from, as a tinted tile; a live ring while it runs. */
function SessionTile({ row, live }: { row: SessionRow; live: boolean }) {
  const channel = sessionChannel(row)
  return (
    <span
      className="sx-tile pj-hue"
      data-tone="soft"
      data-hue={hueOf(channel)}
      data-live={live || undefined}
    >
      <ChannelIcon channel={channel} className="flex" />
    </span>
  )
}

// ── One row ──────────────────────────────────────────────────────────────────

function SessionTableRow({
  row,
  fresh,
  selected,
  onSelect,
  agentsById,
  agentsLoaded,
  projectNames,
  act,
  onMove,
  onDelete,
}: {
  row: SessionRow
  fresh: boolean
  selected: boolean
  onSelect: (on: boolean) => void
  agentsById: Map<string, { id: string; name: string }>
  agentsLoaded: boolean
  projectNames?: ReadonlyMap<string, string>
  act: Gate
  onMove: () => void
  onDelete: () => void
}) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const title = sessionTitle(row)
  const tabHref = useHref(sessionKeyToPath(row.key))
  const [renaming, setRenaming] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Bumped on a successful rename: remounts the title so it flashes once.
  const [renamedAt, setRenamedAt] = useState(0)
  const errorId = useId()

  const agentId = sessionAgent(row)
  const subline = agentSubline(agentId, agentsById, agentsLoaded)
  const project = sessionProject(row)
  const badge = runStatusBadge(row)
  const runStatus = sessionRunStatus(row)
  const executing = runStatus === 'queued' || runStatus === 'running'
  const open = sessionVisualStatus(row) === 'running'

  const startRename = () => {
    setDraft(sessionName(row))
    setError(null)
    setRenaming(true)
  }
  const cancelRename = () => {
    setRenaming(false)
    setError(null)
  }
  const submitRename = async () => {
    const name = draft.trim()
    if (name === sessionName(row).trim()) {
      setRenaming(false)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await rpc.call('sessions.rename', { key: row.key, name })
      queryClient.setQueryData<SessionRow[]>(SESSIONS_QUERY_KEY, (prev) =>
        prev?.map((r) => (r.key === row.key ? { ...r, display_name: name, displayName: name } : r)),
      )
      setRenaming(false)
      setRenamedAt(Date.now())
      void queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY })
    } catch (e) {
      setError(t('webPagesWork.renameFailed', { message: errorMessage(e) }))
    } finally {
      setBusy(false)
    }
  }

  return (
    <tr
      data-session-key={row.key}
      data-selected={selected || undefined}
      className={cn('sx-row', fresh && 'anim-enter')}
    >
      <td data-col="check">
        {row.provisional ? null : (
          <label className="sx-check-hit">
            <input
              type="checkbox"
              aria-label={t('webPagesWork.selectRow', { title })}
              checked={selected}
              onChange={(e) => onSelect(e.target.checked)}
              className="sx-check"
            />
          </label>
        )}
      </td>
      <td data-col="session">
        <div className="flex min-w-0 items-center gap-3">
          <SessionTile row={row} live={executing} />
          <div className="min-w-0 flex-1">
            {renaming ? (
              <form
                className="anim-fade-in flex items-center gap-1.5"
                onSubmit={(e) => {
                  e.preventDefault()
                  void submitRename()
                }}
              >
                <Input
                  size="sm"
                  autoFocus
                  aria-label={t('webPagesWork.renameLabel')}
                  placeholder={t('webPagesWork.renamePlaceholder')}
                  value={draft}
                  disabled={busy}
                  onChange={(e) => setDraft(e.target.value)}
                  onFocus={(e) => e.currentTarget.select()}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') {
                      e.stopPropagation()
                      cancelRename()
                    }
                  }}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? errorId : undefined}
                  className="flex-1"
                />
                <Button size="sm" variant="primary" type="submit" disabled={busy || act.disabled}>
                  {busy ? <Loader2 className="animate-spin" /> : null}
                  {t('webPagesWork.save')}
                </Button>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label={t('webPagesWork.cancel')}
                  disabled={busy}
                  onClick={cancelRename}
                >
                  <X />
                </Button>
              </form>
            ) : (
              <div className="flex min-w-0 items-center gap-1">
                <Link
                  key={renamedAt}
                  to={sessionKeyToPath(row.key)}
                  viewTransition
                  className={cn('sx-title', renamedAt ? 'anim-flash' : null)}
                >
                  <span className="truncate">{title}</span>
                </Link>
                {row.provisional ? (
                  <Badge tone="neutral" className="ml-1">
                    <Loader2 className="animate-spin" aria-hidden />
                    {t('webPagesWork.provisional')}
                  </Badge>
                ) : (
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    className="sx-reveal shrink-0 text-text-muted"
                    aria-label={t('webPagesWork.renameRow', { title })}
                    disabled={act.disabled}
                    title={act.reason ?? undefined}
                    onClick={startRename}
                  >
                    <Pencil />
                  </Button>
                )}
              </div>
            )}
            <div className="sx-key" title={row.key}>
              {row.key}
            </div>
            {error ? (
              <p
                id={errorId}
                role="alert"
                className="anim-fade-in mt-1 flex items-center gap-1 text-small text-danger"
              >
                <AlertTriangle className="size-3.5 shrink-0" aria-hidden data-status-icon />
                {error}
              </p>
            ) : null}
          </div>
        </div>
      </td>
      <td data-col="context">
        <span className="flex min-w-0 flex-wrap items-center gap-1.5">
          <span className="sx-tag">
            <Bot aria-hidden />
            <span className="truncate">
              {agentsById.get(agentId)?.name ?? (agentId || t('webPagesWork.dash'))}
            </span>
          </span>
          {subline?.orphan ? (
            <Badge tone="warn" title={t('webPagesWork.orphanTitle', { agent: agentId })}>
              <AlertTriangle aria-hidden data-status-icon />
              {t('webPagesWork.orphan')}
            </Badge>
          ) : null}
          {project ? (
            <span className="sx-tag">
              <FolderKanban aria-hidden />
              <span className="truncate">{projectNames?.get(project) ?? project}</span>
            </span>
          ) : null}
        </span>
      </td>
      <td data-col="status">
        <div className="flex flex-wrap items-center gap-1.5">
          <LifecycleChip row={row} live={executing} />
          {executing && badge ? (
            <span className="text-small font-medium text-accent">{badge.label}</span>
          ) : open ? (
            <span className="text-small text-text-muted">{t('webPagesWork.statusIdle')}</span>
          ) : null}
        </div>
      </td>
      <td data-col="messages">
        <span className="inline-flex items-center gap-1.5">
          <MessageSquare className="size-3.5 text-text-muted" aria-hidden />
          {formatNumber(Number(row.message_count) || 0)}
        </span>
      </td>
      <td data-col="updated">{relTimeLabel(row.updated_at ?? row.updatedAt ?? '')}</td>
      <td data-col="actions">
        {row.provisional ? null : (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                size="icon-sm"
                variant="ghost"
                className="text-text-muted data-[state=open]:bg-surface-3 data-[state=open]:text-text"
                aria-label={t('webPagesWork.rowActions', { title })}
              >
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-48">
              <DropdownMenuItem asChild>
                <Link to={sessionKeyToPath(row.key)} viewTransition>
                  <MessageSquare aria-hidden />
                  {t('webPagesWork.openChat')}
                </Link>
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => openInNewTab(tabHref)}>
                <ExternalLink aria-hidden />
                {t('webPagesWork.openNewTab')}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem disabled={act.disabled} onSelect={startRename}>
                <Pencil aria-hidden />
                {t('webPagesWork.rename')}
              </DropdownMenuItem>
              <DropdownMenuItem disabled={act.disabled} onSelect={onMove}>
                <FolderInput aria-hidden />
                {t('webPagesWork.moveToProject')}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem variant="danger" disabled={act.disabled} onSelect={onDelete}>
                <Trash2 aria-hidden />
                {t('webPagesWork.delete')}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </td>
    </tr>
  )
}
