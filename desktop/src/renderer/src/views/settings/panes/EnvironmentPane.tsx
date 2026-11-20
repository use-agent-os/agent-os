import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, ExternalLink, Eye, FileText, Lock, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { ModalShell } from '@/components/ModalShell'
import { useConnection } from '@/stores/connection'
import {
  ENV_QUERY_KEY,
  filterVars,
  groupByCategory,
  isShadowed,
  shortPath,
  sourceLabel,
  splitGroupRows,
  summarize,
  validateNewName,
  type EnvFilter,
  type EnvListResponse,
  type EnvVarRow,
} from '@/views/env/logic'
import { Button } from '~/components/ui/button'
import { t, type MessageKey } from '~/i18n'
import { desktopApi, isDesktop } from '~/lib/desktop-api'
import { Card, Head, Notice, Pill, Segmented, Stat, Stats, Value } from '../parts'
import { SNAPSHOT_KEY } from '../use-snapshot'

/** A revealed value hides itself again after this long. */
const REVEAL_MS = 30_000

function fill(key: MessageKey, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce(
    (text, [name, value]) => text.replace(`{${name}}`, String(value)),
    t(key),
  )
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

type Confirming = { kind: 'reveal' | 'unset'; name: string }

/**
 * Settings › Environment: the console's Environment screen in the app's
 * vocabulary. Same RPCs (`env.list/set/unset/import/reveal`) and the same pure
 * helpers, so the two surfaces group, filter and validate identically. A
 * listing never carries a value; reading one is a separate, confirmed,
 * gateway-rate-limited `env.reveal` that hides itself again.
 */
export function EnvironmentPane() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const connected = useConnection((s) => s.state === 'connected')
  const [filter, setFilter] = useState<EnvFilter>('all')
  const [query, setQuery] = useState('')
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [revealed, setRevealed] = useState<Record<string, string>>({})
  const [adding, setAdding] = useState(false)
  const [confirming, setConfirming] = useState<Confirming | null>(null)
  const [openTails, setOpenTails] = useState<Set<string>>(new Set())
  const timers = useRef<number[]>([])

  useEffect(() => () => timers.current.forEach((id) => window.clearTimeout(id)), [])

  const list = useQuery<EnvListResponse>({
    queryKey: ENV_QUERY_KEY,
    enabled: connected,
    refetchOnWindowFocus: false,
    queryFn: () => rpc.call<EnvListResponse>('env.list', {}),
  })

  const rows = useMemo(() => list.data?.vars ?? [], [list.data])
  const groups = useMemo(
    () =>
      groupByCategory(filterVars(rows, filter, query)).filter(
        // "Missing" is about what something needs: a group whose unset
        // variables are all optional has nothing to show there.
        (group) => filter !== 'missing' || splitGroupRows(group).primary.length > 0,
      ),
    [rows, filter, query],
  )
  const summary = useMemo(() => summarize(list.data), [list.data])
  const envFilePath = list.data?.envFilePath ?? ''

  async function refresh() {
    // Provider keys live here too: the Providers section's "needs a key"
    // state comes from the snapshot, so a write here must refresh both.
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ENV_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: SNAPSHOT_KEY }),
    ])
  }

  /** Resolves to the server's error message, or null when the write landed. */
  async function save(name: string, value: string): Promise<string | null> {
    setBusy(name)
    try {
      const result = await rpc.call<EnvVarRow>('env.set', { name, value })
      setEditing(null)
      setDraft('')
      await refresh()
      if (result?.restartRequired)
        toast.warning(fill('settings.env.toast.savedRestart', { name }), { id: 'stg-env' })
      else toast.success(fill('settings.env.toast.saved', { name }), { id: 'stg-env' })
      return null
    } catch (error) {
      toast.error(errorMessage(error), { id: 'stg-env' })
      return errorMessage(error)
    } finally {
      setBusy(null)
    }
  }

  async function importFrom(name: string, sourceId: string) {
    setBusy(name)
    try {
      await rpc.call('env.import', { name, sourceId })
      await refresh()
      toast.success(fill('settings.env.toast.imported', { name }), { id: 'stg-env' })
    } catch (error) {
      toast.error(errorMessage(error), { id: 'stg-env' })
    } finally {
      setBusy(null)
    }
  }

  async function remove(name: string) {
    setBusy(name)
    try {
      await rpc.call('env.unset', { name })
      await refresh()
      toast.success(fill('settings.env.toast.removed', { name }), { id: 'stg-env' })
    } catch (error) {
      toast.error(errorMessage(error), { id: 'stg-env' })
    } finally {
      setBusy(null)
    }
  }

  async function reveal(name: string) {
    setBusy(name)
    try {
      const result = await rpc.call<{ value: string }>('env.reveal', { name })
      setRevealed((prev) => ({ ...prev, [name]: result.value }))
      // A value left on screen ends up in a screen share long after anyone
      // stopped looking at it.
      timers.current.push(
        window.setTimeout(() => {
          setRevealed((prev) => {
            const next = { ...prev }
            delete next[name]
            return next
          })
        }, REVEAL_MS),
      )
    } catch (error) {
      toast.error(errorMessage(error), { id: 'stg-env' })
    } finally {
      setBusy(null)
    }
  }

  function toggleTail(category: string) {
    setOpenTails((current) => {
      const next = new Set(current)
      if (next.has(category)) next.delete(category)
      else next.add(category)
      return next
    })
  }

  const toolbarRef = useRef<HTMLDivElement>(null)

  /** A summary figure is also the way to its rows: set the filter, show the list. */
  function showOnly(next: EnvFilter) {
    setFilter(next)
    setQuery('')
    toolbarRef.current?.scrollIntoView?.({ block: 'start', behavior: 'smooth' })
  }

  const head = (
    <Head
      title={t('settings.section.environment')}
      blurb={t('settings.section.environment.blurb')}
      action={
        connected && !list.isError ? (
          <>
            <Button
              variant="ghost"
              size="icon"
              disabled={list.isFetching}
              aria-label={t('settings.env.refresh')}
              title={t('settings.env.refresh')}
              onClick={() => void refresh()}
            >
              <RefreshCw
                className={list.isFetching ? 'size-3.5 stg-spin' : 'size-3.5'}
                strokeWidth={2}
                aria-hidden
              />
            </Button>
            <Button variant="primary" onClick={() => setAdding(true)}>
              <Plus className="size-3.5" strokeWidth={2} aria-hidden />
              {t('settings.env.add')}
            </Button>
          </>
        ) : undefined
      }
    />
  )

  if (!connected) {
    return (
      <>
        {head}
        <Notice tone="info">{t('settings.offline')}</Notice>
      </>
    )
  }

  if (list.isError) {
    return (
      <>
        {head}
        <Notice
          tone="danger"
          action={<Button onClick={() => void list.refetch()}>{t('settings.env.retry')}</Button>}
        >
          {`${t('settings.env.loadError')}: ${errorMessage(list.error)}`}
        </Notice>
      </>
    )
  }

  const ready = list.isSuccess
  return (
    <>
      {head}

      <Stats label={t('settings.env.stats')}>
        <Stat
          label={t('settings.env.stat.set')}
          value={ready ? `${summary.setCount} / ${summary.totalCount}` : '—'}
          tone={ready && summary.setCount > 0 ? 'ok' : undefined}
          onClick={ready ? () => showOnly('set') : undefined}
        />
        <Stat
          label={t('settings.env.stat.missing')}
          hint={t('settings.env.stat.missing.help')}
          value={ready ? summary.missingCount : '—'}
          tone={summary.missingCount > 0 ? 'warn' : undefined}
          onClick={ready ? () => showOnly('missing') : undefined}
        />
        <Stat
          label={t('settings.env.stat.shadowed')}
          hint={t('settings.env.stat.shadowed.help')}
          value={ready ? summary.shadowedCount : '—'}
          tone={summary.shadowedCount > 0 ? 'warn' : undefined}
        />
      </Stats>

      <div className="env-file">
        <FileText className="size-3.5" strokeWidth={1.75} aria-hidden />
        <span>{t('settings.env.file')}</span>
        <code className="stg-path" title={envFilePath || undefined}>
          {envFilePath ? shortPath(envFilePath) : '…'}
        </code>
        <Button
          variant="ghost"
          disabled={!isDesktop() || !envFilePath}
          onClick={() => void desktopApi().app.showItemInFolder(envFilePath)}
        >
          {t('settings.reveal')}
        </Button>
      </div>

      {summary.shadowedCount > 0 ? (
        <Notice tone="warn">{t('settings.env.shadowWarning')}</Notice>
      ) : null}

      <div className="env-toolbar" ref={toolbarRef}>
        <input
          className="mac-input"
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('settings.env.search')}
          aria-label={t('settings.env.search')}
          autoComplete="off"
          spellCheck={false}
        />
        <Segmented<EnvFilter>
          label={t('settings.env.filter')}
          value={filter}
          onChange={setFilter}
          options={[
            { value: 'all', label: t('settings.env.filter.all') },
            { value: 'missing', label: t('settings.env.filter.missing') },
            { value: 'set', label: t('settings.env.filter.set') },
            { value: 'custom', label: t('settings.env.filter.custom') },
          ]}
        />
      </div>

      {groups.length === 0 ? (
        <p className="env-empty">
          {list.isLoading ? t('settings.env.loading') : t('settings.env.empty')}
        </p>
      ) : (
        groups.map((group) => {
          const { primary, rest } = splitGroupRows(group)
          const tailOpen = openTails.has(group.category)
          const visible = tailOpen ? [...primary, ...rest] : primary
          return (
            <Card
              key={group.category}
              title={group.label}
              action={
                <Value>
                  {fill('settings.env.groupCount', {
                    set: group.setCount,
                    total: group.rows.length,
                  })}
                </Value>
              }
            >
              {visible.map((row) => (
                <EnvRow
                  key={row.name}
                  row={row}
                  busy={busy === row.name}
                  revealed={revealed[row.name]}
                  editing={editing === row.name}
                  draft={draft}
                  onDraft={setDraft}
                  onEdit={() => {
                    setEditing(editing === row.name ? null : row.name)
                    setDraft('')
                  }}
                  onCancel={() => setEditing(null)}
                  onSave={() => void save(row.name, draft)}
                  onImport={(sourceId) => void importFrom(row.name, sourceId)}
                  onReveal={() => setConfirming({ kind: 'reveal', name: row.name })}
                  onRemove={() => setConfirming({ kind: 'unset', name: row.name })}
                />
              ))}
              {rest.length > 0 ? (
                <button
                  type="button"
                  className="env-tail app-no-drag"
                  aria-expanded={tailOpen}
                  onClick={() => toggleTail(group.category)}
                >
                  {fill(tailOpen ? 'settings.env.tail.hide' : 'settings.env.tail.show', {
                    n: rest.length,
                  })}
                </button>
              ) : null}
            </Card>
          )
        })
      )}

      {adding ? (
        <AddDialog
          rows={rows}
          busy={busy}
          onCancel={() => setAdding(false)}
          onSave={async (name, value) => {
            const problem = await save(name, value)
            if (problem === null) setAdding(false)
            return problem
          }}
        />
      ) : null}

      {confirming ? (
        <ConfirmDialog
          confirming={confirming}
          onCancel={() => setConfirming(null)}
          onConfirm={() => {
            const target = confirming
            setConfirming(null)
            if (target.kind === 'reveal') void reveal(target.name)
            else void remove(target.name)
          }}
        />
      ) : null}
    </>
  )
}

