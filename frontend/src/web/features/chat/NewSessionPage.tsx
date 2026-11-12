import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  Check,
  Code2,
  FileText,
  Folder,
  FolderOpen,
  Globe,
  ListChecks,
  Loader2,
  Paperclip,
  type LucideIcon,
} from 'lucide-react'
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ComponentProps,
  type DragEvent,
  type KeyboardEvent,
} from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { blockedBecause, gate, requireConnection } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import { newIdempotencyKey } from '@/lib/ws-rpc'
import { usePreferences } from '@/stores/preferences'
import { useAttachments } from '@/views/chat/Attachments'
import { outgoingAttachment, readPromptFromUrl } from '@/views/chat/logic'
import { projectId, type RawProject } from '@/views/projects/logic'
import { Button } from '@/web/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItemIndicator,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'
import { ErrorState } from '@/web/components/ui/states'
import { createSessionOptimistic, useAgents, useProjectNames } from '@/web/shell/session-data'
import { sessionKeyToPath } from '@/web/shell/session-url'
import { useShell } from '@/web/shell/shell-store'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useMediaQuery } from '@/web/shell/use-media-query'
import '@/web/styles/chat.css'
import { AgentAvatar } from './AgentAvatar'
import { KeyHints, StageBackdrop, StageHeading, StageMark } from './ChatStage'
import { composerEnterAction } from './composer/keys'
import { AttachmentTray, SendButton, ToolbarChip } from './composer/parts'

const MAX_HEIGHT_PX = 320

const SUGGESTIONS: ReadonlyArray<{
  icon: LucideIcon
  label: Parameters<typeof t>[0]
  prompt: Parameters<typeof t>[0]
}> = [
  {
    icon: FileText,
    label: 'webChat.newSuggestSummarise',
    prompt: 'webChat.newSuggestSummarisePrompt',
  },
  { icon: ListChecks, label: 'webChat.newSuggestPlan', prompt: 'webChat.newSuggestPlanPrompt' },
  { icon: Globe, label: 'webChat.newSuggestSearch', prompt: 'webChat.newSuggestSearchPrompt' },
  { icon: Code2, label: 'webChat.newSuggestReview', prompt: 'webChat.newSuggestReviewPrompt' },
]

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

/** A choice in a chip's menu: a check on the right of the chosen one. */
function PickItem({ children, className, ...props }: ComponentProps<typeof DropdownMenuRadioItem>) {
  return (
    <DropdownMenuRadioItem {...props} className={cn('chat-menu-item chat-pick-item', className)}>
      {children}
      <DropdownMenuItemIndicator className="absolute right-2 inline-flex">
        <Check className="text-accent!" aria-hidden />
      </DropdownMenuItemIndicator>
    </DropdownMenuRadioItem>
  )
}

