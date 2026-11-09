import { useQueryClient } from '@tanstack/react-query'
import {
  ArrowUp,
  ChevronDown,
  Loader2,
  Paperclip,
  Pencil,
  Route,
  Settings2,
  Square,
  Trash2,
  X,
} from 'lucide-react'
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
} from 'react'
import { useNavigate } from 'react-router'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { cn } from '@/lib/utils'
import { usePreferences } from '@/stores/preferences'
import { useAttachments } from '@/views/chat/Attachments'
import {
  outgoingAttachment,
  slashCommandKey,
  type PendingAttachment,
  type SlashCommand,
} from '@/views/chat/logic'
import { usePendingQueue } from '@/views/chat/usePendingQueue'
import { useRoutePin } from '@/views/chat/useRoutePin'
import { useSlashCommands } from '@/views/chat/useSlashCommands'
import { Button } from '@/web/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'
import { useMediaQuery } from '@/web/shell/use-media-query'
import type { SendInput } from '../useChatSession'
import { useElevated, usePilotRouter, usePlanMode, useSessionUsage } from './capabilities'
import {
  clearDraft,
  loadDraft,
  loadStagedAttachments,
  saveDraft,
  saveStagedAttachments,
} from './drafts'
import { composerEnterAction } from './keys'

const MAX_HEIGHT_PX = 240

export interface ComposerProps {
  sessionKey: string
  running: boolean
  /** Why sending is impossible right now (offline), or null. */
  blockedReason: string | null
  onSend(input: SendInput): Promise<boolean>
  onAbort(): void
  onCompact(): void
  onQueueChange?(count: number): void
  /** A message to put back in the composer (Edit message). */
  editRequest?: { text: string; nonce: number } | null
  /** Called after a send so the transcript follows again. */
  onSent?(): void
}

function isUploading(att: PendingAttachment): boolean {
  return att.kind === 'uploading' || att.kind === 'inline_pending'
}

/** The slash-command filter: commands whose key or alias starts with the typed word. */
export function filterSlash(commands: SlashCommand[], text: string): SlashCommand[] {
  const word = slashCommandKey(text)
  if (!word || /\s/.test(text.trim())) return []
  return commands
    .filter((c) =>
      [c.cmd, ...(c.aliases ?? [])].some((name) => slashCommandKey(name).startsWith(word)),
    )
    .slice(0, 12)
}

