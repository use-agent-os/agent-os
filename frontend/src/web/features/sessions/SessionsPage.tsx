import { useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  FolderInput,
  Loader2,
  MessagesSquare,
  MoreHorizontal,
  Pencil,
  Plus,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import { useId, useMemo, useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router'
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
import { Button } from '@/web/components/ui/button'
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
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
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
import { sessionKeyToPath } from '@/web/shell/session-url'
import type { RpcState } from '@/lib/ws-rpc'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useSessionsTotal, type SessionsTotal } from './sessions-total'

// `/sessions` (task 8.2): every session in one table, on views/sessions/logic.
// Search, agent and project filters, stat tiles, inline rename, bulk delete,
// move to project, a new-session dialog that can create an agent, and a flag on
// rows whose agent is no longer registered.

const PAGE_SIZE = 50
const NEW_AGENT = '__new__'
const AGENT_ID = /^[a-z0-9_-]+$/

const TONE_CLASS: Record<Tone, string> = {
  ok: 'text-ok',
  warn: 'text-warn',
  danger: 'text-danger',
  info: 'text-info',
  dim: 'text-text-muted',
}

const fieldClass =
  'h-9 w-full rounded-md border border-border-control bg-surface-0 px-3 text-ui touch:h-11 max-md:text-base'

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
    body = (
      <LoadingRegion label={t('webPagesWork.sessionsLoading')}>
        {Array.from({ length: 8 }, (_, i) => (
          <Skeleton key={i} className="my-2 h-9" />
        ))}
      </LoadingRegion>
    )
  } else if (rows.length === 0 && sessions.isError) {
    // The error notice above says what failed; there is nothing to show under it.
    body = null
  } else if (rows.length === 0) {
    body = (
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
    )
  } else {
    body = (
      <>
        <StatTiles stats={stats} totals={totals} />
        <div className="flex flex-wrap items-end gap-2">
          <label className="relative block min-w-0 flex-[2_1_14rem]">
            <span className="sr-only">{t('webPagesWork.sessionsSearch')}</span>
            <Search
              className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-text-muted"
              aria-hidden
            />
            <input
              type="search"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value)
                setLimit(PAGE_SIZE)
              }}
              placeholder={t('webPagesWork.sessionsSearch')}
              className={cn(fieldClass, 'pl-8')}
            />
          </label>
          <label className="flex min-w-0 flex-[1_1_9rem] flex-col gap-1">
            <span className="text-small text-text-muted">{t('webPagesWork.filterAgent')}</span>
            <select
              className={fieldClass}
              value={agentFilter}
              onChange={(e) => setAgentFilter(e.target.value)}
            >
              <option value="">{t('webPagesWork.filterAllAgents')}</option>
              {agentOptions.map((id) => (
                <option key={id} value={id}>
                  {agentsById.get(id)?.name ?? id}
                </option>
              ))}
            </select>
          </label>
          <label className="flex min-w-0 flex-[1_1_9rem] flex-col gap-1">
            <span className="text-small text-text-muted">{t('webPagesWork.filterProject')}</span>
            <select
              className={fieldClass}
              value={projectFilter}
              onChange={(e) => setProjectFilter(e.target.value)}
            >
              <option value="all">{t('webPagesWork.filterAllProjects')}</option>
              <option value="none">{t('webPagesWork.filterNoProject')}</option>
              {[...(projectNames ?? new Map<string, string>())].map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {chosen.length > 0 ? (
          <div
            role="region"
            aria-label={t('webPagesWork.bulkRegion')}
            className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-surface-2 px-3 py-2"
          >
            <span className="flex-1 font-medium">
              {tPlural('webPagesWork.bulkSelected', chosen.length)}
            </span>
            <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>
              {t('webPagesWork.bulkClear')}
            </Button>
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
          </div>
        ) : null}

        {notice ? (
          <p
            role={notice.tone === 'ok' ? 'status' : 'alert'}
            className={cn(
              'flex items-start gap-2 rounded-md border px-3 py-2',
              notice.tone === 'danger'
                ? 'border-danger/40 bg-danger/10'
                : 'border-warn/40 bg-warn/10',
            )}
          >
            <span className="flex-1">{notice.text}</span>
            <button
              type="button"
              aria-label={t('webPagesWork.dismiss')}
              onClick={() => setNotice(null)}
              className="target inline-flex items-center justify-center rounded text-text-muted"
            >
              <X className="size-4" aria-hidden />
            </button>
          </p>
        ) : null}

        {filtered.length === 0 ? (
          <EmptyState
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
          <div
            role="region"
            aria-label={t('webPagesWork.sessionsTable')}
            tabIndex={0}
            className="overflow-x-auto rounded-lg border border-border"
          >
            <table className="w-full min-w-[760px] border-collapse text-left">
              <thead className="bg-surface-2 text-small text-text-muted">
                <tr>
                  <th scope="col" className="w-10 px-3 py-2">
                    <input
                      type="checkbox"
                      aria-label={t('webPagesWork.selectAll')}
                      checked={allVisibleSelected}
                      onChange={(e) => toggleAll(e.target.checked)}
                      className="size-4"
                    />
                  </th>
                  <SortHeader
                    col="key"
                    label={t('webPagesWork.colSession')}
                    sortCol={sortCol}
                    asc={sortAsc}
                    onSort={onSort}
                  />
                  <th scope="col" className="px-3 py-2 font-medium">
                    {t('webPagesWork.colAgent')}
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    {t('webPagesWork.colProject')}
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
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
                  <th scope="col" className="w-10 px-3 py-2">
                    <span className="sr-only">{t('webPagesWork.colActions')}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
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
          </div>
        )}
        {filtered.length > visible.length ? (
          <div className="flex items-center justify-center gap-3">
            <span className="text-small text-text-muted">
              {t('webPagesWork.showingOf', { shown: visible.length, total: filtered.length })}
            </span>
            <Button variant="outline" size="sm" onClick={() => setLimit((n) => n + PAGE_SIZE)}>
              {t('webPagesWork.showMore')}
            </Button>
          </div>
        ) : null}
      </>
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 md:p-6">
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
  const tiles: { label: string; value: string; hint?: string }[] = [
    {
      label: t('webPagesWork.statSessions'),
      value: sessionsCountText(totals),
      hint: t('webPagesWork.statSessionsHint', {
        open: stats.lifecycleOpen,
        done: stats.done,
        failed: stats.failedOrTimedOut,
        aborted: stats.aborted,
      }),
    },
    { label: t('webPagesWork.statRunning'), value: formatNumber(stats.activeRuns) },
    { label: t('webPagesWork.statFailed'), value: formatNumber(stats.failedOrTimedOut) },
    { label: t('webPagesWork.statMessages'), value: formatNumber(stats.totalMessages) },
    { label: t('webPagesWork.statAgents'), value: formatNumber(stats.agents) },
  ]
  return (
    <div className="flex flex-col gap-2">
      {totals.truncated ? (
        <p role="note" className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-small">
          {totals.total !== null
            ? t('webPagesWork.sessionsTruncated', {
                shown: formatNumber(totals.listed),
                total: formatNumber(totals.total),
              })
            : t('webPagesWork.sessionsTruncatedUnknown', { shown: formatNumber(totals.listed) })}
        </p>
      ) : null}
      <p role="status" className="flex items-center gap-2 text-small text-text-muted">
        <span
          aria-hidden
          className={cn(
            'size-2 rounded-full',
            stats.activeRuns > 0 ? 'animate-pulse bg-ok' : 'bg-text-muted/50',
          )}
        />
        {stats.activeRuns > 0
          ? tPlural('webPagesWork.executingNow', stats.activeRuns)
          : t('webPagesWork.executingNone')}
      </p>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        {tiles.map((tile) => (
          <div
            key={tile.label}
            className="min-w-0 rounded-lg border border-border bg-surface-1 px-3 py-2"
          >
            <dt className="text-small text-text-muted">{tile.label}</dt>
            <dd className="text-lg font-semibold tabular-nums">{tile.value}</dd>
            {tile.hint ? (
              <dd className="text-small break-words text-text-muted">{tile.hint}</dd>
            ) : null}
          </div>
        ))}
      </dl>
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

/** The lifecycle chip (open / completed / failed / aborted / timed out) of a row. */
function LifecycleChip({ row }: { row: SessionRow }) {
  const visual = sessionVisualStatus(row)
  const label = LIFECYCLE_LABEL[visual]?.() ?? t('webPagesWork.lifecycleUnknown')
  return (
    <span
      data-lifecycle={visual}
      className={cn(
        'inline-flex items-center rounded-full border border-current/40 px-2 text-small whitespace-nowrap',
        TONE_CLASS[sessionStatusChip(visual)],
      )}
    >
      {label}
    </span>
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
  return (
    <th
      scope="col"
      aria-sort={active ? (asc ? 'ascending' : 'descending') : 'none'}
      className="px-3 py-2 font-medium"
    >
      <button
        type="button"
        onClick={() => onSort(col)}
        className="target inline-flex items-center gap-1 rounded hover:text-text"
      >
        {label}
        {active ? (
          asc ? (
            <ArrowUp className="size-3.5" aria-hidden />
          ) : (
            <ArrowDown className="size-3.5" aria-hidden />
          )
        ) : null}
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
  const [renaming, setRenaming] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
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
      className={cn('border-t border-border align-top', selected && 'bg-accent-soft')}
    >
      <td className="px-3 py-2">
        {row.provisional ? null : (
          <input
            type="checkbox"
            aria-label={t('webPagesWork.selectRow', { title })}
            checked={selected}
            onChange={(e) => onSelect(e.target.checked)}
            className="size-4"
          />
        )}
      </td>
      <td className="max-w-[22rem] px-3 py-2">
        {renaming ? (
          <form
            className="flex items-center gap-1"
            onSubmit={(e) => {
              e.preventDefault()
              void submitRename()
            }}
          >
            <input
              autoFocus
              aria-label={t('webPagesWork.renameLabel')}
              placeholder={t('webPagesWork.renamePlaceholder')}
              value={draft}
              disabled={busy}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  e.stopPropagation()
                  setRenaming(false)
                  setError(null)
                }
              }}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? errorId : undefined}
              className="h-8 min-w-0 flex-1 rounded border border-border-control bg-surface-0 px-2"
            />
            <Button size="sm" type="submit" disabled={busy || act.disabled}>
              {t('webPagesWork.save')}
            </Button>
          </form>
        ) : (
          <div className="flex min-w-0 items-center gap-1">
            <Link
              to={sessionKeyToPath(row.key)}
              className="target min-w-0 truncate font-medium hover:underline"
            >
              {title}
            </Link>
            {row.provisional ? (
              <span className="text-small text-text-muted">{t('webPagesWork.provisional')}</span>
            ) : (
              <Button
                size="icon-sm"
                variant="ghost"
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
        <div className="truncate font-mono text-small text-text-muted">{row.key}</div>
        {error ? (
          <p id={errorId} role="alert" className="text-small text-danger">
            {error}
          </p>
        ) : null}
      </td>
      <td className="px-3 py-2">
        <span>{agentsById.get(agentId)?.name ?? (agentId || t('webPagesWork.dash'))}</span>
        {subline?.orphan ? (
          <span
            className="ml-1 inline-flex items-center gap-1 rounded-full border border-warn/50 px-1.5 text-small text-warn"
            title={t('webPagesWork.orphanTitle', { agent: agentId })}
          >
            <AlertTriangle className="size-3" aria-hidden data-status-icon />
            {t('webPagesWork.orphan')}
          </span>
        ) : null}
      </td>
      <td className="px-3 py-2">
        {project ? (
          (projectNames?.get(project) ?? project)
        ) : (
          <span className="text-text-muted">{t('webPagesWork.dash')}</span>
        )}
      </td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <LifecycleChip row={row} />
          {executing && badge ? (
            <span className={cn('text-small font-medium', TONE_CLASS[badge.tone])}>
              {badge.label}
            </span>
          ) : open ? (
            <span className="text-small text-text-muted">{t('webPagesWork.statusIdle')}</span>
          ) : null}
        </div>
      </td>
      <td className="px-3 py-2 tabular-nums">{formatNumber(Number(row.message_count) || 0)}</td>
      <td className="px-3 py-2 whitespace-nowrap text-text-muted">
        {relTimeLabel(row.updated_at ?? row.updatedAt ?? '')}
      </td>
      <td className="px-3 py-2">
        {row.provisional ? null : (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={t('webPagesWork.rowActions', { title })}
              >
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem asChild>
                <Link to={sessionKeyToPath(row.key)}>{t('webPagesWork.openChat')}</Link>
              </DropdownMenuItem>
              <DropdownMenuItem disabled={act.disabled} onSelect={startRename}>
                {t('webPagesWork.rename')}
              </DropdownMenuItem>
              <DropdownMenuItem disabled={act.disabled} onSelect={onMove}>
                {t('webPagesWork.moveToProject')}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem variant="danger" disabled={act.disabled} onSelect={onDelete}>
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
        <div className="flex flex-col gap-1">
          <label htmlFor={selectId} className="font-medium">
            {t('webPagesWork.colProject')}
          </label>
          <select
            id={selectId}
            className={fieldClass}
            value={target}
            onChange={(e) => setTarget(e.target.value)}
          >
            <option value="">{t('webPagesWork.filterNoProject')}</option>
            {[...(projectNames ?? new Map<string, string>())].map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </select>
        </div>
        {error ? (
          <p role="alert" className="text-danger">
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
          <div className="flex flex-col gap-1">
            <label htmlFor={agentSelectId} className="font-medium">
              {t('webPagesWork.colAgent')}
            </label>
            <select
              id={agentSelectId}
              className={fieldClass}
              value={agent}
              onChange={(e) => setAgent(e.target.value)}
            >
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
              <option value={NEW_AGENT}>{t('webPagesWork.newAgentOption')}</option>
            </select>
          </div>
          {creatingAgent ? (
            <div className="flex flex-col gap-1">
              <label htmlFor={newAgentFieldId} className="font-medium">
                {t('webPagesWork.newAgentId')}
              </label>
              <input
                id={newAgentFieldId}
                autoFocus
                className={fieldClass}
                value={newAgentId}
                placeholder={t('webPagesWork.newAgentPlaceholder')}
                onChange={(e) => setNewAgentId(e.target.value)}
              />
              <span className="text-small text-text-muted">{t('webPagesWork.newAgentHint')}</span>
            </div>
          ) : null}
          <div className="flex flex-col gap-1">
            <label htmlFor={projectSelectId} className="font-medium">
              {t('webPagesWork.colProject')}
            </label>
            <select
              id={projectSelectId}
              className={fieldClass}
              value={project}
              onChange={(e) => setProject(e.target.value)}
            >
              <option value="">{t('webPagesWork.filterNoProject')}</option>
              {[...(projectNames ?? new Map<string, string>())].map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </select>
          </div>
          {error ? (
            <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2">
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
