import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ChevronRight,
  Download,
  Eye,
  EyeOff,
  Loader2,
  Lock,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react'
import { useEffect, useId, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webSetAccess'
import { gate, requireConnection, type Gate } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useRegisterFields } from '../fields'

// Settings › Environment (overhaul-control-ui 7.7, Appendix A "Env"):
// variables by category with a quiet tail, set, unset, reveal (hidden again
// on its own), import, and shadowed / source indicators.

/** How long a revealed value stays on screen (docs: "thirty seconds"). */
const ENV_REVEAL_MS = 30_000

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function useWriteGate(): Gate {
  return gate(requireConnection(useDisplayedConnection()))
}

function GateReason({ gate: g, className }: { gate: Gate; className?: string }) {
  return g.reason ? <p className={cn('text-small text-text-muted', className)}>{g.reason}</p> : null
}

const inputClass =
  'h-9 w-full rounded-md border border-border-control bg-surface-0 px-3 aria-[invalid=true]:border-danger touch:h-11 touch:text-base'

function filterOptions(): readonly { id: EnvFilter; label: string }[] {
  return [
    { id: 'all', label: t('webSetAccess.envFilterAll') },
    { id: 'missing', label: t('webSetAccess.envFilterMissing') },
    { id: 'set', label: t('webSetAccess.envFilterSet') },
    { id: 'custom', label: t('webSetAccess.envFilterCustom') },
  ]
}

interface SetResult {
  restartRequired?: boolean
}

/** env.set / env.import / env.unset, refreshing the list after each. */
function useEnvWrites() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const refresh = () => queryClient.invalidateQueries({ queryKey: ENV_QUERY_KEY })
  const set = useMutation({
    mutationFn: (v: { name: string; value: string }) => rpc.call<SetResult>('env.set', v),
    onSuccess: async (result, v) => {
      await refresh()
      if (result?.restartRequired)
        toast.warning(t('webSetAccess.envSavedRestart', { name: v.name }))
      else toast.success(t('webSetAccess.envSaved', { name: v.name }))
    },
  })
  return { set, refresh }
}

// ── Row ─────────────────────────────────────────────────────────────────────

