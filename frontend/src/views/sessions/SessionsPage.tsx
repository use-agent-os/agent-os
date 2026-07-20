import './sessions.css'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  CopyIcon,
  MessageSquareIcon,
  PlusIcon,
  RefreshCwIcon,
  SearchIcon,
  Trash2Icon,
} from 'lucide-react'
import { toast } from 'sonner'
import { AsciiField } from '@/components/AsciiField'
import { Button } from '@/components/ui/button'
import { useRpc } from '@/app/providers'
import {
  agentIdFromKey,
  agentSubline,
  buildDeleteParams,
  dotTone,
  filterSessions,
  parseBulkDeleteResult,
  parseSingleDeleteResult,
  relTimeLabel,
  runStatusBadge,
  sessionStats,
  sessionStatusChip,
  sessionStatusLabel,
  sessionVisualStatus,
  sortSessions,
  type AgentEntry,
  type RawSession,
  type SortColumn,
  type Tone,
} from './logic'

interface SessionsList {
  sessions?: RawSession[]
}
interface AgentsList {
  agents?: (AgentEntry & { model?: string; type?: string; isBuiltin?: boolean })[]
}
interface RpcError {
  code?: string
  message?: string
}

const PAGE_SIZES = [10, 25, 50, 100]

// ── Modal shell (mirrors the agents view: overlay + Escape/backdrop close) ────
function ModalShell({
  role,
  labelledBy,
  describedBy,
  onClose,
  className,
  children,
}: {
  role: 'dialog' | 'alertdialog'
  labelledBy: string
  describedBy?: string
  onClose: () => void
  className?: string
  children: React.ReactNode
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const first = panelRef.current?.querySelector<HTMLElement>(
      'input:not([disabled]), textarea, select, button',
    )
    first?.focus()
  }, [])
  return (
    <div
      className="sess-modal__overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={panelRef}
        className={`sess-modal panel ${className ?? ''}`}
        role={role}
        aria-modal="true"
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.stopPropagation()
            onClose()
          }
        }}
      >
        {children}
      </div>
    </div>
  )
}

// ── Reusable destructive confirmation (alertdialog) ──────────────────────────
function ConfirmDialog({
  title,
  body,
  confirmLabel,
  cancelLabel = 'Cancel',
  busy = false,
  onCancel,
  onConfirm,
}: {
  title: string
  body: React.ReactNode
  confirmLabel: string
  cancelLabel?: string
  busy?: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={busy ? () => {} : onCancel}
      className="sess-confirm"
    >
      <header className="sess-dialog__head">
        <h3 id={titleId} className="sess-dialog__title">
          {title}
        </h3>
      </header>
      <div id={bodyId} className="sess-confirm__body">
        {body}
      </div>
      <footer className="sess-dialog__foot">
        <Button type="button" variant="ghost" disabled={busy} onClick={onCancel}>
          {cancelLabel}
        </Button>
        <Button type="button" variant="destructive" disabled={busy} onClick={onConfirm}>
          {confirmLabel}
        </Button>
      </footer>
    </ModalShell>
  )
}

