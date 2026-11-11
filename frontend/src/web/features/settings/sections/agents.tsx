import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, CircleAlert, Copy, Lock, MessageSquare, Pencil, Plus, Trash2 } from 'lucide-react'
import { useId, useState } from 'react'
import { Link } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webSetAccess'
import { gate, requireConnection, type Gate } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  agentDisplay,
  agentToForm,
  buildCreatePayload,
  buildUpdatePayload,
  isFormDirty,
  isNoOpUpdate,
  parseToolsInput,
  validateCreate,
  type AgentForm,
  type RawAgent,
} from '@/views/agents/logic'
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
import { Button, buttonVariants } from '@/web/components/ui/button'
import { Card } from '@/web/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { Input } from '@/web/components/ui/input'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { Switch } from '@/web/components/ui/switch'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useRegisterFields } from '../fields'
import { Notice } from '../ui'

// Settings › Agents (overhaul-control-ui 7.5, Appendix A "Agents"): cards,
// create, edit with a dirty-discard prompt, Customize (clone a built-in) and
// delete. Built-ins are locked.

// Under the shell's ['agents', …] prefix so a write refreshes its switcher too.
const AGENTS_KEY = ['agents', 'settings'] as const

interface RpcError {
  code?: string
  message?: string
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function useWriteGate(): Gate {
  return gate(requireConnection(useDisplayedConnection()))
}

function GateReason({ gate: g, className }: { gate: Gate; className?: string }) {
  return g.reason ? <p className={cn('text-small text-text-muted', className)}>{g.reason}</p> : null
}

const EMPTY_FORM: AgentForm = {
  id: '',
  name: '',
  description: '',
  tools: [],
  workspace: '',
  agentDir: '',
  enabled: true,
}

function TextField({
  label,
  value,
  onChange,
  placeholder,
  disabled,
  error,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
  disabled?: boolean
  error?: string | null
}) {
  const id = useId()
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="font-medium">
        {label}
      </label>
      <Input
        id={id}
        className="disabled:opacity-50"
        autoComplete="off"
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : undefined}
        onChange={(e) => onChange(e.target.value)}
      />
      {error ? (
        <p
          id={`${id}-error`}
          role="alert"
          className="flex items-center gap-1.5 text-small text-danger"
        >
          <CircleAlert className="size-3.5 shrink-0" aria-hidden data-status-icon />
          {error}
        </p>
      ) : null}
    </div>
  )
}

// ── Create / edit dialog ────────────────────────────────────────────────────