function EnvRow({
  row,
  busy,
  revealed,
  editing,
  draft,
  onDraft,
  onEdit,
  onCancel,
  onSave,
  onImport,
  onReveal,
  onRemove,
}: {
  row: EnvVarRow
  busy: boolean
  revealed: string | undefined
  editing: boolean
  draft: string
  onDraft: (value: string) => void
  onEdit: () => void
  onCancel: () => void
  onSave: () => void
  onImport: (sourceId: string) => void
  onReveal: () => void
  onRemove: () => void
}) {
  const valueId = useId()
  const badge = row.isSet
    ? { tone: 'ok' as const, key: 'settings.env.badge.set' as const }
    : row.missing
      ? { tone: 'warn' as const, key: 'settings.env.badge.missing' as const }
      : { tone: undefined, key: 'settings.env.badge.unset' as const }
  const help =
    row.description || row.owner || isShadowed(row) || row.url ? (
      <>
        {row.description ? <span>{row.description}</span> : null}
        {row.owner ? <span>{fill('settings.env.owner', { owner: row.owner })}</span> : null}
        {isShadowed(row) ? (
          <span className="env-row__shadow">{t('settings.env.shadowed')}</span>
        ) : null}
        {row.url ? (
          <button
            type="button"
            className="prov-keylink"
            onClick={() => void desktopApi().app.openExternal(row.url)}
          >
            {t('settings.env.link')}
            <ExternalLink className="size-3" strokeWidth={2} aria-hidden />
          </button>
        ) : null}
      </>
    ) : null

  return (
    <div className="env-row" data-testid={`env-row-${row.name}`}>
      <div className="stg-row" data-align="start">
        <div className="stg-row__label">
          <span className="env-row__name">
            <code>{row.name}</code>
            {row.writable ? null : (
              <span className="env-row__lock" title={t('settings.env.locked')}>
                <Lock className="size-3" strokeWidth={2} aria-label={t('settings.env.locked')} />
              </span>
            )}
            <Pill tone={badge.tone}>{t(badge.key)}</Pill>
            {row.isSet ? <span className="env-row__source">{sourceLabel(row.source)}</span> : null}
          </span>
          {help ? <span className="stg-row__help">{help}</span> : null}
        </div>
        <div className="stg-row__control">
          <Value tone={revealed ? 'primary' : undefined} title={row.masked ?? undefined}>
            {revealed ?? row.masked ?? '—'}
          </Value>
          {row.writable ? (
            <>
              {!row.isSet && row.availableFrom ? (
                <Button disabled={busy} onClick={() => onImport(row.availableFrom!.id)}>
                  {fill('settings.env.import', { source: row.availableFrom.label })}
                </Button>
              ) : null}
              <Button
                disabled={busy}
                aria-expanded={editing}
                aria-label={`${row.isSet ? t('settings.env.edit') : t('settings.env.set')} ${row.name}`}
                onClick={onEdit}
              >
                {row.isSet ? t('settings.env.edit') : t('settings.env.set')}
              </Button>
              {row.isSet && row.secret ? (
                <Button
                  variant="ghost"
                  size="icon"
                  disabled={busy}
                  aria-label={fill('settings.env.reveal', { name: row.name })}
                  onClick={onReveal}
                >
                  <Eye className="size-3.5" strokeWidth={2} aria-hidden />
                </Button>
              ) : null}
              {row.isSet ? (
                <Button
                  variant="ghost"
                  size="icon"
                  disabled={busy}
                  aria-label={fill('settings.env.remove', { name: row.name })}
                  onClick={onRemove}
                >
                  <Trash2 className="size-3.5" strokeWidth={2} aria-hidden />
                </Button>
              ) : null}
            </>
          ) : null}
        </div>
      </div>
      {editing ? (
        <form
          className="env-edit"
          onSubmit={(e) => {
            e.preventDefault()
            onSave()
          }}
        >
          <input
            id={valueId}
            className="mac-input"
            data-mono="true"
            type={row.secret ? 'password' : 'text'}
            value={draft}
            onChange={(e) => onDraft(e.target.value)}
            aria-label={fill('settings.env.value', { name: row.name })}
            autoComplete="off"
            spellCheck={false}
            autoFocus
          />
          <Button type="submit" variant="primary" disabled={busy}>
            <Check className="size-3.5" strokeWidth={2} aria-hidden />
            {t('settings.env.save')}
          </Button>
          <Button onClick={onCancel}>{t('settings.env.cancel')}</Button>
        </form>
      ) : null}
    </div>
  )
}

