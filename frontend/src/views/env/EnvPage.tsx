import './env.css'
import { useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence } from 'motion/react'
import {
  AlertTriangleIcon,
  CheckIcon,
  ChevronRightIcon,
  EyeIcon,
  LockIcon,
  PlusIcon,
  RefreshCwIcon,
  Trash2Icon,
} from 'lucide-react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '@/components/ui/button'
import {
  ENV_QUERY_KEY,
  filterVars,
  groupByCategory,
  isShadowed,
  shortPath,
  splitGroupRows,
  sourceLabel,
  summarize,
  validateNewName,
  type EnvFilter,
  type EnvListResponse,
  type EnvVarRow,
} from './logic'

const FILTERS: ReadonlyArray<{ id: EnvFilter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'missing', label: 'Missing' },
  { id: 'set', label: 'Set' },
  { id: 'custom', label: 'Custom' },
]

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function EnvPage() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState<EnvFilter>('all')
  const [query, setQuery] = useState('')
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [revealed, setRevealed] = useState<Record<string, string>>({})
  const [adding, setAdding] = useState(false)
  const [newName, setNewName] = useState('')
  const [newValue, setNewValue] = useState('')
  const [newError, setNewError] = useState<string | null>(null)
  // Confirmations use the app's own modal rather than window.confirm: a
  // native dialog cannot be themed, cannot be reached by a test or a screen
  // reader on the same terms as the rest of the surface, and blocks the whole
  // page while it is open.
  const [confirming, setConfirming] = useState<{ kind: 'reveal' | 'unset'; name: string } | null>(
    null,
  )
  // Categories whose quiet tail the operator has asked to see.
  const [expandedTails, setExpandedTails] = useState<Set<string>>(new Set())

  useEffect(() => {
    document.title = 'Environment - AgentOS Control'
  }, [])

  const listQuery = useQuery<EnvListResponse>({
    queryKey: ENV_QUERY_KEY,
    queryFn: () => rpc.call<EnvListResponse>('env.list', {}),
    refetchOnWindowFocus: false,
  })

  const rows = useMemo(() => listQuery.data?.vars ?? [], [listQuery.data])
  const groups = useMemo(
    () => groupByCategory(filterVars(rows, filter, query)),
    [rows, filter, query],
  )
  const summary = useMemo(() => summarize(listQuery.data), [listQuery.data])
  function toggleTail(category: string) {
    setExpandedTails((current) => {
      const next = new Set(current)
      if (next.has(category)) next.delete(category)
      else next.add(category)
      return next
    })
  }

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ENV_QUERY_KEY })
  }

  async function save(name: string, value: string): Promise<boolean> {
    setBusy(name)
    try {
      const result = await rpc.call<EnvVarRow>('env.set', { name, value })
      setEditing(null)
      setDraft('')
      await refresh()
      if (result?.restartRequired) {
        toast.warning(`${name} saved — restart the gateway for it to take full effect.`)
      } else {
        toast.success(`${name} saved.`)
      }
      return true
    } catch (error) {
      // The server is the authority on names and values; show what it said
      // rather than closing the form and losing what was typed.
      setNewError(errorMessage(error))
      toast.error(errorMessage(error))
      return false
    } finally {
      setBusy(null)
    }
  }

  async function importFrom(name: string, sourceId: string) {
    setBusy(name)
    try {
      await rpc.call('env.import', { name, sourceId })
      await refresh()
      toast.success(`${name} imported. It will not follow that source's own rotation.`)
    } catch (error) {
      toast.error(errorMessage(error))
    } finally {
      setBusy(null)
    }
  }

  async function remove(name: string) {
    setBusy(name)
    try {
      await rpc.call('env.unset', { name })
      await refresh()
      toast.success(`${name} removed.`)
    } catch (error) {
      toast.error(errorMessage(error))
    } finally {
      setBusy(null)
    }
  }

  async function reveal(name: string) {
    setBusy(name)
    try {
      const result = await rpc.call<{ value: string }>('env.reveal', { name })
      setRevealed((prev) => ({ ...prev, [name]: result.value }))
      // Auto-hide: a value left on screen ends up in a screen share or a
      // screenshot long after the operator stopped looking at it.
      window.setTimeout(() => {
        setRevealed((prev) => {
          const next = { ...prev }
          delete next[name]
          return next
        })
      }, 30_000)
    } catch (error) {
      toast.error(errorMessage(error))
    } finally {
      setBusy(null)
    }
  }

  function openAdd() {
    setNewName('')
    setNewValue('')
    setNewError(null)
    setAdding(true)
  }

  function closeAdd() {
    setAdding(false)
    setNewError(null)
  }

  async function addVariable() {
    const problem = validateNewName(newName, rows)
    if (problem) {
      setNewError(problem)
      return
    }
    setNewError(null)
    // Only dismiss once the write lands, so a rejected name keeps its context.
    const saved = await save(newName.trim(), newValue)
    if (saved) closeAdd()
  }

  const confirmCopy = confirming
    ? confirming.kind === 'reveal'
      ? {
          title: `Show ${confirming.name}?`,
          body: 'The real value appears on screen and hides again after 30 seconds.',
          action: 'Show value',
        }
      : {
          title: `Remove ${confirming.name}?`,
          body: 'It is deleted from the AgentOS .env and from the running gateway.',
          action: 'Remove',
        }
    : null

  if (listQuery.isError) {
    return (
      <section className="env-stage">
        <div className="env-load-error" role="alert">
          <span aria-hidden="true">
            <AlertTriangleIcon />
          </span>
          <h1>Environment unavailable</h1>
          <p>{errorMessage(listQuery.error)}</p>
          <Button type="button" variant="outline" onClick={() => void listQuery.refetch()}>
            <RefreshCwIcon />
            Retry
          </Button>
        </div>
      </section>
    )
  }

  return (
    <section className="env-stage" aria-busy={listQuery.isLoading || undefined}>
      <header className="env-stage__header">
        <div className="env-stage__title-block">
          <div className="t-label">Configuration</div>
          <h1 className="t-display">Environment</h1>
          <p className="env-stage__subtitle">
            Credentials and settings the gateway, its providers, and your skills read from the
            environment.
          </p>
        </div>
        <div className="env-stage__actions">
          <Button
            type="button"
            variant="outline"
            disabled={listQuery.isFetching}
            onClick={() => void refresh()}
          >
            <RefreshCwIcon className={listQuery.isFetching ? 'env-spin' : undefined} />
            Refresh
          </Button>
          <Button type="button" onClick={openAdd}>
            <PlusIcon />
            Add variable
          </Button>
        </div>
      </header>

      <div className="env-meta">
        <dl className="env-stats">
          <div className="env-stat">
            <dt>Set</dt>
            <dd>
              {summary.setCount}
              <span>/{summary.totalCount}</span>
            </dd>
          </div>
          <div className={summary.missingCount ? 'env-stat is-warn' : 'env-stat'}>
            <dt>Missing</dt>
            <dd>{summary.missingCount}</dd>
          </div>
          <div className={summary.shadowedCount ? 'env-stat is-warn' : 'env-stat'}>
            <dt>Shadowed</dt>
            <dd>{summary.shadowedCount}</dd>
          </div>
        </dl>
        <p className="env-meta__path">
          <span className="t-label">File</span>
          <code title={listQuery.data?.envFilePath}>{shortPath(listQuery.data?.envFilePath)}</code>
        </p>
      </div>

      {summary.shadowedCount > 0 ? (
        <div className="env-warning" role="status">
          <span aria-hidden="true">
            <AlertTriangleIcon />
          </span>
          <div>
            <strong>
              {summary.shadowedCount} variable(s) are shadowed by the process environment.
            </strong>
            <p>
              The shell that started the gateway exported them, and that value wins over the file.
              Editing them here will not take effect until the export is removed and the gateway
              restarts.
            </p>
          </div>
        </div>
      ) : null}

      <div className="env-toolbar">
        <input
          className="env-search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search variables"
          aria-label="Search variables"
        />
        <div className="env-filters" role="group" aria-label="Filter variables">
          {FILTERS.map((entry) => (
            <button
              key={entry.id}
              type="button"
              className={entry.id === filter ? 'env-filter is-active' : 'env-filter'}
              aria-pressed={entry.id === filter}
              onClick={() => setFilter(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </div>
      </div>

      {groups.length === 0 ? (
        <p className="env-empty">
          {listQuery.isLoading ? 'Loading variables…' : 'No variables match this filter.'}
        </p>
      ) : (
        groups.map((group) => {
          const { primary, rest } = splitGroupRows(group)
          const tailOpen = expandedTails.has(group.category)
          const visible = tailOpen ? [...primary, ...rest] : primary
          return (
            <section key={group.category} className="env-group" aria-label={group.label}>
              <h2 className="env-group__header">
                <span className="env-group__label">{group.label}</span>
                <span className="env-group__count">
                  {group.setCount}/{group.rows.length} set
                </span>
              </h2>
              <ul className="env-list">
                {visible.map((row) => (
                  <li key={row.name} className="env-row">
                    <div className="env-row__main">
                      <div className="env-row__name">
                        <code>{row.name}</code>
                        {row.writable ? null : (
                          <span
                            className="env-row__lock"
                            title="Blocked by AgentOS security policy — edit ~/.agentos/.env directly if you genuinely need it."
                          >
                            <LockIcon aria-label="Not writable through AgentOS" />
                          </span>
                        )}
                        <span className={row.isSet ? 'env-badge is-set' : 'env-badge'}>
                          {row.isSet ? 'set' : row.missing ? 'missing' : 'unset'}
                        </span>
                        {row.isSet ? (
                          <span className="env-row__source">{sourceLabel(row.source)}</span>
                        ) : null}
                      </div>
                      {row.description ? <p className="env-row__desc">{row.description}</p> : null}
                      {row.owner ? <p className="env-row__owner">Needed by {row.owner}</p> : null}
                      {isShadowed(row) ? (
                        <p className="env-row__shadow">
                          Shadowed by the process environment — changes here take effect only after
                          the export is removed and the gateway restarts.
                        </p>
                      ) : null}
                      {row.url ? (
                        <a
                          className="env-row__link"
                          href={row.url}
                          target="_blank"
                          rel="noreferrer noopener"
                        >
                          Where to get this
                        </a>
                      ) : null}
                    </div>

                    <div className="env-row__value">
                      {revealed[row.name] ? (
                        <code className="env-row__revealed">{revealed[row.name]}</code>
                      ) : (
                        <code>{row.masked ?? '—'}</code>
                      )}
                    </div>

                    <div className="env-row__actions">
                      {row.writable ? (
                        <>
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            disabled={busy === row.name}
                            onClick={() => {
                              setEditing(editing === row.name ? null : row.name)
                              setDraft('')
                            }}
                            aria-label={row.isSet ? `Edit ${row.name}` : `Set ${row.name}`}
                          >
                            {row.isSet ? 'Edit' : 'Set'}
                          </Button>
                          {!row.isSet && row.availableFrom ? (
                            <Button
                              type="button"
                              variant="outline"
                              size="sm"
                              disabled={busy === row.name}
                              onClick={() => void importFrom(row.name, row.availableFrom!.id)}
                            >
                              Use {row.availableFrom.label}
                            </Button>
                          ) : null}
                          {row.isSet && row.secret ? (
                            <Button
                              type="button"
                              variant="outline"
                              size="sm"
                              disabled={busy === row.name}
                              onClick={() => setConfirming({ kind: 'reveal', name: row.name })}
                            >
                              <EyeIcon />
                              <span className="sr-only">Reveal {row.name}</span>
                            </Button>
                          ) : null}
                          {row.isSet ? (
                            <Button
                              type="button"
                              variant="outline"
                              size="sm"
                              disabled={busy === row.name}
                              onClick={() => setConfirming({ kind: 'unset', name: row.name })}
                            >
                              <Trash2Icon />
                              <span className="sr-only">Remove {row.name}</span>
                            </Button>
                          ) : null}
                        </>
                      ) : null}
                    </div>

                    {editing === row.name ? (
                      <form
                        className="env-row__form"
                        onSubmit={(event) => {
                          event.preventDefault()
                          void save(row.name, draft)
                        }}
                      >
                        <input
                          type={row.secret ? 'password' : 'text'}
                          value={draft}
                          onChange={(event) => setDraft(event.target.value)}
                          aria-label={`Value for ${row.name}`}
                          autoFocus
                        />
                        <Button type="submit" size="sm" disabled={busy === row.name}>
                          <CheckIcon />
                          Save
                        </Button>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => setEditing(null)}
                        >
                          Cancel
                        </Button>
                      </form>
                    ) : null}
                  </li>
                ))}
              </ul>
              {rest.length > 0 ? (
                <button
                  type="button"
                  className="env-group__more"
                  aria-expanded={tailOpen}
                  onClick={() => toggleTail(group.category)}
                >
                  <ChevronRightIcon
                    className={tailOpen ? 'env-group__chevron is-open' : 'env-group__chevron'}
                    aria-hidden="true"
                  />
                  {tailOpen ? `Hide ${rest.length} unset` : `Show ${rest.length} unset`}
                </button>
              ) : null}
            </section>
          )
        })
      )}
      <AnimatePresence>
        {adding ? (
          <ModalShell
            role="dialog"
            labelledBy="env-add-title"
            onClose={closeAdd}
            overlayClassName="env-modal__overlay"
            className="env-modal panel"
          >
            <h2 id="env-add-title">Add a variable</h2>
            <p>
              Stored in the AgentOS <code>.env</code> and applied to the running gateway.
            </p>
            <form
              className="env-add"
              onSubmit={(event) => {
                event.preventDefault()
                void addVariable()
              }}
            >
              <label>
                Name
                <input
                  value={newName}
                  onChange={(event) => setNewName(event.target.value)}
                  placeholder="MY_SERVICE_TOKEN"
                  aria-label="New variable name"
                  aria-invalid={newError ? true : undefined}
                  aria-describedby={newError ? 'env-add-error' : undefined}
                />
              </label>
              <label>
                Value
                <input
                  type="password"
                  value={newValue}
                  onChange={(event) => setNewValue(event.target.value)}
                  aria-label="New variable value"
                />
              </label>
              {newError ? (
                <p id="env-add-error" className="env-add__error" role="alert">
                  {newError}
                </p>
              ) : null}
              <div className="env-modal__actions">
                <Button type="submit" disabled={busy === newName.trim()}>
                  Save
                </Button>
                <Button type="button" variant="outline" onClick={closeAdd}>
                  Cancel
                </Button>
              </div>
            </form>
          </ModalShell>
        ) : null}
      </AnimatePresence>

      {/* AnimatePresence is required, not decorative: ModalShell enters via
          motion variants, and without a presence context the overlay stays at
          its `initial` opacity of 0 — mounted, focus-trapping, and invisible.
          jsdom reports reduced-motion, so unit tests take ModalShell's
          no-variant branch and cannot catch this. */}
      <AnimatePresence>
        {confirming && confirmCopy ? (
          <ModalShell
            role="alertdialog"
            labelledBy="env-confirm-title"
            onClose={() => setConfirming(null)}
            overlayClassName="env-modal__overlay"
            className="env-modal panel"
          >
            <h2 id="env-confirm-title">{confirmCopy.title}</h2>
            <p>{confirmCopy.body}</p>
            <div className="env-modal__actions">
              <Button
                type="button"
                onClick={() => {
                  const target = confirming
                  setConfirming(null)
                  if (target.kind === 'reveal') void reveal(target.name)
                  else void remove(target.name)
                }}
              >
                {confirmCopy.action}
              </Button>
              <Button type="button" variant="outline" onClick={() => setConfirming(null)}>
                Cancel
              </Button>
            </div>
          </ModalShell>
        ) : null}
      </AnimatePresence>
    </section>
  )
}
