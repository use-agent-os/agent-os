import { useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useId, useState } from 'react'
import { useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { blockedBecause, gate, requireConnection } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import { projectId as projectIdOf, type RawProject } from '@/views/projects/logic'
import { Button } from '@/web/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { Input, Select, Textarea } from '@/web/components/ui/input'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { errorMessage, PROJECT_KNOWLEDGE_MAX, PROJECT_NAME_MAX, ProjectTile } from './project-ui'

/** New project: name (its tile previews as you type), owning agent, knowledge. */
export function CreateProjectDialog({
  agents,
  onClose,
}: {
  agents: { id: string; name: string }[]
  onClose: () => void
}) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [agentId, setAgentId] = useState(() =>
    agents.some((a) => a.id === 'main') || agents.length === 0 ? 'main' : agents[0]!.id,
  )
  const [knowledge, setKnowledge] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const nameId = useId()
  const agentFieldId = useId()
  const knowledgeId = useId()
  const reasonId = useId()
  const createGate = gate(
    requireConnection(useDisplayedConnection()),
    !name.trim() ? blockedBecause(t('webPagesWork.nameRequired')) : null,
  )

  const submit = async () => {
    if (createGate.disabled) return
    setBusy(true)
    setError(null)
    try {
      const res = await rpc.call<{ project?: RawProject }>('projects.create', {
        agentId,
        name: name.trim(),
        knowledge,
      })
      toast.success(t('webPagesWork.projectCreated'))
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
      onClose()
      const id = res?.project ? projectIdOf(res.project) : ''
      if (id) navigate(`/projects/${encodeURIComponent(id)}`)
    } catch (e) {
      setError(t('webPagesWork.projectCreateFailed', { message: errorMessage(e) }))
    } finally {
      setBusy(false)
    }
  }

  const agentChoices = agents.length ? agents : [{ id: 'main', name: 'main' }]
  const preview = name.trim() || t('webPagesWork.projectNamePlaceholder')
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
            <DialogTitle>{t('webPagesWork.createProjectTitle')}</DialogTitle>
            <DialogDescription>{t('webPagesWork.createProjectBody')}</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={nameId} className="text-label">
              {t('webPagesWork.projectName')}
            </label>
            <div className="flex items-center gap-3">
              <ProjectTile
                name={preview}
                className={cn(
                  'transition-opacity duration-[var(--duration-base)]',
                  !name.trim() && 'opacity-45',
                )}
              />
              <Input
                id={nameId}
                autoFocus
                value={name}
                maxLength={PROJECT_NAME_MAX}
                placeholder={t('webPagesWork.projectNamePlaceholder')}
                onChange={(e) => setName(e.target.value)}
                className="flex-1"
              />
            </div>
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={agentFieldId} className="text-label">
              {t('webPagesWork.projectAgent')}
            </label>
            <Select id={agentFieldId} value={agentId} onChange={(e) => setAgentId(e.target.value)}>
              {agentChoices.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </Select>
            <span className="text-small text-text-muted">{t('webPagesWork.projectAgentHint')}</span>
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={knowledgeId} className="text-label">
              {t('webPagesWork.knowledge')}
            </label>
            <Textarea
              id={knowledgeId}
              rows={5}
              className="font-mono text-small"
              value={knowledge}
              maxLength={PROJECT_KNOWLEDGE_MAX}
              placeholder={t('webPagesWork.knowledgePlaceholder')}
              onChange={(e) => setKnowledge(e.target.value)}
            />
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
            {createGate.reason ? (
              <span id={reasonId} className="text-small text-text-muted sm:mr-auto">
                {createGate.reason}
              </span>
            ) : null}
            <Button variant="outline" disabled={busy} onClick={onClose}>
              {t('webPagesWork.cancel')}
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={busy || createGate.disabled}
              aria-describedby={createGate.reason ? reasonId : undefined}
            >
              {busy ? <Loader2 className="animate-spin" /> : null}
              {t('webPagesWork.createProject')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