function EnvRow({ row, write }: { row: EnvVarRow; write: Gate }) {
  const rpc = useRpc()
  const inputId = useId()
  const { set, refresh } = useEnvWrites()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [confirmUnset, setConfirmUnset] = useState(false)
  const [confirmReveal, setConfirmReveal] = useState(false)
  // The fetched value is held only while shown: hiding it, by hand or after
  // ENV_REVEAL_MS, drops it from memory as well as from the screen.
  const [revealed, setRevealed] = useState<string | null>(null)
  const shown = revealed !== null
  useEffect(() => {
    if (revealed === null) return
    const id = window.setTimeout(() => setRevealed(null), ENV_REVEAL_MS)
    return () => window.clearTimeout(id)
  }, [revealed])

  const action = useMutation({
    mutationFn: async (kind: 'reveal' | 'import' | 'unset') => {
      if (kind === 'reveal') {
        const result = await rpc.call<{ value: string }>('env.reveal', { name: row.name })
        setRevealed(result?.value ?? '')
        return
      }
      if (kind === 'import') {
        await rpc.call('env.import', { name: row.name, sourceId: row.availableFrom?.id })
        toast.success(t('webSetAccess.envImported', { name: row.name }))
      } else {
        await rpc.call('env.unset', { name: row.name })
        toast.success(t('webSetAccess.envRemoved', { name: row.name }))
      }
      await refresh()
    },
    onMutate: () => setError(null),
    onError: (e) => setError(errorMessage(e)),
  })
  const busy = action.isPending || set.isPending

  const save = (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    set.mutate(
      { name: row.name, value: draft },
      {
        onSuccess: () => {
          setEditing(false)
          setDraft('')
        },
        onError: (err) => setError(errorMessage(err)),
      },
    )
  }

  const status = row.isSet
    ? t('webSetAccess.envBadgeSet')
    : row.missing
      ? t('webSetAccess.envBadgeMissing')
      : t('webSetAccess.envBadgeUnset')

  return (
    <li className="flex flex-col gap-2 py-3" data-env-row={row.name}>
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <code className="font-mono font-medium">{row.name}</code>
            {row.writable ? null : (
              <span
                className="inline-flex items-center gap-1 text-small text-text-muted"
                title={t('webSetAccess.envReadOnlyTitle')}
              >
                <Lock className="size-3.5" aria-hidden />
                {t('webSetAccess.envReadOnly')}
              </span>
            )}
            <span
              className={cn(
                'rounded-full border px-2 py-0.5 text-small',
                row.isSet
                  ? 'border-ok/50 text-ok'
                  : row.missing
                    ? 'border-warn/50 text-warn'
                    : 'border-border text-text-muted',
              )}
            >
              {status}
            </span>
            {row.isSet && sourceLabel(row.source) ? (
              <span className="text-small text-text-muted">
                {t('webSetAccess.envSource', { source: sourceLabel(row.source) })}
              </span>
            ) : null}
          </div>
          {row.description ? <p className="text-small text-text-muted">{row.description}</p> : null}
          {row.owner ? (
            <p className="text-small text-text-muted">
              {t('webSetAccess.envOwner', { owner: row.owner })}
            </p>
          ) : null}
          {isShadowed(row) ? (
            <p className="flex items-center gap-1 text-small text-warn">
              <AlertTriangle className="size-3.5" aria-hidden data-status-icon />
              {t('webSetAccess.envShadowed')}
            </p>
          ) : null}
          {row.url ? (
            <a
              href={row.url}
              target="_blank"
              rel="noreferrer noopener"
              className="text-small text-accent underline-offset-2 hover:underline"
            >
              {t('webSetAccess.envGetKey')}
            </a>
          ) : null}
        </div>
        <code
          className={cn(
            'max-w-full font-mono text-small break-all',
            shown ? 'text-text' : 'text-text-muted',
          )}
          data-testid={`env-value-${row.name}`}
        >
          {shown ? revealed : (row.masked ?? '—')}
        </code>
      </div>

      {row.writable ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            size="sm"
            variant="outline"
            disabled={write.disabled || busy}
            aria-expanded={editing}
            aria-label={
              row.isSet
                ? t('webSetAccess.envEditLabel', { name: row.name })
                : t('webSetAccess.envSetLabel', { name: row.name })
            }
            onClick={() => {
              setEditing((v) => !v)
              setDraft('')
            }}
          >
            {row.isSet ? t('webSetAccess.envEdit') : t('webSetAccess.envSet')}
          </Button>
          {!row.isSet && row.availableFrom ? (
            <Button
              size="sm"
              variant="outline"
              disabled={write.disabled || busy}
              onClick={() => action.mutate('import')}
            >
              <Download />
              {t('webSetAccess.envImport', { source: row.availableFrom.label })}
            </Button>
          ) : null}
          {row.isSet && row.secret ? (
            <Button
              size="sm"
              variant="ghost"
              disabled={!shown && (write.disabled || busy)}
              aria-label={
                shown
                  ? t('webSetAccess.envHideLabel', { name: row.name })
                  : t('webSetAccess.envRevealLabel', { name: row.name })
              }
              onClick={() => (shown ? setRevealed(null) : setConfirmReveal(true))}
            >
              {action.isPending && action.variables === 'reveal' ? (
                <Loader2 className="animate-spin" />
              ) : shown ? (
                <EyeOff />
              ) : (
                <Eye />
              )}
              {shown ? t('webSetAccess.hide') : t('webSetAccess.reveal')}
            </Button>
          ) : null}
          {row.isSet ? (
            <Button
              size="sm"
              variant="ghost"
              className="text-danger"
              disabled={write.disabled || busy}
              aria-label={t('webSetAccess.envRemoveLabel', { name: row.name })}
              onClick={() => setConfirmUnset(true)}
            >
              <Trash2 />
              {t('webSetAccess.envRemove')}
            </Button>
          ) : null}
          <GateReason gate={write} />
        </div>
      ) : null}

      {editing ? (
        <form onSubmit={save} className="flex flex-wrap items-center gap-2">
          <label htmlFor={inputId} className="sr-only">
            {t('webSetAccess.envValueLabel', { name: row.name })}
          </label>
          <input
            id={inputId}
            type={row.secret ? 'password' : 'text'}
            autoComplete="off"
            className={cn(inputClass, 'max-w-md flex-1')}
            value={draft}
            autoFocus
            onChange={(e) => setDraft(e.target.value)}
          />
          <Button type="submit" size="sm" variant="primary" disabled={write.disabled || busy}>
            {set.isPending ? <Loader2 className="animate-spin" /> : null}
            {t('webSetAccess.envSave')}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
            {t('webCommon.cancel')}
          </Button>
        </form>
      ) : null}

      {error ? (
        <p role="alert" className="text-small text-danger">
          {error}
        </p>
      ) : null}

      <AlertDialog open={confirmReveal} onOpenChange={setConfirmReveal}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('webSetAccess.envRevealTitle', { name: row.name })}
            </AlertDialogTitle>
            <AlertDialogDescription>{t('webSetAccess.envRevealBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webCommon.cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={() => action.mutate('reveal')}>
              {t('webSetAccess.reveal')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <AlertDialog open={confirmUnset} onOpenChange={setConfirmUnset}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('webSetAccess.envRemoveTitle', { name: row.name })}
            </AlertDialogTitle>
            <AlertDialogDescription>{t('webSetAccess.envRemoveBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webCommon.cancel')}</AlertDialogCancel>
            <AlertDialogAction destructive onClick={() => action.mutate('unset')}>
              {t('webSetAccess.envRemove')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </li>
  )
}

// ── Add dialog ──────────────────────────────────────────────────────────────

function AddVariableDialog({
  known,
  write,
  onClose,
}: {
  known: EnvVarRow[]
  write: Gate
  onClose: () => void
}) {
  const nameId = useId()
  const valueId = useId()
  const { set } = useEnvWrites()
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const problem = validateNewName(name, known)
    if (problem) {
      setError(problem)
      return
    }
    setError(null)
    // Close only once the write lands, so a rejected name keeps its context.
    set.mutate(
      { name: name.trim(), value },
      { onSuccess: onClose, onError: (err) => setError(errorMessage(err)) },
    )
  }

  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>{t('webSetAccess.envAddTitle')}</DialogTitle>
            <DialogDescription>{t('webSetAccess.envAddBody')}</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1">
            <label htmlFor={nameId} className="font-medium">
              {t('webSetAccess.envAddName')}
            </label>
            <input
              id={nameId}
              autoComplete="off"
              className={cn(inputClass, 'font-mono')}
              value={name}
              placeholder={t('webSetAccess.envAddNamePlaceholder')}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? `${nameId}-error` : undefined}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor={valueId} className="font-medium">
              {t('webSetAccess.envAddValue')}
            </label>
            <input
              id={valueId}
              type="password"
              autoComplete="off"
              className={inputClass}
              value={value}
              onChange={(e) => setValue(e.target.value)}
            />
          </div>
          {error ? (
            <p id={`${nameId}-error`} role="alert" className="text-small text-danger">
              {error}
            </p>
          ) : null}
          <DialogFooter className="items-center">
            <GateReason gate={write} className="mr-auto" />
            <Button variant="outline" onClick={onClose}>
              {t('webCommon.cancel')}
            </Button>
            <Button type="submit" variant="primary" disabled={write.disabled || set.isPending}>
              {t('webSetAccess.envSave')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ── Section ─────────────────────────────────────────────────────────────────

export function EnvironmentSection() {
  const rpc = useRpc()
  const write = useWriteGate()
  const [filter, setFilter] = useState<EnvFilter>('all')
  const [query, setQuery] = useState('')
  const [adding, setAdding] = useState(false)
  // Categories whose quiet tail (unset, not required) is expanded.
  const [openTails, setOpenTails] = useState<ReadonlySet<string>>(new Set())

  useRegisterFields('environment', [
    { id: 'env.add', label: t('webSetAccess.envAdd') },
    { id: 'env.search', label: t('webSetAccess.envSearch') },
  ])

  const listQuery = useQuery<EnvListResponse>({
    queryKey: ENV_QUERY_KEY,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<EnvListResponse>('env.list', {})
    },
    refetchOnWindowFocus: false,
  })
  const rows = useMemo(() => listQuery.data?.vars ?? [], [listQuery.data])
  const groups = useMemo(
    () => groupByCategory(filterVars(rows, filter, query)),
    [rows, filter, query],
  )
  const summary = summarize(listQuery.data)

  const toggleTail = (category: string) =>
    setOpenTails((current) => {
      const next = new Set(current)
      if (next.has(category)) next.delete(category)
      else next.add(category)
      return next
    })

  if (listQuery.isPending) {
    return (
      <LoadingRegion>
        <Skeleton className="h-48" />
      </LoadingRegion>
    )
  }
  if (listQuery.isError && !listQuery.data) {
    return (
      <ErrorState
        title={t('webSetAccess.envLoadFailed')}
        message={errorMessage(listQuery.error)}
        onRetry={() => void listQuery.refetch()}
      />
    )
  }

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center gap-2" data-field-id="env.add">
        <Button variant="primary" disabled={write.disabled} onClick={() => setAdding(true)}>
          <Plus />
          {t('webSetAccess.envAdd')}
        </Button>
        <Button
          variant="outline"
          disabled={listQuery.isFetching}
          onClick={() => void listQuery.refetch()}
        >
          <RefreshCw className={listQuery.isFetching ? 'animate-spin' : undefined} />
          {t('webSetAccess.refresh')}
        </Button>
        <GateReason gate={write} className="w-full" />
      </div>

      <dl className="grid grid-cols-3 gap-2">
        <div className="rounded-lg border border-border p-3">
          <dt className="text-small text-text-muted">{t('webSetAccess.envStatSet')}</dt>
          <dd className="font-mono text-xl font-semibold">
            {t('webSetAccess.envStatSetValue', {
              set: summary.setCount,
              total: summary.totalCount,
            })}
          </dd>
        </div>
        <div
          className={cn(
            'rounded-lg border p-3',
            summary.missingCount ? 'border-warn/50' : 'border-border',
          )}
        >
          <dt className="text-small text-text-muted">{t('webSetAccess.envStatMissing')}</dt>
          <dd className="font-mono text-xl font-semibold">{summary.missingCount}</dd>
        </div>
        <div
          className={cn(
            'rounded-lg border p-3',
            summary.shadowedCount ? 'border-warn/50' : 'border-border',
          )}
        >
          <dt className="text-small text-text-muted">{t('webSetAccess.envStatShadowed')}</dt>
          <dd className="font-mono text-xl font-semibold">{summary.shadowedCount}</dd>
        </div>
      </dl>
      {listQuery.data?.envFilePath ? (
        <p className="text-small text-text-muted">
          {t('webSetAccess.envFile')}{' '}
          <code className="font-mono" title={listQuery.data.envFilePath}>
            {shortPath(listQuery.data.envFilePath)}
          </code>
        </p>
      ) : null}

      {summary.shadowedCount > 0 ? (
        <div
          role="status"
          className="flex items-start gap-2 rounded-lg border border-warn/50 bg-warn/10 px-3 py-2"
        >
          <AlertTriangle className="mt-0.5 size-4 text-warn" aria-hidden data-status-icon />
          <div>
            <p className="font-medium">
              {t('webSetAccess.envShadowTitle', { count: summary.shadowedCount })}
            </p>
            <p className="text-small text-text-muted">{t('webSetAccess.envShadowBody')}</p>
          </div>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2" data-field-id="env.search">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('webSetAccess.envSearch')}
          aria-label={t('webSetAccess.envSearch')}
          className={cn(inputClass, 'max-w-xs')}
        />
        <div role="group" aria-label={t('webSetAccess.envFilter')} className="flex gap-1">
          {filterOptions().map((option) => (
            <Button
              key={option.id}
              size="sm"
              variant={option.id === filter ? 'secondary' : 'ghost'}
              aria-pressed={option.id === filter}
              onClick={() => setFilter(option.id)}
            >
              {option.label}
            </Button>
          ))}
        </div>
      </div>

      {groups.length === 0 ? (
        <p className="py-6 text-center text-text-muted">{t('webSetAccess.envEmptyFiltered')}</p>
      ) : (
        groups.map((group) => {
          const { primary, rest } = splitGroupRows(group)
          const tailOpen = openTails.has(group.category)
          const visible = tailOpen ? [...primary, ...rest] : primary
          return (
            <section key={group.category} aria-label={group.label} className="flex flex-col">
              <h2 className="flex items-baseline gap-2 border-b border-border pb-1 font-semibold">
                {group.label}
                <span className="text-small font-normal text-text-muted">
                  {t('webSetAccess.envGroupCount', {
                    set: group.setCount,
                    total: group.rows.length,
                  })}
                </span>
              </h2>
              {visible.length ? (
                <ul role="list" className="divide-y divide-border">
                  {visible.map((row) => (
                    <EnvRow key={row.name} row={row} write={write} />
                  ))}
                </ul>
              ) : null}
              {rest.length > 0 ? (
                <Button
                  size="sm"
                  variant="ghost"
                  className="self-start text-text-muted"
                  aria-expanded={tailOpen}
                  onClick={() => toggleTail(group.category)}
                >
                  <ChevronRight
                    className={cn('transition-transform', tailOpen && 'rotate-90')}
                    aria-hidden
                  />
                  {tailOpen
                    ? t('webSetAccess.envTailHide', { count: rest.length })
                    : t('webSetAccess.envTailShow', { count: rest.length })}
                </Button>
              ) : null}
            </section>
          )
        })
      )}

      {adding ? (
        <AddVariableDialog known={rows} write={write} onClose={() => setAdding(false)} />
      ) : null}
    </div>
  )
}