export function Composer(props: ComposerProps) {
  const { sessionKey, running, blockedReason, onSend, onAbort, onCompact, onQueueChange } = props
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const sendShortcut = usePreferences((s) => s.sendShortcut)
  const visualEffects = usePreferences((s) => s.visualEffects)
  const setPreference = usePreferences((s) => s.setPreference)
  const touch = useMediaQuery('(pointer: coarse)')
  const [text, setText] = useState(() => loadDraft(sessionKey))
  const [slashIndex, setSlashIndex] = useState(0)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const attachments = useAttachments()
  const runningRef = useRef(running)
  useEffect(() => {
    runningRef.current = running
  }, [running])
  const intentRef = useRef<string | null>(null)

  // Drafts per session: restore on switch, save as the user types.
  const [loadedFor, setLoadedFor] = useState(sessionKey)
  if (loadedFor !== sessionKey) {
    setLoadedFor(sessionKey)
    setText(loadDraft(sessionKey))
  }
  const { setAll } = attachments
  useEffect(() => {
    setAll(loadStagedAttachments(sessionKey))
  }, [sessionKey, setAll])
  useEffect(() => {
    saveDraft(sessionKey, text)
  }, [sessionKey, text])
  useEffect(() => {
    saveStagedAttachments(sessionKey, attachments.attachments)
  }, [sessionKey, attachments.attachments])

  // Edit message: put a failed turn's text back.
  const editNonce = props.editRequest?.nonce
  const [seenEdit, setSeenEdit] = useState(editNonce)
  if (props.editRequest && editNonce !== seenEdit) {
    setSeenEdit(editNonce)
    setText(props.editRequest.text)
  }

  // Grow with the content up to a cap, then scroll inside.
  useLayoutEffect(() => {
    const el = textarea.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT_PX)}px`
    el.style.overflowY = el.scrollHeight > MAX_HEIGHT_PX ? 'auto' : 'hidden'
  }, [text])

  const deliver = useCallback(
    async (raw: string, atts: PendingAttachment[], intent: string | null) => {
      const ok = await onSend({
        text: raw.trim() || t('webChat.attachmentsPrompt'),
        displayText: raw.trim(),
        attachments: atts.length ? atts.map(outgoingAttachment) : undefined,
        intent,
      })
      props.onSent?.()
      return ok
    },
    [onSend, props],
  )

  const queue = usePendingQueue(
    {
      getComposerText: () => textarea.current?.value ?? '',
      setComposerText: setText,
      getAttachments: () => attachments.attachments,
      setAttachments: attachments.setAll,
      getIntent: () => intentRef.current,
      setIntent: (intent) => {
        intentRef.current = intent
      },
      sendDrainedHead: (head, atts, intent) => void deliver(head, atts, intent),
      isStreaming: () => runningRef.current,
      isCompactInFlight: () => false,
    },
    sessionKey,
  )
  useEffect(() => onQueueChange?.(queue.length), [queue.length, onQueueChange])

  // A turn ended: send the next queued message.
  const wasRunning = useRef(running)
  const { scheduleDrainAfterTerminal } = queue
  useEffect(() => {
    if (wasRunning.current && !running) scheduleDrainAfterTerminal()
    wasRunning.current = running
  }, [running, scheduleDrainAfterTerminal])

  const slash = useSlashCommands({
    sessionKey,
    onSessionAction: (action) => {
      if (action === 'new_chat') navigate('/new')
      else if (action === 'compact_context') onCompact()
    },
    onRouteHoldChange: () => void queryClient.invalidateQueries({ queryKey: ['router.hold.get'] }),
  })
  const matches = useMemo(
    () => (text.startsWith('/') ? filterSlash(slash.commands, text) : []),
    [slash.commands, text],
  )
  const slashOpen = matches.length > 0
  const activeSlash = Math.min(slashIndex, Math.max(0, matches.length - 1))

  const uploading = attachments.attachments.some(isUploading)
  const hasDraft = text.trim().length > 0 || attachments.attachments.length > 0
  const sendReason = blockedReason ?? (uploading ? t('webChat.waitingUploads') : null)

  const clearComposer = () => {
    setText('')
    attachments.clear()
    clearDraft(sessionKey)
    saveStagedAttachments(sessionKey, [])
  }

  const submit = async () => {
    if (sendReason || !hasDraft) return
    const value = text
    if (value.trim().startsWith('/')) {
      setText('')
      clearDraft(sessionKey)
      await slash.execute(value.trim())
      return
    }
    if (running) {
      if (
        queue.enqueue({
          text: value,
          attachments: attachments.attachments,
          intent: intentRef.current,
        })
      ) {
        clearComposer()
      }
      return
    }
    const normalized = await attachments.normalizeForSend(value, false)
    if (!normalized) return
    clearComposer()
    await deliver(normalized.text, normalized.attachments, intentRef.current)
  }

  const runSlash = async (cmd: SlashCommand) => {
    setText('')
    clearDraft(sessionKey)
    await slash.execute(cmd.cmd)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (slashOpen) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        const delta = e.key === 'ArrowDown' ? 1 : -1
        setSlashIndex((activeSlash + delta + matches.length) % matches.length)
        return
      }
      if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault()
        void runSlash(matches[activeSlash]!)
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        setText('')
        return
      }
    }
    if (e.key === 'Escape') {
      if (running) {
        e.preventDefault()
        onAbort()
      } else if (queue.length > 0) {
        e.preventDefault()
        queue.popAllIntoComposer()
      }
      return
    }
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
      void submit()
    }
  }

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = [...(e.clipboardData?.files ?? [])]
    if (files.length) {
      e.preventDefault()
      attachments.addFiles(files)
    }
  }
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    if (!e.dataTransfer.files.length) return
    e.preventDefault()
    attachments.addFiles(e.dataTransfer.files)
  }

  const primary: 'send' | 'stop' | 'queue' = running ? (hasDraft ? 'queue' : 'stop') : 'send'

  return (
    <div
      onDragOver={(e) => e.dataTransfer.types.includes('Files') && e.preventDefault()}
      onDrop={onDrop}
      className="mx-auto w-full max-w-3xl px-4 pb-[max(env(safe-area-inset-bottom),12px)]"
    >
      {queue.length > 0 ? (
        <section
          aria-label={t('webChat.queued')}
          className="mb-2 rounded-lg border border-border bg-surface-1 p-2"
        >
          <ol className="flex flex-col gap-1">
            {queue.queue.map((item, i) => (
              <li key={i} className="flex items-center gap-2 text-ui">
                <span className="min-w-0 flex-1 truncate">{item.text}</span>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label={t('webChat.queuedEdit', { index: i + 1 })}
                  onClick={() => {
                    queue.remove(i)
                    setText(item.text)
                  }}
                >
                  <Pencil />
                </Button>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label={t('webChat.queuedRemove', { index: i + 1 })}
                  onClick={() => queue.remove(i)}
                >
                  <Trash2 />
                </Button>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {slashOpen ? (
        <ul
          role="listbox"
          aria-label={t('webChat.slashMenu')}
          className="mb-2 max-h-64 overflow-y-auto rounded-lg border border-border bg-surface-2 p-1 shadow-2"
        >
          {matches.map((cmd, i) => (
            <li
              key={cmd.cmd}
              role="option"
              aria-selected={i === activeSlash}
              onMouseDown={(e) => {
                e.preventDefault()
                void runSlash(cmd)
              }}
              className={cn(
                'flex cursor-default gap-3 rounded-md px-2 py-1.5',
                // surface-3 alone is ~1.1:1; the ring marks the active option at 3:1.
                i === activeSlash &&
                  'bg-surface-3 outline-2 -outline-offset-2 outline-ring outline-solid',
              )}
            >
              <span className="font-mono">{cmd.cmd}</span>
              <span className="truncate text-text-muted">{cmd.desc}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {/* Focus: border and a 1px ring together. The border colour change alone
          is only ~1.6:1 in the light theme (olive over grey). */}
      <div className="rounded-2xl border border-border-control bg-surface-1 shadow-1 focus-within:border-accent focus-within:ring-1 focus-within:ring-ring">
        {attachments.attachments.length > 0 || attachments.rejection ? (
          <div
            className="flex flex-wrap gap-2 border-b border-border p-2"
            aria-label={t('webChat.attach')}
          >
            {attachments.attachments.map((att) => (
              <span
                key={att.local_id}
                data-attachment-state={att.kind}
                className="inline-flex max-w-56 items-center gap-1.5 rounded-md bg-surface-3 py-1 pr-1 pl-2 text-small"
              >
                {isUploading(att) ? (
                  <Loader2 className="motion-status size-3.5 animate-spin" aria-hidden />
                ) : null}
                {att.dataUrl && att.mime.startsWith('image/') ? (
                  <img src={att.dataUrl} alt="" className="size-6 rounded object-cover" />
                ) : null}
                <span className="truncate">{att.name}</span>
                <button
                  type="button"
                  aria-label={t('webChat.removeAttachment', { name: att.name })}
                  onClick={() => attachments.remove(att.local_id)}
                  className="target inline-flex items-center justify-center rounded text-text-muted hover:text-text"
                >
                  <X className="size-3.5" aria-hidden />
                </button>
              </span>
            ))}
            {attachments.rejection ? (
              <p role="alert" className="w-full text-small text-danger">
                {attachments.rejection}
              </p>
            ) : null}
          </div>
        ) : null}
        <textarea
          ref={textarea}
          data-composer-input
          aria-label={t('webChat.composerLabel')}
          placeholder={
            touch ? t('webChat.composerPlaceholderTouch') : t('webChat.composerPlaceholder')
          }
          rows={1}
          value={text}
          onChange={(e) => {
            setText(e.target.value)
            setSlashIndex(0)
          }}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          enterKeyHint={touch ? 'enter' : 'send'}
          className="block w-full resize-none bg-transparent px-4 pt-3 pb-1 text-[length:var(--text-prose)] leading-normal outline-none placeholder:text-text-muted max-md:text-base touch:text-base"
        />
        <div className="flex min-w-0 items-center gap-1 px-2 pb-2">
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
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={t('webChat.attach')}
            onClick={() => fileInput.current?.click()}
          >
            <Paperclip />
          </Button>
          <RoutePicker sessionKey={sessionKey} />
          <CapabilityMenu
            sessionKey={sessionKey}
            visualEffects={visualEffects}
            setVisualEffects={(v) => setPreference('visualEffects', v)}
          />
          <UsageReadout sessionKey={sessionKey} />
          <span className="flex-1" />
          {sendReason && hasDraft && primary === 'send' ? (
            <span id="send-reason" className="min-w-0 truncate text-small text-text-muted">
              {sendReason}
            </span>
          ) : null}
          {primary === 'stop' ? (
            <Button
              size="icon-sm"
              variant="secondary"
              aria-label={t('webChat.stop')}
              onClick={onAbort}
              className="rounded-full"
            >
              <Square className="fill-current" />
            </Button>
          ) : (
            <Button
              size={primary === 'queue' ? 'sm' : 'icon-sm'}
              variant="primary"
              aria-label={primary === 'queue' ? t('webChat.queue') : t('webChat.send')}
              aria-describedby={sendReason ? 'send-reason' : undefined}
              disabled={!hasDraft || Boolean(sendReason)}
              onClick={() => void submit()}
              className="rounded-full"
            >
              {primary === 'queue' ? t('webChat.queue') : <ArrowUp />}
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}

function RoutePicker({ sessionKey }: { sessionKey: string }) {
  const rpc = useRpc()
  const pin = useRoutePin(rpc, sessionKey)
  const label = pin.pinnedModel
    ? t('webChat.routePinnedModel', { model: pin.pinnedModel })
    : pin.pinned
      ? t('webChat.routePinnedTier', { tier: pin.pinned })
      : t('webChat.routeAuto')
  return (
    <DropdownMenu onOpenChange={(open) => open && pin.reload()}>
      <DropdownMenuTrigger asChild>
        <Button
          size="sm"
          variant="ghost"
          aria-label={`${t('webChat.route')}: ${label}`}
          data-pinned={pin.isPinned || undefined}
          // The one toolbar control that gives way on a narrow composer, so Send stays inside it.
          className="min-w-0 shrink"
        >
          <Route />
          <span className="max-w-32 min-w-0 truncate">{label}</span>
          <ChevronDown className="size-3" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="max-h-80 overflow-y-auto">
        <DropdownMenuCheckboxItem checked={!pin.isPinned} onSelect={() => pin.clear()}>
          {t('webChat.routeAuto')}
        </DropdownMenuCheckboxItem>
        {pin.tiers.length ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuLabel>{t('webChat.routeTiers')}</DropdownMenuLabel>
            {pin.tiers.map((tier) => (
              <DropdownMenuCheckboxItem
                key={tier.tier}
                checked={pin.pinned === tier.tier}
                onSelect={() => pin.pin(tier.tier)}
              >
                {tier.tier} · {tier.model}
              </DropdownMenuCheckboxItem>
            ))}
          </>
        ) : null}
        {pin.models.length ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuLabel>{t('webChat.routeModels')}</DropdownMenuLabel>
            {pin.models.map((model) => (
              <DropdownMenuCheckboxItem
                key={model.id}
                checked={pin.pinnedModel === model.id}
                onSelect={() => pin.pinModel(model.id)}
              >
                {model.name}
              </DropdownMenuCheckboxItem>
            ))}
          </>
        ) : null}
        {!pin.tiers.length && !pin.models.length ? (
          <DropdownMenuItem disabled>{t('webChat.routeLoading')}</DropdownMenuItem>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function CapabilityMenu({
  sessionKey,
  visualEffects,
  setVisualEffects,
}: {
  sessionKey: string
  visualEffects: boolean
  setVisualEffects(v: boolean): void
}) {
  const plan = usePlanMode(sessionKey)
  const elevated = useElevated(sessionKey)
  const router = usePilotRouter()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="icon-sm" variant="ghost" aria-label={t('webChat.capabilities')}>
          <Settings2 />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <DropdownMenuCheckboxItem
          checked={plan.on}
          onCheckedChange={(v) => void plan.set(Boolean(v))}
        >
          {t('webChat.planMode')}
        </DropdownMenuCheckboxItem>
        <DropdownMenuCheckboxItem
          checked={elevated.bypass}
          disabled={elevated.unavailable}
          onCheckedChange={(v) => void elevated.set(Boolean(v))}
        >
          {t('webChat.elevated')}
        </DropdownMenuCheckboxItem>
        <DropdownMenuCheckboxItem
          checked={router.enabled}
          onCheckedChange={(v) => void router.set(Boolean(v))}
        >
          {t('webChat.pilotRouter')}
        </DropdownMenuCheckboxItem>
        <DropdownMenuCheckboxItem
          checked={visualEffects}
          onCheckedChange={(v) => setVisualEffects(Boolean(v))}
        >
          {t('webChat.visualEffects')}
        </DropdownMenuCheckboxItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function UsageReadout({ sessionKey }: { sessionKey: string }) {
  const usage = useSessionUsage(sessionKey)
  const data = usage.data
  if (!data) return null
  const tokens = (data.input + data.output).toLocaleString()
  const cost = data.cost === null ? '—' : `$${data.cost.toFixed(4)}`
  return (
    <span
      className="hidden text-small text-text-muted tabular-nums sm:inline"
      title={t('webChat.usage', { tokens, cost })}
    >
      {tokens} · {cost}
    </span>
  )
}