// ── New-session dialog (agent picker + inline create) ────────────────────────
// sessions.js:561-716 — an agent combobox that either selects an existing agent
// or, when a novel id is typed, creates it (agents.create) before the session.
function NewSessionDialog({
  agents,
  onCancel,
  onSubmit,
  submitting,
  error,
}: {
  agents: { id: string; label: string; sublabel: string }[]
  onCancel: () => void
  onSubmit: (agentId: string, createPending: boolean) => void
  submitting: boolean
  error: string | null
}) {
  const titleId = useId()
  const listId = useId()
  // sessions.js:608 — default to `main` when it exists.
  const [value, setValue] = useState(() => (agents.some((a) => a.id === 'main') ? 'main' : ''))
  const typed = value.trim()
  // sessions.js:640-646 — an exact match selects that agent; otherwise a typed
  // id is a pending create.
  const exact = agents.find((a) => a.id === typed || a.label === typed)
  const createPending = !exact && typed.length > 0
  const canSubmit = typed.length > 0 && !submitting

  function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!canSubmit) return
    const agentId = exact ? exact.id : typed
    onSubmit(agentId, createPending)
  }

  return (
    <ModalShell role="dialog" labelledBy={titleId} onClose={onCancel} className="sess-newchat">
      <form className="sess-dialog" onSubmit={submit}>
        <header className="sess-dialog__head">
          <span className="t-label">Control · Sessions</span>
          <h3 id={titleId} className="sess-dialog__title">
            Start a new chat
          </h3>
        </header>
        <div className="sess-dialog__body">
          <label className="sess-field">
            <span className="t-label">Agent</span>
            <input
              className="sess-input"
              list={listId}
              autoComplete="off"
              value={value}
              placeholder="Pick an agent or type a new ID"
              onChange={(e) => setValue(e.target.value)}
            />
            <datalist id={listId}>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.sublabel ? `${a.label} — ${a.sublabel}` : a.label}
                </option>
              ))}
            </datalist>
            <small className="sess-field__hint">
              {createPending
                ? `↵ Create a new agent "${typed}" and start a chat.`
                : 'Pick an agent or type a new ID to create it.'}
            </small>
          </label>
          {error ? (
            <div className="sess-field__error" role="alert">
              {error}
            </div>
          ) : null}
        </div>
        <footer className="sess-dialog__foot">
          <Button type="button" variant="ghost" disabled={submitting} onClick={onCancel}>
            Cancel
          </Button>
          <Button type="submit" disabled={!canSubmit}>
            {submitting ? (createPending ? 'Creating…' : 'Starting…') : 'Start chat'}
          </Button>
        </footer>
      </form>
    </ModalShell>
  )
}

