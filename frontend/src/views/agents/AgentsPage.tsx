import './agents.css'
import { useEffect, useId, useState } from 'react'
import { useNavigate } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MessageSquareIcon, PencilIcon, PlusIcon, RefreshCwIcon, Trash2Icon } from 'lucide-react'
import { toast } from 'sonner'
import { AsciiField } from '@/components/AsciiField'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '@/components/ui/button'
import { useRpc } from '@/app/providers'
import {
  agentDisplay,
  agentStats,
  agentToForm,
  buildCreatePayload,
  buildUpdatePayload,
  isFormDirty,
  isNoOpUpdate,
  parseToolsInput,
  validateCreate,
  type AgentForm,
  type RawAgent,
} from './logic'

// agents.js:84-91 — the single read; refreshed imperatively (no legacy poll).
interface AgentsList {
  agents?: RawAgent[]
}

interface AgentsListError {
  code?: string
  message?: string
}

function toneClass(tone: 'ok' | 'info'): string {
  return tone === 'ok' ? 'tone-ok' : 'tone-info'
}

// ── Create / Edit dialog ─────────────────────────────────────────────────────
function AgentDialog({
  mode,
  seed,
  saving,
  onCancel,
  onCreate,
  onSave,
}: {
  mode: 'create' | 'edit'
  seed: AgentForm
  saving: boolean
  onCancel: () => void
  onCreate: (id: string, name: string) => void
  onSave: (initial: AgentForm, current: AgentForm) => void
}) {
  const [form, setForm] = useState<AgentForm>(seed)
  const [toolsText, setToolsText] = useState(seed.tools.join(', '))
  const [idError, setIdError] = useState<string | null>(null)
  const [showDiscard, setShowDiscard] = useState(false)
  const titleId = useId()
  const isCreate = mode === 'create'
  const idDisabled = !isCreate // id is never editable post-create (agents.js:324)

  // agents.js:272-275,307-312 — the edit form is dirty vs its seed (tools live
  // in the free-text field, so fold it into the comparison snapshot).
  const dirty = !isCreate && isFormDirty(seed, { ...form, tools: parseToolsInput(toolsText) })

  function set<K extends keyof AgentForm>(key: K, value: AgentForm[K]) {
    setForm((f) => ({ ...f, [key]: value }))
  }

  // agents.js:307-312,499-506 — a dirty edit prompts to discard on close;
  // create mode / a non-dirty edit closes immediately.
  function attemptClose() {
    if (saving) return
    if (dirty) {
      setShowDiscard(true)
      return
    }
    onCancel()
  }

  function submit(e: React.FormEvent) {
    e.preventDefault()
    if (isCreate) {
      const errors = validateCreate({ id: form.id, name: form.name })
      if (errors.id) {
        setIdError(errors.id)
        return
      }
      onCreate(form.id, form.name)
      return
    }
    onSave(seed, { ...form, tools: parseToolsInput(toolsText) })
  }

  return (
    <ModalShell
      role="dialog"
      labelledBy={titleId}
      onClose={attemptClose}
      overlayClassName="ag-modal__overlay"
      className="ag-modal panel"
    >
      <form className="ag-dialog" onSubmit={submit}>
        <header className="ag-dialog__head">
          <span className="t-label">Control · Agents</span>
          <h3 id={titleId} className="ag-dialog__title">
            {isCreate ? 'New agent' : `Edit agent: ${seed.id}`}
          </h3>
        </header>

        <div className="ag-dialog__body">
          <label className="ag-field">
            <span className="t-label">Agent ID</span>
            <input
              className="ag-input"
              autoComplete="off"
              disabled={idDisabled}
              value={form.id}
              placeholder="e.g. data-analyst"
              aria-invalid={idError ? true : undefined}
              onChange={(e) => {
                set('id', e.target.value)
                if (idError) setIdError(null)
              }}
            />
            {idError ? (
              <span className="ag-field__error" role="alert">
                {idError}
              </span>
            ) : null}
          </label>

          <label className="ag-field">
            <span className="t-label">Display name</span>
            <input
              className="ag-input"
              autoComplete="off"
              value={form.name}
              placeholder="Defaults to ID"
              onChange={(e) => set('name', e.target.value)}
            />
          </label>

          {isCreate ? (
            <p className="ag-dialog__hint">
              Created agents inherit the global default model. Add tools and other capabilities
              after creating from the agent&apos;s Edit dialog.
            </p>
          ) : (
            <>
              <label className="ag-field">
                <span className="t-label">Description</span>
                <input
                  className="ag-input"
                  autoComplete="off"
                  value={form.description}
                  placeholder="A short one-liner"
                  onChange={(e) => set('description', e.target.value)}
                />
              </label>

              <details
                className="ag-dialog__advanced"
                open={Boolean(
                  form.workspace || form.agentDir || form.tools.length || !form.enabled,
                )}
              >
                <summary>Capabilities · Advanced</summary>
                <label className="ag-field">
                  <span className="t-label">Tools (comma-separated)</span>
                  <input
                    className="ag-input"
                    autoComplete="off"
                    value={toolsText}
                    placeholder="Leave blank to inherit defaults"
                    onChange={(e) => setToolsText(e.target.value)}
                  />
                </label>
                <label className="ag-field">
                  <span className="t-label">Workspace</span>
                  <input
                    className="ag-input"
                    autoComplete="off"
                    value={form.workspace}
                    placeholder="Leave blank to use the default path"
                    onChange={(e) => set('workspace', e.target.value)}
                  />
                </label>
                <label className="ag-field">
                  <span className="t-label">Agent dir</span>
                  <input
                    className="ag-input"
                    autoComplete="off"
                    value={form.agentDir}
                    placeholder="Optional"
                    onChange={(e) => set('agentDir', e.target.value)}
                  />
                </label>
                <label className="ag-field ag-field--inline">
                  <input
                    type="checkbox"
                    checked={form.enabled}
                    onChange={(e) => set('enabled', e.target.checked)}
                  />
                  <span>Enabled</span>
                </label>
              </details>
            </>
          )}
        </div>

        <footer className="ag-dialog__foot">
          <Button type="button" variant="ghost" disabled={saving} onClick={attemptClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={saving}>
            {isCreate ? 'Create agent' : 'Save changes'}
          </Button>
        </footer>
      </form>

      {showDiscard ? (
        <ConfirmDialog
          title="Discard unsaved changes?"
          body="You have unsaved edits. Closing now will lose them."
          confirmLabel="Discard"
          cancelLabel="Keep editing"
          onCancel={() => setShowDiscard(false)}
          onConfirm={() => {
            setShowDiscard(false)
            onCancel()
          }}
        />
      ) : null}
    </ModalShell>
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
      overlayClassName="ag-modal__overlay"
      className="ag-modal panel"
    >
      <div className="ag-dialog ag-confirm">
        <header className="ag-dialog__head">
          <h3 id={titleId} className="ag-dialog__title">
            {title}
          </h3>
        </header>
        <p id={bodyId} className="ag-confirm__body">
          {body}
        </p>
        <footer className="ag-dialog__foot">
          <Button type="button" variant="ghost" disabled={busy} onClick={onCancel}>
            {cancelLabel}
          </Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </footer>
      </div>
    </ModalShell>
  )
}

// ── Agent card ───────────────────────────────────────────────────────────────
function AgentCard({
  agent,
  busy,
  onChat,
  onEdit,
  onCustomize,
  onDelete,
}: {
  agent: RawAgent
  busy: boolean
  onChat: (id: string) => void
  onEdit: (agent: RawAgent) => void
  onCustomize: (id: string) => void
  onDelete: (id: string) => void
}) {
  const d = agentDisplay(agent)
  return (
    <article className={`panel ag-card ${toneClass(d.tone)}`} aria-label={`Agent ${d.id}`}>
      <header className="ag-card__head">
        <span
          className={`ag-card__dot tone-${d.tone === 'ok' ? 'ok' : 'info'}`}
          aria-hidden="true"
        />
        <span className="ag-card__id" title={d.id}>
          {d.id}
        </span>
        <span className={`ag-card__type t-data ${toneClass(d.tone)}`}>{d.type}</span>
      </header>

      <div className="ag-card__name">{d.name}</div>
      {d.description ? <p className="ag-card__desc">{d.description}</p> : null}

      <dl className="ag-card__meta">
        {d.model ? (
          <div>
            <dt className="t-label">Model</dt>
            <dd className="t-data ag-mono">{d.model}</dd>
          </div>
        ) : null}
        {d.toolCount ? (
          <div>
            <dt className="t-label">Tools</dt>
            <dd className="t-data">{d.toolCount}</dd>
          </div>
        ) : null}
        {d.skillCount ? (
          <div>
            <dt className="t-label">Skills</dt>
            <dd className="t-data">{d.skillCount}</dd>
          </div>
        ) : null}
      </dl>

      {d.toolChips.length ? (
        <div className="ag-card__chips">
          <span className="ag-card__chips-label t-label">Tools</span>
          {d.toolChips.map((t) => (
            <span key={t} className="ag-chip t-data">
              {t}
            </span>
          ))}
          {d.overflow ? <span className="ag-chip ag-chip--dim t-data">+{d.overflow}</span> : null}
        </div>
      ) : null}

      <footer className="ag-card__actions">
        <Button type="button" size="sm" variant="outline" onClick={() => onChat(d.id)}>
          <MessageSquareIcon />
          <span>Chat</span>
        </Button>
        {d.isBuiltin ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            title="Use as a starting point for a new agent"
            onClick={() => onCustomize(d.id)}
          >
            <PlusIcon />
            <span>Customize</span>
          </Button>
        ) : (
          <>
            <Button type="button" size="sm" variant="outline" onClick={() => onEdit(agent)}>
              <PencilIcon />
              <span>Edit</span>
            </Button>
            <Button
              type="button"
              size="sm"
              variant="destructive"
              disabled={busy}
              onClick={() => onDelete(d.id)}
            >
              <Trash2Icon />
              <span>Delete</span>
            </Button>
          </>
        )}
      </footer>
    </article>
  )
}

