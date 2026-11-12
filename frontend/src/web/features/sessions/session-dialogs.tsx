import { useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useId, useState } from 'react'
import { useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { blockedBecause, gate, requireConnection, type Gate } from '@/lib/disabled-reason'
import type { RpcState } from '@/lib/ws-rpc'
import { Button } from '@/web/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { Input, Select } from '@/web/components/ui/input'
import {
  AGENTS_QUERY_KEY,
  createSessionOptimistic,
  PROJECTS_QUERY_KEY,
  SESSIONS_QUERY_KEY,
} from '@/web/shell/session-data'
import { sessionKeyToPath } from '@/web/shell/session-url'

// The Sessions page's two dialogs: move sessions to a project, and start a
// new session (creating its agent first when asked).

const NEW_AGENT = '__new__'
const AGENT_ID = /^[a-z0-9_-]+$/

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function errorCode(error: unknown): string {
  return (error as { code?: string } | null)?.code ?? ''
}

// ── Move to project ──────────────────────────────────────────────────────────

export function MoveDialog({
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

export function NewSessionDialog({
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
