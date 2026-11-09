import { useQueryClient } from '@tanstack/react-query'
import { Loader2, Paperclip, X } from 'lucide-react'
import { useId, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { blockedBecause, gate, requireConnection } from '@/lib/disabled-reason'
import { newIdempotencyKey } from '@/lib/ws-rpc'
import { useAttachments } from '@/views/chat/Attachments'
import { outgoingAttachment } from '@/views/chat/logic'
import { Button } from '@/web/components/ui/button'
import { createSessionOptimistic, useAgents, useProjectNames } from '@/web/shell/session-data'
import { sessionKeyToPath } from '@/web/shell/session-url'
import { useShell } from '@/web/shell/shell-store'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

const fieldClass =
  'h-10 w-full rounded-md border border-border-control bg-surface-0 px-3 touch:h-11 max-md:text-base touch:text-base'

/**
 * `/new` (chat spec "New session page"): agent and project pickers and a first
 * message with attachments. Start creates the session, sends the message and
 * opens it; on failure the draft and attachments stay and the error shows.
 */
export function NewSessionPage() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const agents = useAgents()
  const projects = useProjectNames()
  const activeAgent = useShell((s) => s.activeAgent)
  const connection = useDisplayedConnection()
  const [agent, setAgent] = useState<string | null>(params.get('agent') ?? activeAgent)
  const [project, setProject] = useState<string>(params.get('project') ?? '')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const attachments = useAttachments()
  const fileInput = useRef<HTMLInputElement>(null)
  const agentId = useId()
  const projectId = useId()
  const promptId = useId()

  const loading = agents.isPending || projects.isPending
  const chosenAgent = agent ?? agents.data?.[0]?.id ?? null
  const uploading = attachments.attachments.some(
    (a) => a.kind === 'uploading' || a.kind === 'inline_pending',
  )
  const startGate = gate(
    requireConnection(connection),
    loading ? blockedBecause(t('webChat.newLoading')) : null,
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
      const key = await createSessionOptimistic(rpc, queryClient, {
        agentId: chosenAgent,
        projectId: project || null,
      })
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
      navigate(sessionKeyToPath(key))
    } catch (e) {
      // The draft and attachments are still in place: nothing was cleared.
      setError(t('webChat.newFailed', { message: errorMessage(e) }))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-4 p-4 md:p-8">
      <h1 className="text-xl font-semibold">{t('webChat.newTitle')}</h1>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <label htmlFor={agentId} className="font-medium">
            {t('webChat.newAgent')}
          </label>
          <select
            id={agentId}
            className={fieldClass}
            value={chosenAgent ?? ''}
            disabled={loading}
            onChange={(e) => setAgent(e.target.value)}
          >
            {(agents.data ?? []).map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={projectId} className="font-medium">
            {t('webChat.newProject')}
          </label>
          <select
            id={projectId}
            className={fieldClass}
            value={project}
            disabled={loading}
            onChange={(e) => setProject(e.target.value)}
          >
            <option value="">{t('webChat.newProjectNone')}</option>
            {[...(projects.data ?? new Map<string, string>())].map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={promptId} className="font-medium">
          {t('webChat.newPrompt')}
        </label>
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
          className="w-full rounded-md border border-border-control bg-surface-0 p-3 text-[length:var(--text-prose)] max-md:text-base touch:text-base"
        />
        {attachments.attachments.length ? (
          <div className="flex flex-wrap gap-2">
            {attachments.attachments.map((att) => (
              <span
                key={att.local_id}
                className="inline-flex items-center gap-1 rounded-md bg-surface-3 py-1 pr-1 pl-2 text-small"
              >
                {att.name}
                <button
                  type="button"
                  aria-label={t('webChat.removeAttachment', { name: att.name })}
                  onClick={() => attachments.remove(att.local_id)}
                  className="target inline-flex items-center justify-center rounded text-text-muted"
                >
                  <X className="size-3.5" aria-hidden />
                </button>
              </span>
            ))}
          </div>
        ) : null}
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
        <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2">
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" onClick={() => fileInput.current?.click()}>
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
        >
          {busy ? <Loader2 className="animate-spin" /> : null}
          {busy ? t('webChat.newStarting') : t('webChat.newStart')}
        </Button>
      </div>
    </div>
  )
}