function AgentDialog({
  mode,
  seed,
  write,
  onClose,
}: {
  mode: 'create' | 'edit'
  seed: AgentForm
  write: Gate
  onClose: () => void
}) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const [form, setForm] = useState<AgentForm>(seed)
  const [toolsText, setToolsText] = useState(seed.tools.join(', '))
  const [idError, setIdError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const isCreate = mode === 'create'
  const current = { ...form, tools: parseToolsInput(toolsText) }
  const dirty = !isCreate && isFormDirty(seed, current)

  const set = <K extends keyof AgentForm>(key: K, value: AgentForm[K]) => {
    setForm((f) => ({ ...f, [key]: value }))
    setNotice(null)
  }

  const done = () => {
    void queryClient.invalidateQueries({ queryKey: ['agents'] })
    onClose()
  }

  const create = useMutation({
    mutationFn: () => rpc.call('agents.create', buildCreatePayload(form)),
    onMutate: () => setError(null),
    onSuccess: () => {
      toast.success(t('webSetAccess.agCreated', { id: form.id.trim() }))
      done()
    },
    onError: (e) => {
      const err = e as RpcError
      setError(
        err.code === 'agent.exists'
          ? t('webSetAccess.agExists', { id: form.id.trim() })
          : t('webSetAccess.agSaveFailed', { message: err.message || errorMessage(e) }),
      )
    },
  })

  const update = useMutation({
    mutationFn: (payload: Record<string, unknown>) => rpc.call('agents.update', payload),
    onMutate: () => setError(null),
    onSuccess: () => {
      toast.success(t('webSetAccess.agUpdated', { id: seed.id }))
      done()
    },
    onError: (e) => {
      const err = e as RpcError
      setError(
        err.code === 'agent.not_found'
          ? t('webSetAccess.agNotFound', { id: seed.id })
          : err.code === 'agent.builtin_immutable'
            ? t('webSetAccess.agBuiltinImmutable', { id: seed.id })
            : t('webSetAccess.agSaveFailed', { message: err.message || errorMessage(e) }),
      )
    },
  })
  const saving = create.isPending || update.isPending

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (isCreate) {
      const errors = validateCreate({ id: form.id, name: form.name })
      if (errors.id) {
        setIdError(errors.id)
        return
      }
      create.mutate()
      return
    }
    const payload = buildUpdatePayload(seed, current)
    if (isNoOpUpdate(payload)) setNotice(t('webSetAccess.agNothingToSave'))
    else update.mutate(payload)
  }

  const requestClose = () => {
    if (saving) return
    if (dirty) setConfirmDiscard(true)
    else onClose()
  }

  return (
    <>
      <Dialog open onOpenChange={(open) => (open ? undefined : requestClose())}>
        <DialogContent>
          <form onSubmit={submit} className="flex flex-col gap-3.5">
            <DialogHeader>
              <DialogTitle>
                {isCreate
                  ? t('webSetAccess.agCreateTitle')
                  : t('webSetAccess.agEditTitle', { id: seed.id })}
              </DialogTitle>
              {isCreate ? (
                <DialogDescription>{t('webSetAccess.agCreateHint')}</DialogDescription>
              ) : null}
            </DialogHeader>
            <TextField
              label={t('webSetAccess.agFieldId')}
              value={form.id}
              disabled={!isCreate || saving}
              placeholder={t('webSetAccess.agFieldIdPlaceholder')}
              error={idError}
              onChange={(v) => {
                set('id', v)
                setIdError(null)
              }}
            />
            <TextField
              label={t('webSetAccess.agFieldName')}
              value={form.name}
              disabled={saving}
              onChange={(v) => set('name', v)}
            />
            {isCreate ? null : (
              <>
                <TextField
                  label={t('webSetAccess.agFieldDescription')}
                  value={form.description}
                  disabled={saving}
                  onChange={(v) => set('description', v)}
                />
                <TextField
                  label={t('webSetAccess.agFieldTools')}
                  value={toolsText}
                  disabled={saving}
                  placeholder={t('webSetAccess.agFieldToolsPlaceholder')}
                  onChange={(v) => {
                    setToolsText(v)
                    setNotice(null)
                  }}
                />
                <TextField
                  label={t('webSetAccess.agFieldWorkspace')}
                  value={form.workspace}
                  disabled={saving}
                  onChange={(v) => set('workspace', v)}
                />
                <TextField
                  label={t('webSetAccess.agFieldAgentDir')}
                  value={form.agentDir}
                  disabled={saving}
                  onChange={(v) => set('agentDir', v)}
                />
                <label className="flex items-center justify-between gap-3 rounded-control border border-border bg-surface-2 px-3 py-2.5">
                  <span className="font-medium">{t('webSetAccess.agFieldEnabled')}</span>
                  <Switch
                    checked={form.enabled}
                    disabled={saving}
                    onCheckedChange={(v) => set('enabled', v)}
                  />
                </label>
              </>
            )}
            {error ? (
              <Notice role="alert" tone="danger" className="anim-fade-in text-small">
                {error}
              </Notice>
            ) : notice ? (
              <Notice role="status" tone="info" className="anim-fade-in text-small">
                {notice}
              </Notice>
            ) : null}
            <DialogFooter className="items-center">
              <GateReason gate={write} className="mr-auto" />
              <Button variant="outline" disabled={saving} onClick={requestClose}>
                {t('webCommon.cancel')}
              </Button>
              <Button type="submit" variant="primary" disabled={saving || write.disabled}>
                {isCreate ? t('webSetAccess.agCreate') : t('webSetAccess.agSave')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <AlertDialog open={confirmDiscard} onOpenChange={setConfirmDiscard}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('webSetAccess.discardTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('webSetAccess.discardBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webSetAccess.discardKeep')}</AlertDialogCancel>
            <AlertDialogAction destructive onClick={onClose}>
              {t('webSetAccess.discardConfirm')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

// ── Card ────────────────────────────────────────────────────────────────────

function AgentCard({
  agent,
  write,
  onEdit,
  onCustomize,
  onDelete,
}: {
  agent: RawAgent
  write: Gate
  onEdit: () => void
  onCustomize: () => void
  onDelete: () => void
}) {
  const d = agentDisplay(agent)
  return (
    <li>
      <Card asChild className="flex h-full flex-col gap-3">
        <article aria-label={t('webSetAccess.agCardLabel', { id: d.id })}>
          <header className="flex items-start gap-3">
            <span
              aria-hidden
              className={cn(
                'grid size-9 shrink-0 place-items-center rounded-control shadow-highlight',
                d.isBuiltin ? 'bg-surface-2 text-text-muted' : 'bg-accent-tint text-accent',
              )}
            >
              <Bot className="size-4" />
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate font-medium">{d.name}</p>
              <p className="truncate font-mono text-small text-text-muted">{d.id}</p>
            </div>
            <Badge tone={d.isBuiltin ? 'neutral' : 'info'}>
              {d.isBuiltin ? <Lock aria-hidden data-status-icon /> : null}
              {d.isBuiltin ? t('webSetAccess.agBuiltin') : t('webSetAccess.agCustom')}
            </Badge>
          </header>
          {d.description ? (
            <p className="line-clamp-3 text-small text-pretty text-text-muted">{d.description}</p>
          ) : null}
          <dl className="flex flex-wrap gap-x-4 gap-y-1 text-small">
            {d.model ? (
              <div className="flex gap-1">
                <dt className="text-text-muted">{t('webSetAccess.agModel')}</dt>
                <dd className="font-mono">{d.model}</dd>
              </div>
            ) : null}
            <div className="flex gap-1">
              <dt className="text-text-muted">{t('webSetAccess.agTools')}</dt>
              <dd className="font-mono tabular-nums">{d.toolCount}</dd>
            </div>
            <div className="flex gap-1">
              <dt className="text-text-muted">{t('webSetAccess.agSkills')}</dt>
              <dd className="font-mono tabular-nums">{d.skillCount}</dd>
            </div>
          </dl>
          {d.toolChips.length ? (
            <ul role="list" aria-label={t('webSetAccess.agTools')} className="flex flex-wrap gap-1">
              {d.toolChips.map((tool) => (
                <li key={tool} className="set-key">
                  {tool}
                </li>
              ))}
              {d.overflow ? (
                <li className="px-1.5 py-0.5 text-small text-text-muted tabular-nums">
                  +{d.overflow}
                </li>
              ) : null}
            </ul>
          ) : null}
          <footer className="mt-auto flex flex-wrap items-center gap-1.5 border-t border-border pt-3">
            <Link
              to={`/new?agent=${encodeURIComponent(d.id)}`}
              className={buttonVariants({ size: 'sm', variant: 'outline' })}
            >
              <MessageSquare />
              {t('webSetAccess.agChat')}
            </Link>
            {d.isBuiltin ? (
              <>
                <Button size="sm" variant="outline" disabled={write.disabled} onClick={onCustomize}>
                  <Copy />
                  {t('webSetAccess.agCustomize')}
                </Button>
                <span className="ml-auto text-small text-text-muted">
                  {t('webSetAccess.agLocked')}
                </span>
              </>
            ) : (
              <>
                <Button size="sm" variant="outline" disabled={write.disabled} onClick={onEdit}>
                  <Pencil />
                  {t('webSetAccess.agEdit')}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="ml-auto text-danger hover:bg-danger/10"
                  disabled={write.disabled}
                  onClick={onDelete}
                >
                  <Trash2 />
                  {t('webSetAccess.agDelete')}
                </Button>
              </>
            )}
          </footer>
        </article>
      </Card>
    </li>
  )
}

// ── Section ─────────────────────────────────────────────────────────────────

type DialogState =
  { kind: 'none' } | { kind: 'create'; seed: AgentForm } | { kind: 'edit'; seed: AgentForm }

export function AgentsSection() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const write = useWriteGate()
  const [dialog, setDialog] = useState<DialogState>({ kind: 'none' })
  const [deleting, setDeleting] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  useRegisterFields('agents', [{ id: 'agents.create', label: t('webSetAccess.agNew') }])

  const agentsQuery = useQuery<RawAgent[]>({
    queryKey: AGENTS_KEY,
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<{ agents?: RawAgent[] }>('agents.list', {})
      return data?.agents ?? []
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => rpc.call('agents.delete', { id }),
    onMutate: () => setDeleteError(null),
    onSuccess: (_data, id) => {
      toast.success(t('webSetAccess.agDeleted', { id }))
      void queryClient.invalidateQueries({ queryKey: ['agents'] })
    },
    onError: (e, id) =>
      setDeleteError(t('webSetAccess.agDeleteFailed', { id, message: errorMessage(e) })),
  })

  // Customize seeds the create dialog with a copy of the built-in's id.
  const customize = (id: string) =>
    setDialog({
      kind: 'create',
      seed: {
        ...EMPTY_FORM,
        id: `${id || 'main'}-copy`,
        name: t('webSetAccess.agCopyName', { id }),
      },
    })

  const agents = agentsQuery.data ?? []

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3" data-field-id="agents.create">
        <Button
          variant="primary"
          disabled={write.disabled}
          onClick={() => setDialog({ kind: 'create', seed: { ...EMPTY_FORM } })}
        >
          <Plus />
          {t('webSetAccess.agNew')}
        </Button>
        <GateReason gate={write} />
      </div>

      {deleteError ? (
        <Notice role="alert" tone="danger" className="anim-fade-in text-small">
          {deleteError}
        </Notice>
      ) : null}

      {agentsQuery.isPending ? (
        <LoadingRegion className="grid gap-3 sm:grid-cols-2">
          <Skeleton className="h-44 rounded-card" />
          <Skeleton className="h-44 rounded-card" />
        </LoadingRegion>
      ) : agentsQuery.isError ? (
        <ErrorState
          title={t('webSetAccess.agLoadFailed')}
          message={errorMessage(agentsQuery.error)}
          onRetry={() => void agentsQuery.refetch()}
        />
      ) : agents.length === 0 ? (
        <EmptyState
          icon={<Bot />}
          title={t('webSetAccess.agEmptyTitle')}
          description={t('webSetAccess.agEmptyBody')}
        />
      ) : (
        <ul role="list" className="anim-stagger grid gap-3 sm:grid-cols-2">
          {agents.map((agent, i) => {
            const id = agentDisplay(agent).id
            return (
              <AgentCard
                key={String(agent.id || agent.name || i)}
                agent={agent}
                write={write}
                onEdit={() => setDialog({ kind: 'edit', seed: agentToForm(agent) })}
                onCustomize={() => customize(id)}
                onDelete={() => setDeleting(id)}
              />
            )
          })}
        </ul>
      )}

      {dialog.kind !== 'none' ? (
        <AgentDialog
          key={`${dialog.kind}:${dialog.seed.id}`}
          mode={dialog.kind}
          seed={dialog.seed}
          write={write}
          onClose={() => setDialog({ kind: 'none' })}
        />
      ) : null}

      <AlertDialog
        open={deleting !== null}
        onOpenChange={(open) => (open ? null : setDeleting(null))}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('webSetAccess.agDeleteTitle', { id: deleting ?? '' })}
            </AlertDialogTitle>
            <AlertDialogDescription>{t('webSetAccess.agDeleteBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webCommon.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              destructive
              disabled={write.disabled}
              onClick={() => {
                if (deleting) remove.mutate(deleting)
                setDeleting(null)
              }}
            >
              {t('webSetAccess.agDelete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