/**
 * `/new` (chat spec "New session page"): the front door. A greeting over one
 * large composer whose toolbar carries the agent and project as menu chips,
 * starter suggestions and keyboard hints. Start creates the session, sends
 * the message and opens it; on failure the draft and attachments stay and
 * the error shows.
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
  const sendShortcut = usePreferences((s) => s.sendShortcut)
  const touch = useMediaQuery('(pointer: coarse)')
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
  const [dragging, setDragging] = useState(false)
  // A session created by a Start whose first message then failed: a retry
  // with the same choices sends into it instead of creating another.
  const created = useRef<{ key: string; agent: string; project: string } | null>(null)
  const attachments = useAttachments()
  const fileInput = useRef<HTMLInputElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const agentValueId = useId()
  const projectValueId = useId()
  const reasonId = useId()

  // Grow with the content up to a cap, then scroll inside.
  useLayoutEffect(() => {
    const el = textarea.current
    if (!el) return
    el.style.height = 'auto'
    const needed = el.scrollHeight
    el.style.height = `${Math.min(needed, MAX_HEIGHT_PX)}px`
    el.style.overflowY = needed > MAX_HEIGHT_PX ? 'auto' : 'hidden'
  }, [text])

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
  const agentName = agents.data?.find((a) => a.id === chosenAgent)?.name ?? chosenAgent
  const projectName = chosenProject
    ? (projects.data?.get(chosenProject) ?? chosenProject)
    : t('webChat.newProjectNone')
  const listError = agents.isError ? agents.error : projects.isError ? projects.error : null
  const uploading = attachments.attachments.some(
    (a) => a.kind === 'uploading' || a.kind === 'inline_pending',
  )
  const emptyDraft = !text.trim() && attachments.attachments.length === 0
  const startGate = gate(
    requireConnection(connection),
    loading ? blockedBecause(t('webChat.newLoading')) : null,
    !loading && !chosenAgent ? blockedBecause(t('webChat.newNoAgent')) : null,
    uploading ? blockedBecause(t('webChat.waitingUploads')) : null,
    emptyDraft ? blockedBecause(t('webChat.emptyDraft')) : null,
  )
  // "Type a message first" and "Loading…" are the resting states: a tooltip
  // and a description. Anything the user must act on is printed under the card.
  const quietReason =
    startGate.reason === t('webChat.emptyDraft') || startGate.reason === t('webChat.newLoading')

  const start = async () => {
    if (startGate.disabled || !chosenAgent || busy) return
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

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    const action = composerEnterAction(
      {
        key: e.key,
        shiftKey: e.shiftKey,
        ctrlKey: e.ctrlKey,
        metaKey: e.metaKey,
        altKey: e.altKey,
        isComposing: e.nativeEvent.isComposing,
        keyCode: e.keyCode,
      },
      sendShortcut,
      touch,
    )
    if (action === 'send') {
      e.preventDefault()
      void start()
    }
  }

  // A starter suggestion fills the composer (it does not send) and focuses it.
  const fill = (prompt: string) => {
    setText(prompt)
    requestAnimationFrame(() => {
      const el = textarea.current
      if (!el) return
      el.focus()
      el.setSelectionRange(prompt.length, prompt.length)
    })
  }

  // Files dragged anywhere over the stage: the card lights up as the drop zone.
  const onDragOver = (e: DragEvent<HTMLDivElement>) => {
    if (!e.dataTransfer.types.includes('Files')) return
    e.preventDefault()
    if (!dragging) setDragging(true)
  }
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    setDragging(false)
    if (!e.dataTransfer.files.length) return
    e.preventDefault()
    attachments.addFiles(e.dataTransfer.files)
  }

  return (
    <div
      className="chat-stage"
      onDragOver={onDragOver}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false)
      }}
      onDrop={onDrop}
    >
      <StageBackdrop />
      <div className="stage-inner stage-enter">
        <StageMark />
        <StageHeading />
        <div className="flex w-full flex-col items-center gap-3">
          {listError ? (
            <ErrorState
              title={agents.isError ? t('webChat.newAgentsFailed') : t('webChat.newProjectsFailed')}
              message={errorMessage(listError)}
              retrying={agents.isFetching || projects.isFetching}
              onRetry={() => {
                if (agents.isError) void agents.refetch()
                if (projects.isError) void projects.refetch()
              }}
              className="w-full"
            />
          ) : null}
          <div
            className="chat-composer w-full"
            data-size="hero"
            data-dragging={dragging || undefined}
          >
            {dragging ? (
              <div className="chat-dropzone" aria-hidden>
                <Paperclip className="size-4" />
                {t('webChat.stageDrop')}
              </div>
            ) : null}
            <AttachmentTray
              attachments={attachments.attachments}
              rejection={attachments.rejection}
              onRemove={attachments.remove}
            />
            <textarea
              ref={textarea}
              data-composer-input
              aria-label={t('webChat.newPrompt')}
              placeholder={
                !loading && !chosenAgent
                  ? t('webChat.newPlaceholderNoAgent')
                  : t('webChat.newPlaceholder')
              }
              rows={3}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={onKeyDown}
              onPaste={(e) => {
                const files = [...(e.clipboardData?.files ?? [])]
                if (files.length) {
                  e.preventDefault()
                  attachments.addFiles(files)
                }
              }}
              enterKeyHint={touch ? 'enter' : 'send'}
              className="chat-composer-input max-md:text-base touch:text-base"
            />
            <div className="chat-toolbar">
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={t('webChat.attach')}
                onClick={() => fileInput.current?.click()}
                className="chat-tool-icon size-8 hover:text-text"
              >
                <Paperclip />
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild disabled={loading}>
                  <ToolbarChip
                    aria-label={t('webChat.newAgent')}
                    aria-describedby={agentValueId}
                    icon={
                      loading ? (
                        <Loader2 className="motion-status animate-spin" aria-hidden />
                      ) : (
                        <AgentAvatar size={20} className="-ml-0.5" />
                      )
                    }
                    label={
                      <span id={agentValueId}>
                        {loading
                          ? t('webChat.newAgentLoading')
                          : (agentName ?? t('webChat.newAgent'))}
                      </span>
                    }
                  />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="max-h-80 min-w-56 overflow-y-auto">
                  <DropdownMenuLabel>{t('webChat.newAgent')}</DropdownMenuLabel>
                  <DropdownMenuRadioGroup value={chosenAgent ?? ''} onValueChange={setAgent}>
                    {(agents.data ?? []).map((a) => (
                      <PickItem key={a.id} value={a.id}>
                        <AgentAvatar size={20} />
                        <span className="truncate">{a.name}</span>
                      </PickItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuContent>
              </DropdownMenu>
              <DropdownMenu>
                <DropdownMenuTrigger asChild disabled={loading}>
                  <ToolbarChip
                    aria-label={t('webChat.newProject')}
                    aria-describedby={projectValueId}
                    icon={chosenProject ? <FolderOpen aria-hidden /> : <Folder aria-hidden />}
                    label={<span id={projectValueId}>{projectName}</span>}
                  />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="max-h-80 min-w-56 overflow-y-auto">
                  <DropdownMenuLabel>{t('webChat.newProject')}</DropdownMenuLabel>
                  <DropdownMenuRadioGroup value={chosenProject} onValueChange={setProject}>
                    <PickItem value="">
                      <Folder aria-hidden />
                      {t('webChat.newProjectNone')}
                    </PickItem>
                    {projects.data?.size ? <DropdownMenuSeparator /> : null}
                    {[...(projects.data ?? new Map<string, string>())].map(([id, name]) => (
                      <PickItem key={id} value={id}>
                        <FolderOpen aria-hidden />
                        <span className="truncate">{name}</span>
                      </PickItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuContent>
              </DropdownMenu>
              <span className="min-w-0 flex-1" />
              <SendButton
                mode="send"
                size="lg"
                label={busy ? t('webChat.newStarting') : t('webChat.newStart')}
                busy={busy}
                disabled={startGate.disabled || busy}
                reason={startGate.reason}
                reasonId={reasonId}
                reasonDisplay="external"
                onClick={() => void start()}
              />
            </div>
          </div>
          {startGate.reason ? (
            <p
              id={reasonId}
              data-tone="warn"
              className={quietReason ? 'sr-only' : 'stage-note anim-enter'}
            >
              {quietReason ? null : <AlertCircle aria-hidden />}
              {startGate.reason}
            </p>
          ) : null}
          {error ? (
            <p role="alert" data-tone="danger" className="stage-note anim-enter">
              <AlertCircle aria-hidden />
              {error}
            </p>
          ) : null}
        </div>
        <div role="group" aria-label={t('webChat.newSuggestions')} className="stage-suggestions">
          {SUGGESTIONS.map(({ icon: Icon, label, prompt }) => (
            <button
              key={label}
              type="button"
              className="stage-suggestion target"
              onClick={() => fill(t(prompt))}
            >
              <Icon aria-hidden />
              {t(label)}
            </button>
          ))}
        </div>
        <KeyHints />
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
    </div>
  )
}