function AddDialog({
  rows,
  busy,
  onCancel,
  onSave,
}: {
  rows: EnvVarRow[]
  busy: string | null
  onCancel: () => void
  onSave: (name: string, value: string) => Promise<string | null>
}) {
  const titleId = useId()
  const bodyId = useId()
  const nameId = useId()
  const valueId = useId()
  const errorId = useId()
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)

  async function submit() {
    // Answered instantly for a typo; the server stays the authority and its
    // refusal keeps the dialog open with what was typed.
    const problem = validateNewName(name, rows)
    if (problem) {
      setError(problem)
      return
    }
    setError(await onSave(name.trim(), value))
  }

  return (
    <ModalShell
      role="dialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={onCancel}
      overlayClassName="stg-confirm__overlay"
      className="stg-confirm env-dialog"
    >
      <h2 id={titleId}>{t('settings.env.addTitle')}</h2>
      <p id={bodyId}>{t('settings.env.addBody')}</p>
      <form
        className="env-dialog__form"
        onSubmit={(e) => {
          e.preventDefault()
          void submit()
        }}
      >
        <label htmlFor={nameId}>{t('settings.env.addName')}</label>
        <input
          id={nameId}
          className="mac-input"
          data-mono="true"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t('settings.env.addNamePlaceholder')}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          autoComplete="off"
          spellCheck={false}
          autoFocus
        />
        <label htmlFor={valueId}>{t('settings.env.addValue')}</label>
        <input
          id={valueId}
          className="mac-input"
          data-mono="true"
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoComplete="off"
        />
        {error ? (
          <p id={errorId} className="stg-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="stg-confirm__actions">
          <Button onClick={onCancel}>{t('settings.env.cancel')}</Button>
          <Button type="submit" variant="primary" disabled={busy !== null}>
            {t('settings.env.save')}
          </Button>
        </div>
      </form>
    </ModalShell>
  )
}

function ConfirmDialog({
  confirming,
  onCancel,
  onConfirm,
}: {
  confirming: Confirming
  onCancel: () => void
  onConfirm: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  const reveal = confirming.kind === 'reveal'
  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={onCancel}
      overlayClassName="stg-confirm__overlay"
      className="stg-confirm"
    >
      <h2 id={titleId}>
        {fill(reveal ? 'settings.env.confirmReveal.title' : 'settings.env.confirmRemove.title', {
          name: confirming.name,
        })}
      </h2>
      <p id={bodyId}>
        {t(reveal ? 'settings.env.confirmReveal.body' : 'settings.env.confirmRemove.body')}
      </p>
      <div className="stg-confirm__actions">
        <Button onClick={onCancel}>{t('settings.env.cancel')}</Button>
        <Button variant={reveal ? 'primary' : 'danger'} onClick={onConfirm}>
          {t(reveal ? 'settings.env.confirmReveal.confirm' : 'settings.env.confirmRemove.confirm')}
        </Button>
      </div>
    </ModalShell>
  )
}
