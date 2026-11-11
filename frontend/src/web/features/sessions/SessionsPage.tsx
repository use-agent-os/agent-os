import { useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  Bot,
  ChevronDown,
  ExternalLink,
  FolderInput,
  FolderKanban,
  Loader2,
  MessageSquare,
  MessagesSquare,
  MoreHorizontal,
  Pencil,
  Plus,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import { useId, useMemo, useState, type ReactNode } from 'react'
import { Link, useHref, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { formatNumber, t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { blockedBecause, gate, requireConnection, type Gate } from '@/lib/disabled-reason'
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
import { Card } from '@/web/components/ui/card'
import { Input, Select } from '@/web/components/ui/input'
import { PageHeader } from '@/web/components/ui/page-header'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'
import { EmptyState, ErrorState, Skeleton } from '@/web/components/ui/states'
import {
  AGENTS_QUERY_KEY,
  createSessionOptimistic,
  PROJECTS_QUERY_KEY,
  SESSIONS_QUERY_KEY,
  useAgents,
  useProjectNames,
  useSessionsList,
} from '@/web/shell/session-data'
import {
  sessionAgent,
  sessionProject,
  sessionTitle,
  type SessionRow,
} from '@/web/shell/session-list'
import { openInNewTab } from '@/web/shell/SessionList'
import { sessionKeyToPath } from '@/web/shell/session-url'
import type { RpcState } from '@/lib/ws-rpc'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { StatStrip, useArrival, WorkSkeleton, type StripItem } from './StatStrip'
import { useSessionsTotal, type SessionsTotal } from './sessions-total'

// `/sessions` (task 8.2): every session in one table, on views/sessions/logic.
// Search, agent and project filters, stat tiles, inline rename, bulk delete,
// move to project, a new-session dialog that can create an agent, and a flag on
// rows whose agent is no longer registered.

const PAGE_SIZE = 50
const NEW_AGENT = '__new__'
const AGENT_ID = /^[a-z0-9_-]+$/

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function errorCode(error: unknown): string {
  return (error as { code?: string } | null)?.code ?? ''
}

type Confirm = { keys: string[] } | null
type Notice = { tone: 'ok' | 'warn' | 'danger'; text: string } | null

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
  const [agentFilter, setAgentFilter] = useState('')
  const [projectFilter, setProjectFilter] = useState('all')
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
  const agentOptions = useMemo(() => {
    const ids = new Set(agentsById.keys())
    for (const row of rows) {
      const id = sessionAgent(row)
      if (id) ids.add(id)
    }
    return [...ids].sort()
  }, [agentsById, rows])

  const filtered = useMemo(() => {
    const byAgent = agentFilter ? rows.filter((r) => sessionAgent(r) === agentFilter) : rows
    const byProject = filterSessionsByProject(byAgent, projectFilter) as SessionRow[]
    return sortSessions(filterSessions(byProject, search), sortCol, sortAsc) as SessionRow[]
  }, [rows, agentFilter, projectFilter, search, sortCol, sortAsc])
  const visible = filtered.slice(0, limit)
  const stats = useMemo(() => sessionStats(rows), [rows])
  const totals = useSessionsTotal(sessions.data?.length ?? 0)

  // Selection only ever holds keys that are still listed: a row deleted
  // elsewhere drops out instead of lingering in the bulk count.
  const listedKeys = useMemo(() => new Set(rows.map((r) => r.key)), [rows])
  const chosen = [...selected].filter((k) => listedKeys.has(k))
  const selectable = visible.filter((r) => !r.provisional)
  const allVisibleSelected = selectable.length > 0 && selectable.every((r) => selected.has(r.key))
  const filtersActive = !!search.trim() || !!agentFilter || projectFilter !== 'all'
  // Rows rise in once, when the list first arrives; never on a refetch.
  const arriving = useArrival(!sessions.isPending)

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
  const clearFilters = () => {
    setSearch('')
    setAgentFilter('')
    setProjectFilter('all')
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

  const header = (
    <PageHeader
      title={t('webPagesWork.sessionsTitle')}
      description={t('webPagesWork.sessionsSubtitle')}
      actions={
        <Button
          variant="primary"
          disabled={act.disabled}
          aria-describedby={act.reason ? reasonId : undefined}
          onClick={() => setCreating(true)}
        >
          <Plus />
          {t('webPagesWork.sessionsNew')}
        </Button>
      }
      note={
        act.reason ? (
          <span id={reasonId} className="text-small text-text-muted">
            {act.reason}
          </span>
        ) : null
      }
    />
  )

  let body: ReactNode
  if (sessions.isPending) {
    body = <SessionsSkeleton />
  } else if (rows.length === 0 && sessions.isError) {
    // The error notice above says what failed; there is nothing to show under it.
    body = null
  } else if (rows.length === 0) {
    body = (
      <Card variant="plain" padding="none">
        <EmptyState
          icon={<MessagesSquare />}
          title={t('webPagesWork.sessionsEmpty')}
          description={t('webPagesWork.sessionsEmptyBody')}
          action={
            <Button variant="primary" disabled={act.disabled} onClick={() => setCreating(true)}>
              <Plus />
              {t('webPagesWork.sessionsNew')}
            </Button>
          }
        />
      </Card>
    )
  } else {
    body = (
      <>
        <StatTiles stats={stats} totals={totals} />
        <div className="flex flex-wrap items-center gap-2">
          <Input
            type="search"
            aria-label={t('webPagesWork.sessionsSearch')}
            icon={<Search />}
            wrapperClassName="flex-[2_1_16rem]"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setLimit(PAGE_SIZE)
            }}
            placeholder={t('webPagesWork.sessionsSearch')}
          />
          <label className="flex min-w-0 flex-[1_1_11rem] items-center gap-2">
            <span className="shrink-0 text-label text-text-muted max-sm:w-12">
              {t('webPagesWork.filterAgent')}
            </span>
            <Select value={agentFilter} onChange={(e) => setAgentFilter(e.target.value)}>
              <option value="">{t('webPagesWork.filterAllAgents')}</option>
              {agentOptions.map((id) => (
                <option key={id} value={id}>
                  {agentsById.get(id)?.name ?? id}
                </option>
              ))}
            </Select>
          </label>
          <label className="flex min-w-0 flex-[1_1_11rem] items-center gap-2">
            <span className="shrink-0 text-label text-text-muted max-sm:w-12">
              {t('webPagesWork.filterProject')}
            </span>
            <Select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)}>
              <option value="all">{t('webPagesWork.filterAllProjects')}</option>
              <option value="none">{t('webPagesWork.filterNoProject')}</option>
              {[...(projectNames ?? new Map<string, string>())].map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </Select>
          </label>
          {filtersActive ? (
            <Button variant="ghost" size="sm" className="anim-fade-in" onClick={clearFilters}>
              <X />
              {t('webPagesWork.clearFilters')}
            </Button>
          ) : null}
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
          <Card variant="plain" padding="none">
            <EmptyState
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
          </Card>
        ) : (
          <Card
            variant="plain"
            padding="none"
            role="region"
            aria-label={t('webPagesWork.sessionsTable')}
            className="work-table-wrap overflow-clip"
          >
            <table className="work-table">
              <thead className="text-label text-text-muted">
                <tr>
                  <th scope="col" data-col="check" className="w-11 py-2.5 pr-1 pl-4">
                    <input
                      type="checkbox"
                      aria-label={t('webPagesWork.selectAll')}
                      checked={allVisibleSelected}
                      onChange={(e) => toggleAll(e.target.checked)}
                      className="work-check"
                    />
                  </th>
                  <SortHeader
                    col="key"
                    label={t('webPagesWork.colSession')}
                    sortCol={sortCol}
                    asc={sortAsc}
                    onSort={onSort}
                  />
                  <th scope="col" data-col="agent" className="px-3 py-2.5 font-medium">
                    {t('webPagesWork.colAgent')}
                  </th>
                  <th scope="col" data-col="project" className="px-3 py-2.5 font-medium">
                    {t('webPagesWork.colProject')}
                  </th>
                  <th scope="col" data-col="status" className="px-3 py-2.5 font-medium">
                    {t('webPagesWork.colStatus')}
                  </th>
                  <SortHeader
                    col="message_count"
                    label={t('webPagesWork.colMessages')}
                    sortCol={sortCol}
                    asc={sortAsc}
                    onSort={onSort}
                    numeric
                  />
                  <SortHeader
                    col="updated_at"
                    label={t('webPagesWork.colUpdated')}
                    sortCol={sortCol}
                    asc={sortAsc}
                    onSort={onSort}
                  />
                  <th scope="col" data-col="actions" className="w-12 py-2.5 pr-3">
                    <span className="sr-only">{t('webPagesWork.colActions')}</span>
                  </th>
                </tr>
              </thead>
              <tbody className={cn(arriving && 'anim-stagger')}>
                {visible.map((row) => (
                  <SessionTableRow
                    key={row.key}
                    row={row}
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
              <div className="flex flex-wrap items-center justify-center gap-3 border-t border-border bg-surface-2/50 px-4 py-3">
                <span className="text-small text-text-muted tabular-nums">
                  {t('webPagesWork.showingOf', { shown: visible.length, total: filtered.length })}
                </span>
                <Button variant="outline" size="sm" onClick={() => setLimit((n) => n + PAGE_SIZE)}>
                  <ChevronDown />
                  {t('webPagesWork.showMore')}
                </Button>
              </div>
            ) : null}
          </Card>
        )}

        {chosen.length > 0 ? (
          <div
            role="region"
            aria-label={t('webPagesWork.bulkRegion')}
            className="anim-enter sticky bottom-4 z-[var(--z-sticky)] mx-auto flex w-fit max-w-full flex-wrap items-center gap-x-2 gap-y-1.5 rounded-overlay border border-border-strong bg-surface-overlay p-1.5 shadow-overlay"
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
              <span
                aria-hidden
                className="grid h-5 min-w-5 place-items-center rounded-full bg-accent px-1.5 text-small font-semibold text-accent-fg tabular-nums"
              >
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
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 p-4 md:p-6">
      {header}
      {sessions.isError ? (
        <ErrorState
          title={t('webPagesWork.sessionsLoadFailed')}
          message={errorMessage(sessions.error)}
          onRetry={() => void sessions.refetch()}
          retrying={sessions.isFetching}
        />
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

// ── Loading shape ────────────────────────────────────────────────────────────

function SessionsSkeleton() {
  return (
    <WorkSkeleton label={t('webPagesWork.sessionsLoading')} cells={5} wide>
      <div className="flex flex-wrap gap-2">
        <Skeleton className="h-9 flex-[2_1_16rem]" />
        <Skeleton className="h-9 flex-[1_1_11rem]" />
        <Skeleton className="h-9 flex-[1_1_11rem]" />
      </div>
      <Card variant="plain" padding="none" className="overflow-hidden">
        <div className="h-10 border-b border-border bg-surface-2" />
        {Array.from({ length: 6 }, (_, i) => (
          <div key={i} className="flex items-center gap-4 border-b border-border px-4 py-3.5">
            <Skeleton className="size-4 shrink-0" />
            <div className="flex min-w-0 flex-1 flex-col gap-1.5">
              <Skeleton className="h-4 w-40 max-w-full" />
              <Skeleton className="h-3 w-56 max-w-full" />
            </div>
            <Skeleton className="h-5 w-20 max-sm:hidden" />
            <Skeleton className="h-4 w-12 max-sm:hidden" />
          </div>
        ))}
      </Card>
    </WorkSkeleton>
  )
}

// ── Stat tiles ───────────────────────────────────────────────────────────────

/** The count of stored sessions: the true total, or "500+" when the gateway did not say. */
function sessionsCountText(totals: SessionsTotal): string {
  if (totals.total !== null) return formatNumber(totals.total)
  return t('webPagesWork.atLeast', { count: formatNumber(totals.listed) })
}

function StatTiles({
  stats,
  totals,
}: {
  stats: ReturnType<typeof sessionStats>
  totals: SessionsTotal
}) {
  const live = stats.activeRuns > 0
  // Open, completed, failed or timed out, aborted: the same order as the hint.
  const parts = [
    { n: stats.lifecycleOpen, tone: 'text-ok' },
    { n: stats.done, tone: 'text-info' },
    { n: stats.failedOrTimedOut, tone: 'text-danger' },
    { n: stats.aborted, tone: 'text-text-muted' },
  ]
  const hint = t('webPagesWork.statSessionsHint', {
    open: stats.lifecycleOpen,
    done: stats.done,
    failed: stats.failedOrTimedOut,
    aborted: stats.aborted,
  })
  // The hint becomes a legend keyed to the meter. The separators stay in the
  // text (screen readers and copy read the sentence); a catalog that words it
  // differently just shows as one plain line.
  const legend = hint.split(' · ')
  const items: StripItem[] = [
    {
      label: t('webPagesWork.statSessions'),
      icon: <MessagesSquare />,
      value: sessionsCountText(totals),
      wide: true,
      detail: (
        <span className="flex flex-col gap-2">
          {parts.some((part) => part.n > 0) ? (
            <span className="work-meter" aria-hidden>
              {parts
                .filter((part) => part.n > 0)
                .map((part) => (
                  <span key={part.tone} className={part.tone} style={{ flexGrow: part.n }} />
                ))}
            </span>
          ) : null}
          <span className="flex flex-wrap gap-x-3 gap-y-0.5">
            {legend.map((text, i) => (
              <span key={i} className="inline-flex items-center gap-1.5 whitespace-nowrap">
                {legend.length === parts.length ? (
                  <StatusDot className={cn('size-1.5', parts[i]!.tone)} />
                ) : null}
                {text}
                {i < legend.length - 1 ? <span className="sr-only"> · </span> : null}
              </span>
            ))}
          </span>
        </span>
      ),
    },
    {
      label: t('webPagesWork.statRunning'),
      icon: <Activity />,
      value: formatNumber(stats.activeRuns),
      tone: live ? 'accent' : 'default',
      detail: (
        <span role="status" className="flex min-w-0 items-center gap-1.5">
          <StatusDot tone={live ? 'accent' : 'neutral'} live={live} className="size-1.5" />
          <span className="truncate">
            {live
              ? tPlural('webPagesWork.executingNow', stats.activeRuns)
              : t('webPagesWork.executingNone')}
          </span>
        </span>
      ),
    },
    {
      label: t('webPagesWork.statFailed'),
      icon: <AlertTriangle />,
      value: formatNumber(stats.failedOrTimedOut),
      tone: stats.failedOrTimedOut > 0 ? 'danger' : 'default',
    },
    {
      label: t('webPagesWork.statMessages'),
      icon: <MessageSquare />,
      value: formatNumber(stats.totalMessages),
    },
    { label: t('webPagesWork.statAgents'), icon: <Bot />, value: formatNumber(stats.agents) },
  ]
  return (
    <div className="flex flex-col gap-2">
      {totals.truncated ? (
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
      <StatStrip items={items} />
    </div>
  )
}

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
  numeric = false,
}: {
  col: SortColumn
  label: string
  sortCol: SortColumn
  asc: boolean
  onSort: (col: SortColumn) => void
  numeric?: boolean
}) {
  const active = sortCol === col
  const dataCol = col === 'key' ? 'session' : col === 'message_count' ? 'messages' : 'updated'
  return (
    <th
      scope="col"
      data-col={dataCol}
      aria-sort={active ? (asc ? 'ascending' : 'descending') : 'none'}
      className={cn('px-3 py-2.5 font-medium', numeric && 'text-right')}
    >
      <button
        type="button"
        onClick={() => onSort(col)}
        className={cn(
          'press group/sort target -mx-1 inline-flex items-center gap-1 rounded-md px-1 hover:text-text',
          active && 'text-text',
        )}
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

// ── One table row ────────────────────────────────────────────────────────────

function SessionTableRow({
  row,
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
      className={cn(
        'align-middle transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)]',
        selected ? 'bg-accent-tint' : 'hover:bg-surface-2',
      )}
    >
      <td data-col="check" className="py-3 pr-1 pl-4">
        {row.provisional ? null : (
          <input
            type="checkbox"
            aria-label={t('webPagesWork.selectRow', { title })}
            checked={selected}
            onChange={(e) => onSelect(e.target.checked)}
            className="work-check"
          />
        )}
      </td>
      <td data-col="session" className="max-w-[24rem] min-w-0 px-3 py-2.5">
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
              className={cn(
                'flex min-h-6 min-w-0 items-center rounded-sm font-medium hover:underline hover:underline-offset-2 touch:min-h-11',
                renamedAt ? 'anim-flash' : null,
              )}
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
                className="work-reveal shrink-0 text-text-muted"
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
        <div className="truncate font-mono text-small text-text-muted" title={row.key}>
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
      </td>
      <td data-col="agent" className="px-3 py-2.5">
        <span className="inline-flex min-w-0 flex-wrap items-center gap-1.5">
          <span className="truncate">
            {agentsById.get(agentId)?.name ?? (agentId || t('webPagesWork.dash'))}
          </span>
          {subline?.orphan ? (
            <Badge tone="warn" title={t('webPagesWork.orphanTitle', { agent: agentId })}>
              <AlertTriangle aria-hidden data-status-icon />
              {t('webPagesWork.orphan')}
            </Badge>
          ) : null}
        </span>
      </td>
      <td data-col="project" data-empty={project ? undefined : ''} className="px-3 py-2.5">
        {project ? (
          <span className="inline-flex max-w-full items-center gap-1.5">
            <FolderKanban className="size-3.5 shrink-0 text-text-muted" aria-hidden />
            <span className="truncate">{projectNames?.get(project) ?? project}</span>
          </span>
        ) : (
          <span className="text-text-muted">{t('webPagesWork.dash')}</span>
        )}
      </td>
      <td data-col="status" className="px-3 py-2.5">
        <div className="flex flex-wrap items-center gap-1.5">
          <LifecycleChip row={row} live={executing} />
          {executing && badge ? (
            <span className="text-small font-medium text-accent">{badge.label}</span>
          ) : open ? (
            <span className="text-small text-text-muted">{t('webPagesWork.statusIdle')}</span>
          ) : null}
        </div>
      </td>
      <td data-col="messages" className="px-3 py-2.5 text-right tabular-nums">
        <MessageSquare
          className="work-narrow-only mr-1 size-3.5 align-[-2px] text-text-muted"
          aria-hidden
        />
        {formatNumber(Number(row.message_count) || 0)}
      </td>
      <td data-col="updated" className="px-3 py-2.5 whitespace-nowrap text-text-muted">
        {relTimeLabel(row.updated_at ?? row.updatedAt ?? '')}
      </td>
      <td data-col="actions" className="py-2.5 pr-3">
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

// ── Move to project ──────────────────────────────────────────────────────────

function MoveDialog({
  keys,
  projectNames,
  current,
  act,
  onClose,
}: {
  keys: string[]
  projectNames?: ReadonlyMap<string, string>
  current: string
  act: Gate
  onClose: (moved: boolean) => void
}) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const [target, setTarget] = useState(current)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const selectId = useId()

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      // sessions.patch takes one key; a null project detaches the session.
      for (const key of keys) {
        await rpc.call('sessions.patch', { key, projectId: target || null })
      }
      toast.success(target ? t('webPagesWork.moved') : t('webPagesWork.detached'))
      void queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY })
      void queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY })
      onClose(true)
    } catch (e) {
      setError(t('webPagesWork.moveFailed', { message: errorMessage(e) }))
      void queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && !busy && onClose(false)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{tPlural('webPagesWork.moveTitle', keys.length)}</DialogTitle>
          <DialogDescription>{t('webPagesWork.moveBody')}</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-1.5">
          <label htmlFor={selectId} className="text-label">
            {t('webPagesWork.colProject')}
          </label>
          <Select id={selectId} value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">{t('webPagesWork.filterNoProject')}</option>
            {[...(projectNames ?? new Map<string, string>())].map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </Select>
        </div>
        {error ? (
          <p
            role="alert"
            className="anim-fade-in rounded-control border border-danger/30 bg-danger/10 px-3 py-2"
          >
            {error}
          </p>
        ) : null}
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={() => onClose(false)}>
            {t('webPagesWork.cancel')}
          </Button>
          <Button
            variant="primary"
            disabled={busy || act.disabled}
            title={act.reason ?? undefined}
            onClick={() => void submit()}
          >
            {busy ? <Loader2 className="animate-spin" /> : null}
            {t('webPagesWork.move')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ── New session (can create the agent first) ─────────────────────────────────

function NewSessionDialog({
  agents,
  projectNames,
  connection,
  onClose,
}: {
  agents: { id: string; name: string }[]
  projectNames?: ReadonlyMap<string, string>
  connection: RpcState
  onClose: () => void
}) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [agent, setAgent] = useState(
    agents.some((a) => a.id === 'main') ? 'main' : (agents[0]?.id ?? NEW_AGENT),
  )
  const [newAgentId, setNewAgentId] = useState('')
  const [project, setProject] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const agentSelectId = useId()
  const newAgentFieldId = useId()
  const projectSelectId = useId()
  const reasonId = useId()

  const creatingAgent = agent === NEW_AGENT
  const agentId = creatingAgent ? newAgentId.trim().toLowerCase() : agent
  const startGate = gate(
    requireConnection(connection),
    !agentId ? blockedBecause(t('webPagesWork.newNeedsAgent')) : null,
    creatingAgent && agentId && !AGENT_ID.test(agentId)
      ? blockedBecause(t('webPagesWork.newAgentInvalid'))
      : null,
  )

  const submit = async () => {
    if (startGate.disabled) return
    setBusy(true)
    setError(null)
    let createdAgent = false
    try {
      if (creatingAgent) {
        try {
          await rpc.call('agents.create', { id: agentId, name: agentId })
          createdAgent = true
        } catch (e) {
          // An agent that already exists is fine: open a session on it.
          if (errorCode(e) !== 'agent.exists') throw e
        }
        void queryClient.invalidateQueries({ queryKey: AGENTS_QUERY_KEY })
      }
      const key = await createSessionOptimistic(rpc, queryClient, {
        agentId,
        projectId: project || null,
      })
      toast.success(
        createdAgent
          ? t('webPagesWork.createdWithAgent', { agent: agentId })
          : t('webPagesWork.created'),
      )
      onClose()
      navigate(sessionKeyToPath(key))
    } catch (e) {
      const code = errorCode(e)
      if (code === 'UNAUTHORIZED' && creatingAgent) setError(t('webPagesWork.createUnauthorized'))
      else if (code === 'agent.not_found')
        setError(t('webPagesWork.createAgentMissing', { agent: agentId }))
      else setError(t('webPagesWork.createFailed', { message: errorMessage(e) }))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && !busy && onClose()}>
      <DialogContent>
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
        >
          <DialogHeader>
            <DialogTitle>{t('webPagesWork.newTitle')}</DialogTitle>
            <DialogDescription>{t('webPagesWork.newBody')}</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={agentSelectId} className="text-label">
              {t('webPagesWork.colAgent')}
            </label>
            <Select id={agentSelectId} value={agent} onChange={(e) => setAgent(e.target.value)}>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
              <option value={NEW_AGENT}>{t('webPagesWork.newAgentOption')}</option>
            </Select>
          </div>
          {creatingAgent ? (
            <div className="flex flex-col gap-1.5">
              <label htmlFor={newAgentFieldId} className="text-label">
                {t('webPagesWork.newAgentId')}
              </label>
              <Input
                id={newAgentFieldId}
                autoFocus
                className="font-mono"
                value={newAgentId}
                placeholder={t('webPagesWork.newAgentPlaceholder')}
                onChange={(e) => setNewAgentId(e.target.value)}
              />
              <span className="text-small text-text-muted">{t('webPagesWork.newAgentHint')}</span>
            </div>
          ) : null}
          <div className="flex flex-col gap-1.5">
            <label htmlFor={projectSelectId} className="text-label">
              {t('webPagesWork.colProject')}
            </label>
            <Select
              id={projectSelectId}
              value={project}
              onChange={(e) => setProject(e.target.value)}
            >
              <option value="">{t('webPagesWork.filterNoProject')}</option>
              {[...(projectNames ?? new Map<string, string>())].map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </Select>
          </div>
          {error ? (
            <p
              role="alert"
              className="anim-fade-in rounded-control border border-danger/30 bg-danger/10 px-3 py-2"
            >
              {error}
            </p>
          ) : null}
          <DialogFooter className="items-center">
            {startGate.reason ? (
              <span id={reasonId} className="text-small text-text-muted sm:mr-auto">
                {startGate.reason}
              </span>
            ) : null}
            <Button variant="outline" disabled={busy} onClick={onClose}>
              {t('webPagesWork.cancel')}
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={busy || startGate.disabled}
              aria-describedby={startGate.reason ? reasonId : undefined}
            >
              {busy ? <Loader2 className="animate-spin" /> : null}
              {creatingAgent ? t('webPagesWork.newCreateAndStart') : t('webPagesWork.newStart')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