function StatTile({
  label,
  value,
  hint,
  hero,
}: {
  label: string
  value: React.ReactNode
  hint: React.ReactNode
  hero?: boolean
}) {
  return (
    <div className={`ag-stat${hero ? ' ag-stat--hero' : ''}`} aria-label={label}>
      <span className="ag-stat__label t-label">{label}</span>
      <strong className="ag-stat__value t-data">{value}</strong>
      <span className="ag-stat__hint">{hint}</span>
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────
type DialogState =
  | { kind: 'none' }
  | { kind: 'create'; seed: AgentForm }
  | { kind: 'edit'; seed: AgentForm }
  | { kind: 'delete'; id: string }

const EMPTY_FORM: AgentForm = {
  id: '',
  name: '',
  description: '',
  tools: [],
  workspace: '',
  agentDir: '',
  enabled: true,
}

export function AgentsPage() {
  const rpc = useRpc()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [dialog, setDialog] = useState<DialogState>({ kind: 'none' })

  useEffect(() => {
    document.title = 'Agents - AgentOS Control'
  }, [])

  const agentsQuery = useQuery<RawAgent[]>({
    queryKey: ['agents'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<AgentsList>('agents.list', {})
      return data.agents ?? []
    },
    refetchOnWindowFocus: false,
  })

  // agents.js:90 — load-failure toast (stable id so repeats dedupe).
  useEffect(() => {
    if (agentsQuery.isError) {
      const err = agentsQuery.error
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to load agents: ' + message, { id: 'agents-load-err' })
    }
  }, [agentsQuery.isError, agentsQuery.error])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['agents'] })

  // agents.js:224-240 — create; agent.exists → warn (not error).
  const createMutation = useMutation({
    mutationFn: (input: { id: string; name: string }) =>
      rpc.call('agents.create', buildCreatePayload(input)),
    onSuccess: (_data, input) => {
      toast.success('Agent created: ' + input.id.trim(), { id: 'agents-create' })
      setDialog({ kind: 'none' })
      void invalidate()
    },
    onError: (err, input) => {
      const e = err as AgentsListError
      if (e.code === 'agent.exists') {
        toast.warning(`Agent "${input.id.trim()}" already exists`, { id: 'agents-create' })
      } else {
        toast.error('Failed to create agent: ' + (e.message || String(err)), {
          id: 'agents-create-err',
        })
      }
    },
  })

  // agents.js:426-462 — update; friendly messages for not_found / builtin_immutable.
  const updateMutation = useMutation({
    mutationFn: (vars: { id: string; payload: Record<string, unknown> }) =>
      rpc.call('agents.update', vars.payload),
    onSuccess: (_data, vars) => {
      toast.success('Agent updated: ' + vars.id, { id: 'agents-update' })
      setDialog({ kind: 'none' })
      void invalidate()
    },
    onError: (err, vars) => {
      const e = err as AgentsListError
      let friendly = 'Failed to save: ' + (e.message || String(err))
      if (e.code === 'agent.not_found') friendly = `Agent "${vars.id}" no longer exists.`
      if (e.code === 'agent.builtin_immutable')
        friendly = `"${vars.id}" is a built-in agent and cannot be modified.`
      toast.error(friendly, { id: 'agents-update-err' })
    },
  })

  // agents.js:508-520 — delete after confirmation.
  const deleteMutation = useMutation({
    mutationFn: (id: string) => rpc.call('agents.delete', { id }),
    onSuccess: (_data, id) => {
      toast.success('Agent deleted: ' + id, { id: 'agents-delete' })
      setDialog({ kind: 'none' })
      void invalidate()
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to delete agent: ' + message, { id: 'agents-delete-err' })
    },
  })

  const agents = agentsQuery.data ?? []
  const stats = agentStats(agents)
  const mutating = createMutation.isPending || updateMutation.isPending || deleteMutation.isPending

  // agents.js:242-256 — Customize seeds the create dialog with `<id>-copy`.
  const openCustomize = (builtinId: string) => {
    setDialog({
      kind: 'create',
      seed: { ...EMPTY_FORM, id: (builtinId || 'main') + '-copy', name: builtinId + ' (copy)' },
    })
  }

  return (
    <div className="ag-stage">
      <header className="ag-stage__header">
        <AsciiField />
        <div className="ag-stage__title-block">
          <span className="t-label">Control · Agents</span>
          <h2 className="t-display">Agents</h2>
          <p className="ag-stage__subtitle">
            Custom personalities and skill sets you can chat with.
          </p>
        </div>
        <div className="ag-stage__actions">
          <Button
            variant="outline"
            title="Refresh"
            className="text-xs uppercase tracking-[0.14em]"
            onClick={() => void invalidate()}
          >
            <RefreshCwIcon />
            <span>Refresh</span>
          </Button>
          <Button
            className="text-xs uppercase tracking-[0.14em]"
            onClick={() => setDialog({ kind: 'create', seed: { ...EMPTY_FORM } })}
          >
            <PlusIcon />
            <span>New agent</span>
          </Button>
        </div>
      </header>

      <section className="ag-stats" aria-label="Agents summary">
        <StatTile
          label="Total agents"
          hero
          value={stats.total}
          hint={
            [
              stats.builtins ? `${stats.builtins} built-in` : '',
              stats.customs ? `${stats.customs} custom` : '',
            ]
              .filter(Boolean)
              .join(' · ') || 'none configured'
          }
        />
        <StatTile
          label="Models in use"
          value={stats.models || '—'}
          hint={stats.models ? 'distinct models' : 'unset'}
        />
        <StatTile label="Tools wired" value={stats.tools} hint="across all agents" />
      </section>

      <section className="ag-list">
        <div className="ag-list__head">
          <h3 className="ag-list__title t-label">
            Configured agents{' '}
            {agents.length ? <span className="ag-list__count t-data">{agents.length}</span> : null}
          </h3>
        </div>

        {agents.length === 0 ? (
          <div className="ag-empty">
            <div className="ag-empty__title">No agents configured.</div>
            <p className="ag-empty__msg">
              Use <strong>New agent</strong> above to add one. The default <code>main</code> agent
              is always available.
            </p>
          </div>
        ) : (
          <div className="ag-cards">
            {agents.map((agent, i) => (
              <AgentCard
                key={String(agent.id || agent.name || i)}
                agent={agent}
                busy={mutating}
                onChat={(id) => navigate('/chat?agent=' + encodeURIComponent(id))}
                onEdit={(a) => setDialog({ kind: 'edit', seed: agentToForm(a) })}
                onCustomize={openCustomize}
                onDelete={(id) => setDialog({ kind: 'delete', id })}
              />
            ))}
          </div>
        )}
      </section>

      {dialog.kind === 'create' || dialog.kind === 'edit' ? (
        <AgentDialog
          // Remount on a new seed so the form state resets when switching
          // between create / customize / a different agent's edit.
          key={dialog.kind + ':' + dialog.seed.id}
          mode={dialog.kind}
          seed={dialog.seed}
          saving={createMutation.isPending || updateMutation.isPending}
          onCancel={() => setDialog({ kind: 'none' })}
          onCreate={(id, name) => createMutation.mutate({ id, name })}
          onSave={(initial, current) => {
            const payload = buildUpdatePayload(initial, current)
            // agents.js:432-437 — no-op save: nothing changed → skip the RPC,
            // toast 'Nothing to save', and keep the dialog open.
            if (isNoOpUpdate(payload)) {
              toast.info('Nothing to save', { id: 'agents-update' })
              return
            }
            updateMutation.mutate({ id: current.id, payload })
          }}
        />
      ) : null}

      {dialog.kind === 'delete' ? (
        <ConfirmDialog
          title="Delete agent"
          body={
            <>
              Delete agent <strong>{dialog.id}</strong>? Existing chats with this agent will keep
              working but become unmanaged.
            </>
          }
          confirmLabel="Delete agent"
          busy={deleteMutation.isPending}
          onCancel={() => setDialog({ kind: 'none' })}
          onConfirm={() => deleteMutation.mutate(dialog.id)}
        />
      ) : null}
    </div>
  )
}
