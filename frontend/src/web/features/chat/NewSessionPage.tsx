import { useQuery, useQueryClient } from '@tanstack/react-query'
import { FileText, Loader2, MessageSquarePlus, Paperclip, X } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { blockedBecause, gate, requireConnection } from '@/lib/disabled-reason'
import { newIdempotencyKey } from '@/lib/ws-rpc'
import { useAttachments } from '@/views/chat/Attachments'
import { outgoingAttachment, readPromptFromUrl } from '@/views/chat/logic'
import { projectId, type RawProject } from '@/views/projects/logic'
import { Button } from '@/web/components/ui/button'
import { Select } from '@/web/components/ui/input'
import { ErrorState } from '@/web/components/ui/states'
import { createSessionOptimistic, useAgents, useProjectNames } from '@/web/shell/session-data'
import { sessionKeyToPath } from '@/web/shell/session-url'
import { useShell } from '@/web/shell/shell-store'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import '@/web/styles/chat.css'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** The agent each project belongs to: a chat in a project starts under it. */
function useProjectOwners() {
  const rpc = useRpc()
  return useQuery({
    queryKey: ['projects', 'list', 'owners'],
    queryFn: async (): Promise<Map<string, string>> => {
      await rpc.waitForConnection()
      const data = await rpc.call<{ projects?: RawProject[] }>('projects.list', {})
      const owners = new Map<string, string>()
      for (const p of data?.projects ?? []) {
        const owner = String(p.agent_id || p.agentId || '')
        if (projectId(p) && owner) owners.set(projectId(p), owner)
      }
      return owners
    },
    staleTime: 30_000,
  })
}

/**
 * `/new` (chat spec "New session page"): agent and project pickers and a first
 * message with attachments. Start creates the session, sends the message and
 * opens it; on failure the draft and attachments stay and the error shows.
 */
