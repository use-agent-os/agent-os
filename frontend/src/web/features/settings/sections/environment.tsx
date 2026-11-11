import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  CircleDashed,
  Download,
  ExternalLink,
  Eye,
  EyeOff,
  FileText,
  KeyRound,
  Layers,
  Loader2,
  Lock,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  SearchX,
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
import { Badge } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { StatCard } from '@/web/components/ui/card'
import { IconTile, STAT_CARD_DENSE } from '@/web/features/home/overview-ui'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { Input } from '@/web/components/ui/input'
import { SegmentedControl } from '@/web/components/ui/segmented-control'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { SecretInput, useRegisterFields, useReveal } from '../fields'
import { Notice } from '../ui'

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

  const revealing = action.isPending && action.variables === 'reveal'

  return (
    <li
      className="flex flex-col gap-2.5 px-4 py-3 transition-colors duration-[var(--duration-fast)] hover:bg-surface-2/40"
      data-env-row={row.name}
    >
      <div className="grid gap-x-4 gap-y-2 @xl:grid-cols-[minmax(0,1fr)_minmax(0,15rem)] @xl:items-start">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <code className="set-key font-medium">{row.name}</code>
            <Badge
              tone={row.isSet ? 'ok' : row.missing ? 'warn' : 'neutral'}
              dot={row.isSet || row.missing}
            >
              {status}
            </Badge>
            {row.writable ? null : (
              <Badge tone="neutral" variant="outline" title={t('webSetAccess.envReadOnlyTitle')}>
                <Lock className="size-3" aria-hidden />
                {t('webSetAccess.envReadOnly')}
              </Badge>
            )}
            {row.isSet && sourceLabel(row.source) ? (
              <span className="text-small text-text-muted">
                {t('webSetAccess.envSource', { source: sourceLabel(row.source) })}
              </span>
            ) : null}
          </div>
          {row.description ? (
            <p className="mt-1 text-small text-pretty text-text-muted">{row.description}</p>
          ) : null}
          {row.owner ? (
            <p className="text-small text-text-muted">
              {t('webSetAccess.envOwner', { owner: row.owner })}
            </p>
          ) : null}
          {isShadowed(row) ? (
            <p className="mt-1 flex items-center gap-1 text-small text-warn">
              <AlertTriangle className="size-3.5" aria-hidden data-status-icon />
              {t('webSetAccess.envShadowed')}
            </p>
          ) : null}
          {row.url ? (
            <a
              href={row.url}
              target="_blank"
              rel="noreferrer noopener"
              className="mt-1 inline-flex items-center gap-1 text-small text-accent underline-offset-2 hover:underline"
            >
              {t('webSetAccess.envGetKey')}
              <ExternalLink className="size-3" aria-hidden />
            </a>
          ) : null}
        </div>
        <div className="flex min-w-0 items-center gap-1 rounded-control border border-border bg-surface-inset py-0.5 pr-0.5 pl-2.5 shadow-inset">
          <code
            className={cn(
              'min-w-0 flex-1 py-1 font-mono text-small break-all',
              shown ? 'text-text' : 'text-text-muted',
            )}
            data-testid={`env-value-${row.name}`}
          >
            {shown ? revealed : (row.masked ?? '—')}
          </code>
          {row.writable && row.isSet && row.secret ? (
            <Button
              size="icon-sm"
              variant="ghost"
              className="text-text-muted hover:text-text"
              disabled={!shown && (write.disabled || busy)}
              aria-label={
                shown
                  ? t('webSetAccess.envHideLabel', { name: row.name })
                  : t('webSetAccess.envRevealLabel', { name: row.name })
              }
              title={shown ? t('webSetAccess.hide') : t('webSetAccess.reveal')}
              onClick={() => (shown ? setRevealed(null) : setConfirmReveal(true))}
            >
              {revealing ? <Loader2 className="animate-spin" /> : shown ? <EyeOff /> : <Eye />}
            </Button>
          ) : null}
        </div>
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
            {row.isSet ? <Pencil /> : <Plus />}
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
          {row.isSet ? (
            <Button
              size="sm"
              variant="ghost"
              className="text-danger hover:bg-danger/10"
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
        <form onSubmit={save} className="anim-enter flex flex-wrap items-center gap-2">
          <label htmlFor={inputId} className="sr-only">
            {t('webSetAccess.envValueLabel', { name: row.name })}
          </label>
          <Input
            id={inputId}
            type={row.secret ? 'password' : 'text'}
            autoComplete="off"
            size="sm"
            className="max-w-md flex-1 font-mono"
            wrapperClassName="max-w-md flex-1"
            icon={row.secret ? <KeyRound /> : undefined}
            value={draft}
            autoFocus
            onChange={(e) => setDraft(e.target.value)}
          />
          <Button type="submit" size="sm" variant="primary" disabled={write.disabled || busy}>
            {set.isPending ? <Loader2 className="animate-spin" /> : <Check />}
            {t('webSetAccess.envSave')}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
            {t('webCommon.cancel')}
          </Button>
        </form>
      ) : null}

      {error ? (
        <p role="alert" className="flex items-center gap-1.5 text-small text-danger">
          <CircleAlert className="size-3.5 shrink-0" aria-hidden data-status-icon />
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
  const [valueShown, toggleValue] = useReveal()

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
          <div className="flex flex-col gap-1.5">
            <label htmlFor={nameId} className="font-medium">
              {t('webSetAccess.envAddName')}
            </label>
            <Input
              id={nameId}
              autoComplete="off"
              className="font-mono uppercase placeholder:normal-case"
              value={name}
              placeholder={t('webSetAccess.envAddNamePlaceholder')}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? `${nameId}-error` : undefined}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={valueId} className="font-medium">
              {t('webSetAccess.envAddValue')}
            </label>
            <SecretInput
              id={valueId}
              shown={valueShown}
              onToggle={toggleValue}
              className="font-mono"
              value={value}
              onChange={(e) => setValue(e.target.value)}
            />
          </div>
          {error ? (
            <p
              id={`${nameId}-error`}
              role="alert"
              className="flex items-center gap-1.5 text-small text-danger"
            >
              <CircleAlert className="size-3.5 shrink-0" aria-hidden data-status-icon />
              {error}
            </p>
          ) : null}
          <DialogFooter className="items-center">
            <GateReason gate={write} className="mr-auto" />
            <Button variant="outline" onClick={onClose}>
              {t('webCommon.cancel')}
            </Button>
            <Button type="submit" variant="primary" disabled={write.disabled || set.isPending}>
              {set.isPending ? <Loader2 className="animate-spin" /> : null}
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
      <LoadingRegion className="flex flex-col gap-5">
        <div className="flex gap-2">
          <Skeleton className="h-9 w-36" />
          <Skeleton className="h-9 w-24" />
        </div>
        <div className="grid grid-cols-3 gap-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-24 rounded-card" />
          ))}
        </div>
        <Skeleton className="h-9 w-full max-w-md" />
        <Skeleton className="h-64 rounded-card" />
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

      <div className="anim-stagger grid grid-cols-1 gap-3 sm:grid-cols-3">
        <StatCard
          className={STAT_CARD_DENSE}
          headingAs="p"
          icon={
            <IconTile>
              <CheckCircle2 />
            </IconTile>
          }
          label={t('webSetAccess.envStatSet')}
          value={t('webSetAccess.envStatSetValue', {
            set: summary.setCount,
            total: summary.totalCount,
          })}
        />
        <StatCard
          className={STAT_CARD_DENSE}
          headingAs="p"
          icon={
            <IconTile>
              <CircleDashed />
            </IconTile>
          }
          label={t('webSetAccess.envStatMissing')}
          value={summary.missingCount}
          tone={summary.missingCount ? 'warn' : 'default'}
        />
        <StatCard
          className={STAT_CARD_DENSE}
          headingAs="p"
          icon={
            <IconTile>
              <Layers />
            </IconTile>
          }
          label={t('webSetAccess.envStatShadowed')}
          value={summary.shadowedCount}
          tone={summary.shadowedCount ? 'warn' : 'default'}
        />
      </div>
      {listQuery.data?.envFilePath ? (
        <p className="-mt-2 flex flex-wrap items-center gap-1.5 text-small text-text-muted">
          <FileText className="size-3.5" aria-hidden />
          {t('webSetAccess.envFile')}{' '}
          <code className="set-key" title={listQuery.data.envFilePath}>
            {shortPath(listQuery.data.envFilePath)}
          </code>
        </p>
      ) : null}

      {summary.shadowedCount > 0 ? (
        <Notice role="status" tone="warn">
          <p className="font-medium">
            {t('webSetAccess.envShadowTitle', { count: summary.shadowedCount })}
          </p>
          <p className="text-small text-text-muted">{t('webSetAccess.envShadowBody')}</p>
        </Notice>
      ) : null}

      <div className="flex flex-wrap items-center gap-2" data-field-id="env.search">
        <Input
          type="search"
          icon={<Search />}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('webSetAccess.envSearch')}
          aria-label={t('webSetAccess.envSearch')}
          wrapperClassName="w-full max-w-xs"
        />
        <SegmentedControl
          kind="toggle"
          aria-label={t('webSetAccess.envFilter')}
          value={filter}
          onChange={setFilter}
          options={filterOptions().map((o) => ({ value: o.id, label: o.label }))}
        />
      </div>

      {groups.length === 0 ? (
        <EmptyState
          size="sm"
          className="set-card py-8"
          icon={<SearchX />}
          title={t('webSetAccess.envEmptyFiltered')}
        />
      ) : (
        groups.map((group) => {
          const { primary, rest } = splitGroupRows(group)
          const tailOpen = openTails.has(group.category)
          const visible = tailOpen ? [...primary, ...rest] : primary
          return (
            <section
              key={group.category}
              aria-label={group.label}
              className="set-list @container flex flex-col"
            >
              <h2 className="flex items-center gap-2 bg-surface-2/60 px-4 py-2.5 set-card-title">
                {group.label}
                <span className="ml-auto text-small font-normal text-text-muted tabular-nums">
                  {t('webSetAccess.envGroupCount', {
                    set: group.setCount,
                    total: group.rows.length,
                  })}
                </span>
              </h2>
              {visible.length ? (
                <ul role="list" className="flex flex-col divide-y divide-border">
                  {visible.map((row) => (
                    <EnvRow key={row.name} row={row} write={write} />
                  ))}
                </ul>
              ) : null}
              {rest.length > 0 ? (
                <Button
                  size="sm"
                  variant="ghost"
                  className="m-1.5 self-start text-text-muted"
                  aria-expanded={tailOpen}
                  onClick={() => toggleTail(group.category)}
                >
                  <ChevronRight
                    className={cn(
                      'transition-transform duration-[var(--duration-base)] ease-[var(--ease-out)]',
                      tailOpen && 'rotate-90',
                    )}
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