// ── Stat tile ────────────────────────────────────────────────────────────────
function StatTile({
  label,
  value,
  hint,
  hero,
  active,
}: {
  label: string
  value: React.ReactNode
  hint: React.ReactNode
  hero?: boolean
  active?: boolean
}) {
  return (
    <div className={`sess-stat${hero ? ' sess-stat--hero' : ''}`} aria-label={label}>
      <span className="sess-stat__label t-label">{label}</span>
      <strong className="sess-stat__value t-data">
        {value}
        {active ? <span className="sess-stat__pulse tone-ok" aria-hidden="true" /> : null}
      </strong>
      <span className="sess-stat__hint">{hint}</span>
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────
type Dialog =
  | { kind: 'none' }
  | { kind: 'new' }
  | { kind: 'delete'; key: string }
  | { kind: 'bulk'; keys: string[] }

export function SessionsPage() {
  const rpc = useRpc()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [search, setSearch] = useState('')
  const [debounced, setDebounced] = useState('')
  const [sortCol, setSortCol] = useState<SortColumn>('updated_at')
  const [sortAsc, setSortAsc] = useState(false)
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(25)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [dialog, setDialog] = useState<Dialog>({ kind: 'none' })
  const [createError, setCreateError] = useState<string | null>(null)

  useEffect(() => {
    document.title = 'Sessions - AgentOS Control'
  }, [])

  // sessions.js:84-97 — debounce the search input (180ms); a new query resets
  // the page and selection.
  useEffect(() => {
    const id = setTimeout(() => {
      setDebounced(search.trim().toLowerCase())
      setPage(0)
      setSelected(new Set())
    }, 180)
    return () => clearTimeout(id)
  }, [search])

  // sessions.js:135-136 — sessions.list {limit:200} (opt into the larger page
  // size for the WebUI only; CLI default stays 50).
  const sessionsQuery = useQuery<RawSession[]>({
    queryKey: ['sessions'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<SessionsList>('sessions.list', { limit: 200 })
      return data.sessions ?? []
    },
    refetchOnWindowFocus: false,
  })

  // sessions.js:137,140-148 — agents.list drives orphan detection; a failure
  // keeps the last known map (handled by react-query retaining prior data).
  const agentsQuery = useQuery<AgentEntry[]>({
    queryKey: ['sessions', 'agents'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<AgentsList>('agents.list', {})
      return data.agents ?? []
    },
    refetchOnWindowFocus: false,
  })

  // sessions.js:150-152 — every successful sessions fetch clears the selection
  // (legacy _loadData `_selected.clear()`), so a Refresh/refetch drops bulk
  // selection + the bulk-action bar. dataUpdatedAt advances on every successful
  // load (including a refetch that resolves to identical data). Done as a
  // render-phase reset keyed on dataUpdatedAt (React's supported "adjust state
  // when a derived value changes" pattern, mirroring ConfigPage) rather than an
  // effect, so the cleared state lands before paint with no cascading render.
  const sessionsUpdatedAt = sessionsQuery.dataUpdatedAt
  const [lastLoadAt, setLastLoadAt] = useState(0)
  if (sessionsUpdatedAt && sessionsUpdatedAt !== lastLoadAt) {
    setLastLoadAt(sessionsUpdatedAt)
    if (selected.size > 0) setSelected(new Set())
  }

  // sessions.js:158 — load-failure toast (stable id so repeats dedupe).
  useEffect(() => {
    if (sessionsQuery.isError) {
      const err = sessionsQuery.error
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to load sessions: ' + message, { id: 'sessions-load-err' })
    }
  }, [sessionsQuery.isError, sessionsQuery.error])

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['sessions'] })
  }

  const allSessions = useMemo(() => sessionsQuery.data ?? [], [sessionsQuery.data])
  const agentsLoaded = agentsQuery.isSuccess
  const agentsById = useMemo(() => {
    const map = new Map<string, AgentEntry>()
    for (const a of agentsQuery.data ?? []) if (a.id) map.set(a.id, a)
    return map
  }, [agentsQuery.data])

  const stats = sessionStats(allSessions)
  const filtered = useMemo(
    () => sortSessions(filterSessions(allSessions, debounced), sortCol, sortAsc),
    [allSessions, debounced, sortCol, sortAsc],
  )

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const safePage = Math.min(page, totalPages - 1)
  const slice = filtered.slice(safePage * pageSize, (safePage + 1) * pageSize)
  const allOnPageSelected = slice.length > 0 && slice.every((s) => selected.has(s.key ?? ''))

  // ── Delete mutation (single + bulk share the endpoint) ─────────────────────
  const deleteMutation = useMutation({
    mutationFn: (keys: string[]) => rpc.call('sessions.delete', buildDeleteParams(keys)),
    onSuccess: (data, keys) => {
      if (keys.length === 1) {
        const outcome = parseSingleDeleteResult(data as never, keys[0]!)
        if (outcome.ok) toast.success('Session deleted', { id: 'sessions-delete' })
        else toast.error('Delete failed: ' + outcome.reason, { id: 'sessions-delete-err' })
      } else {
        const { okCount, errCount } = parseBulkDeleteResult(data as never, keys.length)
        if (errCount > 0)
          toast.warning(`Deleted ${okCount}, ${errCount} failed`, { id: 'sessions-delete' })
        else
          toast.success(`Deleted ${okCount} session${okCount === 1 ? '' : 's'}`, {
            id: 'sessions-delete',
          })
      }
      setSelected(new Set())
      setDialog({ kind: 'none' })
      invalidate()
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Delete failed: ' + message, { id: 'sessions-delete-err' })
      setDialog({ kind: 'none' })
      invalidate()
    },
  })

  // ── Create-session mutation (optional inline agent create) ─────────────────
  const createMutation = useMutation({
    mutationFn: async (vars: { agentId: string; createPending: boolean }) => {
      let createdAgent = false
      if (vars.createPending) {
        try {
          await rpc.call('agents.create', { id: vars.agentId, name: vars.agentId })
          createdAgent = true
        } catch (err) {
          // sessions.js:686-688 — tolerate an already-existing agent.
          if ((err as RpcError).code !== 'agent.exists') throw err
        }
      }
      const res = await rpc.call<{ key?: string }>('sessions.create', { agentId: vars.agentId })
      return { key: res?.key, createdAgent }
    },
    onSuccess: (res, vars) => {
      toast.success(
        res.createdAgent ? `Created agent "${vars.agentId}" and started chat` : 'Session created',
        { id: 'sessions-create' },
      )
      setDialog({ kind: 'none' })
      setCreateError(null)
      invalidate()
      if (res.key) navigate('/chat?session=' + encodeURIComponent(res.key))
    },
    onError: (err, vars) => {
      // sessions.js:698-708 — friendly inline errors; dialog stays open.
      const e = err as RpcError
      const code = e.code || ''
      let friendly = 'Failed to start chat: ' + (e.message || String(err))
      if (code === 'UNAUTHORIZED' && vars.createPending)
        friendly = 'This connection does not have permission to create agents.'
      if (code === 'agent.not_found')
        friendly = `Agent "${vars.agentId}" doesn't exist. Type a new ID and it will be created.`
      if (code === 'agent.exists')
        friendly = `Agent "${vars.agentId}" already exists — pick it from the list instead.`
      setCreateError(friendly)
    },
  })

  const agentOptions = useMemo(
    () =>
      (agentsQuery.data ?? []).map((a) => {
        const withMeta = a as AgentEntry & { model?: string; type?: string; isBuiltin?: boolean }
        return {
          id: a.id ?? '',
          label: a.name || a.id || '',
          sublabel:
            withMeta.model || (withMeta.isBuiltin || withMeta.type === 'builtin' ? 'built-in' : ''),
        }
      }),
    [agentsQuery.data],
  )

  function toggleRow(key: string, on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (on) next.add(key)
      else next.delete(key)
      return next
    })
  }
  function toggleAllOnPage(on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev)
      for (const s of slice) {
        const k = s.key ?? ''
        if (on) next.add(k)
        else next.delete(k)
      }
      return next
    })
  }
  function onSort(col: SortColumn) {
    if (sortCol === col) setSortAsc((a) => !a)
    else {
      setSortCol(col)
      setSortAsc(true)
    }
  }
  async function copyKey(key: string) {
    try {
      await navigator.clipboard.writeText(key)
      toast.success('Copied session key', { id: 'sessions-copy-ok', duration: 1600 })
    } catch {
      toast.warning('Copy failed', { id: 'sessions-copy-err' })
    }
  }

  const hasSessions = allSessions.length > 0
  const sortArrow = (col: SortColumn) => (sortCol === col ? (sortAsc ? ' ▲' : ' ▼') : '')
  const ariaSort = (col: SortColumn): 'ascending' | 'descending' | 'none' =>
    sortCol === col ? (sortAsc ? 'ascending' : 'descending') : 'none'

  return (
    <div className="sess-stage">
      <header className="sess-stage__header">
        <AsciiField />
        <div className="sess-stage__title-block">
          <span className="t-label">Control · Sessions</span>
          <h2 className="t-display">Sessions</h2>
          <p className="sess-stage__subtitle">
            Session history, current task activity, and agent runs — open one to chat, or clean up
            old state.
          </p>
        </div>
        <div className="sess-stage__actions">
          <div className="sess-search-wrap">
            <SearchIcon className="sess-search-icon" aria-hidden="true" />
            <input
              type="text"
              className="sess-search-input"
              placeholder="Search sessions…"
              autoComplete="off"
              aria-label="Search sessions"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <Button
            variant="outline"
            title="Refresh"
            className="text-xs uppercase tracking-[0.14em]"
            onClick={invalidate}
          >
            <RefreshCwIcon />
            <span>Refresh</span>
          </Button>
          <Button
            className="text-xs uppercase tracking-[0.14em]"
            onClick={() => {
              setCreateError(null)
              setDialog({ kind: 'new' })
            }}
          >
            <PlusIcon />
            <span>New session</span>
          </Button>
        </div>
      </header>

      <section className="sess-stats" aria-label="Sessions summary">
        <StatTile
          label="Total sessions"
          hero
          value={stats.total}
          hint={`${stats.lifecycleOpen} open · ${stats.done} completed · ${stats.failedOrTimedOut} failed/timed out · ${stats.aborted} aborted`}
        />
        <StatTile
          label="Executing"
          value={stats.activeRuns}
          active={stats.activeRuns > 0}
          hint={stats.activeRuns ? 'tasks queued/running' : 'none executing'}
        />
        <StatTile
          label="Messages"
          value={stats.totalMessages.toLocaleString()}
          hint={`${stats.agents} agent${stats.agents === 1 ? '' : 's'} · across all sessions`}
        />
      </section>

      {selected.size > 0 ? (
        <div className="sess-bulk-bar" role="region" aria-label="Bulk actions">
          <span className="sess-bulk-bar__count">
            <strong>{selected.size}</strong> selected
          </span>
          <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
            Clear
          </Button>
          <span className="sess-bulk-bar__spacer" />
          <Button
            variant="destructive"
            size="sm"
            onClick={() => setDialog({ kind: 'bulk', keys: Array.from(selected) })}
          >
            <Trash2Icon />
            <span>Delete selected</span>
          </Button>
        </div>
      ) : null}

      <section className="sess-list">
        <div className="sess-list__head">
          <h3 className="sess-list__title t-label">
            {debounced ? 'Matching sessions' : 'All sessions'}{' '}
            <span className="sess-list__count t-data">
              {debounced ? `${filtered.length} of ${stats.total}` : stats.total}
            </span>
          </h3>
          <label className="sess-page-size t-label">
            <span>Show</span>
            <select
              value={pageSize}
              aria-label="Rows per page"
              onChange={(e) => {
                setPageSize(Number(e.target.value))
                setPage(0)
              }}
            >
              {PAGE_SIZES.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
        </div>

        {!hasSessions ? (
          <div className="sess-empty">
            <div className="sess-empty__title">No sessions yet.</div>
            <p className="sess-empty__msg">
              Sessions appear here as soon as you chat with an agent or schedule a cron job. Start
              one and pick up the conversation any time.
            </p>
            <Button
              onClick={() => {
                setCreateError(null)
                setDialog({ kind: 'new' })
              }}
            >
              <PlusIcon />
              <span>Start a new session</span>
            </Button>
          </div>
        ) : slice.length === 0 ? (
          <div className="sess-empty sess-empty--search">
            <div className="sess-empty__title">No matches</div>
            <p className="sess-empty__msg">
              No sessions match your search. Try a different query, or clear it to see everything.
            </p>
          </div>
        ) : (
          <div className="sess-table-wrap">
            <table className="sess-table">
              <thead>
                <tr>
                  <th className="sess-table__cell--check">
                    <input
                      type="checkbox"
                      aria-label="Select all sessions on this page"
                      checked={allOnPageSelected}
                      onChange={(e) => toggleAllOnPage(e.target.checked)}
                    />
                  </th>
                  <th aria-sort={ariaSort('key')}>
                    <button type="button" className="sess-th-sort" onClick={() => onSort('key')}>
                      Session key<span aria-hidden="true">{sortArrow('key')}</span>
                    </button>
                  </th>
                  <th>Status</th>
                  <th aria-sort={ariaSort('message_count')}>
                    <button
                      type="button"
                      className="sess-th-sort"
                      onClick={() => onSort('message_count')}
                    >
                      Msgs<span aria-hidden="true">{sortArrow('message_count')}</span>
                    </button>
                  </th>
                  <th aria-sort={ariaSort('updated_at')}>
                    <button
                      type="button"
                      className="sess-th-sort"
                      onClick={() => onSort('updated_at')}
                    >
                      Modified<span aria-hidden="true">{sortArrow('updated_at')}</span>
                    </button>
                  </th>
                  <th className="sess-table__cell--actions" />
                </tr>
              </thead>
              <tbody>
                {slice.map((row) => {
                  const key = row.key ?? ''
                  const visual = sessionVisualStatus(row)
                  const statusTone = dotTone(visual)
                  const statusLabel = sessionStatusLabel(visual)
                  const chipTone: Tone = sessionStatusChip(visual)
                  const badge = runStatusBadge(row)
                  const agentId = row.agent_id || row.agentId || agentIdFromKey(key) || ''
                  const sub = agentSubline(agentId, agentsById, agentsLoaded)
                  const isSel = selected.has(key)
                  return (
                    <tr key={key} className={isSel ? 'is-selected' : ''}>
                      <td className="sess-table__cell--check">
                        <input
                          type="checkbox"
                          aria-label={`Select session ${key}`}
                          checked={isSel}
                          onChange={(e) => toggleRow(key, e.target.checked)}
                        />
                      </td>
                      <td className="sess-table__cell--key">
                        <div className="sess-key-content">
                          <span
                            className={`sess-dot tone-${statusTone}`}
                            title={statusLabel}
                            aria-hidden="true"
                          />
                          <button
                            type="button"
                            className="sess-key-link t-data"
                            title="Open chat"
                            onClick={() => navigate('/chat?session=' + encodeURIComponent(key))}
                          >
                            {key}
                          </button>
                          {sub ? (
                            <span
                              className={`sess-key__agent${sub.orphan ? ' sess-key__agent--orphan' : ''}`}
                              title={
                                sub.orphan
                                  ? `Agent '${sub.name}' is no longer registered`
                                  : undefined
                              }
                            >
                              {sub.name}
                              {sub.orphan ? (
                                <span className="sess-chip tone-warn">⚠ Orphaned</span>
                              ) : null}
                            </span>
                          ) : null}
                        </div>
                      </td>
                      <td>
                        <div className="sess-status-stack">
                          <span className={`sess-chip tone-${chipTone}`}>{statusLabel}</span>
                          {badge ? (
                            <span className={`sess-chip tone-${badge.tone}`} title={badge.label}>
                              {badge.label}
                            </span>
                          ) : null}
                        </div>
                      </td>
                      <td className="t-data sess-dim">
                        {row.message_count != null
                          ? Number(row.message_count).toLocaleString()
                          : '—'}
                      </td>
                      <td className="t-data sess-dim">
                        {row.updated_at != null ? relTimeLabel(row.updated_at) : '—'}
                      </td>
                      <td className="sess-table__cell--actions">
                        <Button
                          variant="ghost"
                          size="icon-xs"
                          title="Open chat"
                          aria-label={`Open chat for ${key}`}
                          onClick={() => navigate('/chat?session=' + encodeURIComponent(key))}
                        >
                          <MessageSquareIcon />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon-xs"
                          title="Copy session key"
                          aria-label={`Copy session key ${key}`}
                          onClick={() => void copyKey(key)}
                        >
                          <CopyIcon />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon-xs"
                          className="sess-iconbtn--danger"
                          title="Delete"
                          aria-label={`Delete session ${key}`}
                          onClick={() => setDialog({ kind: 'delete', key })}
                        >
                          <Trash2Icon />
                        </Button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {hasSessions && filtered.length > 0 ? (
          <div className="sess-pagination">
            <Button
              variant="ghost"
              size="icon-xs"
              disabled={safePage === 0}
              title="Previous page"
              aria-label="Previous page"
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              <ChevronLeftIcon />
            </Button>
            <span className="sess-page-info t-data">
              {safePage + 1} / {totalPages}{' '}
              <span className="sess-dim">· {filtered.length} total</span>
            </span>
            <Button
              variant="ghost"
              size="icon-xs"
              disabled={safePage >= totalPages - 1}
              title="Next page"
              aria-label="Next page"
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            >
              <ChevronRightIcon />
            </Button>
          </div>
        ) : null}
      </section>

      {dialog.kind === 'new' ? (
        <NewSessionDialog
          agents={agentOptions}
          submitting={createMutation.isPending}
          error={createError}
          onCancel={() => {
            setDialog({ kind: 'none' })
            setCreateError(null)
          }}
          onSubmit={(agentId, createPending) => {
            setCreateError(null)
            createMutation.mutate({ agentId, createPending })
          }}
        />
      ) : null}

      {dialog.kind === 'delete' ? (
        <ConfirmDialog
          title="Delete session"
          body={
            <>
              <p>
                Delete session <strong>{dialog.key}</strong>? This cannot be undone.
              </p>
              <p className="sess-confirm__warn">
                The transcript will not be flushed to disk; use <code>/reset</code> first if you
                want a backup.
              </p>
            </>
          }
          confirmLabel="Delete"
          busy={deleteMutation.isPending}
          onCancel={() => setDialog({ kind: 'none' })}
          onConfirm={() => deleteMutation.mutate([dialog.key])}
        />
      ) : null}

      {dialog.kind === 'bulk' ? (
        <ConfirmDialog
          title="Delete sessions"
          body={
            <>
              <p>
                Delete <strong>{dialog.keys.length}</strong> session
                {dialog.keys.length === 1 ? '' : 's'}? This cannot be undone.
              </p>
              <p className="sess-confirm__warn">
                The transcript will not be flushed to disk; use <code>/reset</code> first if you
                want a backup.
              </p>
            </>
          }
          confirmLabel="Delete all"
          busy={deleteMutation.isPending}
          onCancel={() => setDialog({ kind: 'none' })}
          onConfirm={() => deleteMutation.mutate(dialog.keys)}
        />
      ) : null}
    </div>
  )
}