export function NewSessionPage() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const agents = useAgents()
  const projects = useProjectNames()
  const owners = useProjectOwners()
  const activeAgent = useShell((s) => s.activeAgent)
  const connection = useDisplayedConnection()
  const [agent, setAgent] = useState<string | null>(params.get('agent'))
  const [project, setProject] = useState<string>(params.get('project') ?? '')
  // `?prompt=` (the Skills page's Use) prefills the message once.
  const [text, setText] = useState(() => readPromptFromUrl(params.toString()) ?? '')
  useEffect(() => {
    if (!params.has('prompt')) return
    const next = new URLSearchParams(params)
    next.delete('prompt')
    setParams(next, { replace: true })
  }, [params, setParams])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // A session created by a Start whose first message then failed: a retry
  // with the same choices sends into it instead of creating another.
  const created = useRef<{ key: string; agent: string; project: string } | null>(null)
  const attachments = useAttachments()
  const fileInput = useRef<HTMLInputElement>(null)
  const agentId = useId()
  const projectId = useId()
  const promptId = useId()

  const loading = agents.isPending || projects.isPending
  // A preselected agent or project (from the address, the switcher, the
  // project's owner) counts only if it is still there.
  const isAgent = (id: string | null | undefined): id is string =>
    Boolean(id) && Boolean(agents.data?.some((a) => a.id === id))
  const chosenProject = project && projects.data?.has(project) ? project : ''
  const chosenAgent =
    [agent, owners.data?.get(chosenProject), activeAgent].find(isAgent) ??
    agents.data?.[0]?.id ??
    null
  const listError = agents.isError ? agents.error : projects.isError ? projects.error : null
  const uploading = attachments.attachments.some(
    (a) => a.kind === 'uploading' || a.kind === 'inline_pending',
  )
  const startGate = gate(
    requireConnection(connection),
    loading ? blockedBecause(t('webChat.newLoading')) : null,
    !loading && !chosenAgent ? blockedBecause(t('webChat.newNoAgent')) : null,
    uploading ? blockedBecause(t('webChat.waitingUploads')) : null,
    !text.trim() && attachments.attachments.length === 0
      ? blockedBecause(t('webChat.emptyDraft'))
      : null,
  )

  const start = async () => {
    if (startGate.disabled || !chosenAgent) return
    setBusy(true)
    setError(null)
    try {
      const reuse =
        created.current?.agent === chosenAgent && created.current.project === chosenProject
          ? created.current.key
          : null
      const key =
        reuse ??
        (await createSessionOptimistic(rpc, queryClient, {
          agentId: chosenAgent,
          projectId: chosenProject || null,
        }))
      created.current = { key, agent: chosenAgent, project: chosenProject }
      const payload = await attachments.normalizeForSend(text, false)
      const message = payload?.text.trim() || t('webChat.attachmentsPrompt')
      await rpc.call('chat.send', {
        sessionKey: key,
        message,
        idempotency_key: newIdempotencyKey(),
        ...(payload?.attachments.length
          ? { attachments: payload.attachments.map(outgoingAttachment), displayText: text.trim() }
          : {}),
      })
      created.current = null
      navigate(sessionKeyToPath(key))
    } catch (e) {
      // The draft and attachments are still in place: nothing was cleared.
      setError(t('webChat.newFailed', { message: errorMessage(e) }))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bg-atmosphere min-h-full">
      <div className="anim-stagger mx-auto flex w-full max-w-2xl flex-col gap-6 px-4 pt-10 pb-8 md:px-8 md:pt-16">
        <div className="flex flex-col items-center gap-4 text-center">
          <span className="chat-hero-mark isolate size-12" aria-hidden>
            <MessageSquarePlus className="size-5" />
          </span>
          <h1 className="text-title">{t('webChat.newTitle')}</h1>
        </div>
        {listError ? (
          <ErrorState
            title={agents.isError ? t('webChat.newAgentsFailed') : t('webChat.newProjectsFailed')}
            message={errorMessage(listError)}
            retrying={agents.isFetching || projects.isFetching}
            onRetry={() => {
              if (agents.isError) void agents.refetch()
              if (projects.isError) void projects.refetch()
            }}
          />
        ) : null}
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <label htmlFor={agentId} className="text-label text-text-muted">
              {t('webChat.newAgent')}
            </label>
            <Select
              id={agentId}
              size="lg"
              value={chosenAgent ?? ''}
              disabled={loading}
              onChange={(e) => setAgent(e.target.value)}
            >
              {(agents.data ?? []).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={projectId} className="text-label text-text-muted">
              {t('webChat.newProject')}
            </label>
            <Select
              id={projectId}
              size="lg"
              value={chosenProject}
              disabled={loading}
              onChange={(e) => setProject(e.target.value)}
            >
              <option value="">{t('webChat.newProjectNone')}</option>
              {[...(projects.data ?? new Map<string, string>())].map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </Select>
          </div>
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor={promptId} className="text-label text-text-muted">
            {t('webChat.newPrompt')}
          </label>
          <div className="chat-composer">
            {attachments.attachments.length ? (
              <div className="flex flex-wrap gap-2 px-3 pt-3">
                {attachments.attachments.map((att) => (
                  <span key={att.local_id} className="chat-chip anim-scale-in origin-left">
                    <span className="chat-chip-thumb" aria-hidden>
                      <FileText className="size-3.5" />
                    </span>
                    <span className="truncate">{att.name}</span>
                    <button
                      type="button"
                      aria-label={t('webChat.removeAttachment', { name: att.name })}
                      onClick={() => attachments.remove(att.local_id)}
                      className="target inline-flex items-center justify-center rounded-md text-text-muted transition-colors duration-[var(--duration-fast)] hover:bg-surface-3 hover:text-text"
                    >
                      <X className="size-3.5" aria-hidden />
                    </button>
                  </span>
                ))}
              </div>
            ) : null}
            <textarea
              id={promptId}
              data-composer-input
              rows={5}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onPaste={(e) => {
                const files = [...(e.clipboardData?.files ?? [])]
                if (files.length) {
                  e.preventDefault()
                  attachments.addFiles(files)
                }
              }}
              className="block w-full resize-none bg-transparent px-4 pt-3.5 pb-1.5 text-[length:var(--text-prose)] leading-relaxed outline-none max-md:text-base touch:text-base"
            />
            <div className="flex flex-wrap items-center gap-2 px-2 pb-2">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => fileInput.current?.click()}
                className="text-text-muted hover:text-text"
              >
                <Paperclip />
                {t('webChat.attach')}
              </Button>
              <span className="flex-1" />
              {startGate.reason ? (
                <span id="start-reason" className="text-small text-text-muted">
                  {startGate.reason}
                </span>
              ) : null}
              <Button
                variant="primary"
                disabled={startGate.disabled || busy}
                aria-describedby={startGate.reason ? 'start-reason' : undefined}
                onClick={() => void start()}
                className="chat-primary rounded-full px-4"
              >
                {busy ? <Loader2 className="motion-status animate-spin" /> : null}
                {busy ? t('webChat.newStarting') : t('webChat.newStart')}
              </Button>
            </div>
          </div>
        </div>
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            if (e.target.files) attachments.addFiles(e.target.files)
            e.target.value = ''
          }}
        />
        {error ? (
          <p role="alert" className="chat-failed anim-enter px-3.5 py-2.5">
            {error}
          </p>
        ) : null}
      </div>
    </div>
  )
}
