import {
  Copy,
  Download,
  FolderOpen,
  MoreHorizontal,
  PanelBottom,
  PanelRight,
  Pencil,
  RotateCcw,
  Scissors,
} from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { useRpc } from '@/app/providers'
import { formatCombo } from '@/components/KeyboardShortcuts'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import '@/i18n/en/webShell'
import '@/i18n/en/webPanel'
import { Button } from '@/web/components/ui/button'
import { fieldVariants } from '@/web/components/ui/input'
import { SimpleTooltip } from '@/web/components/ui/tooltip'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'
import { ChannelIcon } from '@/web/shell/channel-icon'
import { ResetSessionDialog } from '@/web/shell/ResetSessionDialog'
import { exportSessionMarkdown } from '@/web/shell/session-actions'
import { SESSIONS_QUERY_KEY, useProjectNames } from '@/web/shell/session-data'
import {
  sessionAgent,
  sessionChannel,
  sessionProject,
  type SessionRow,
} from '@/web/shell/session-list'
import '@/web/styles/chat-stage.css'
import { AgentAvatar } from './AgentAvatar'
import { PANEL_TOGGLE_KEY } from './panels/open-paths'
import { PANEL_ID, PANEL_TOGGLE_ID, type PanelDock } from './panels/panel-store'

// Round outlined, like the composer's options button (chat-stage.css).
const ICON_BUTTON = 'chat-head-action'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** The first line of a message, as a title. */
function titleFrom(text: string): string {
  const line = text.trim().split('\n', 1)[0] ?? ''
  return line.length > 80 ? `${line.slice(0, 79).trimEnd()}…` : line
}

/**
 * The headline: the session's name, else its derived title, else the first
 * thing the user asked, else "New conversation" — never a raw session id.
 */
export function headerTitle(row: SessionRow | undefined, firstMessage?: string | null): string {
  const named = String(row?.display_name || row?.displayName || '').trim()
  if (named) return named
  const derived = String(row?.derived_title || row?.derivedTitle || '').trim()
  if (derived) return derived
  const asked = firstMessage ? titleFrom(firstMessage) : ''
  return asked || t('webChat.headerUntitled')
}

export interface ChatHeaderProps {
  sessionKey: string
  row: SessionRow | undefined
  running: boolean
  phone: boolean
  /** The workspace panel is showing. */
  panelOpen: boolean
  panelDock: PanelDock
  onTogglePanels(opener: HTMLElement): void
  onCompact(): void
  onReset(): void
  /** The first user message, for a title when the session has no name. */
  firstMessage?: string | null
}

/** The conversation header: title (rename in place), chips, toggles and ⋯ menu. */
export function ChatHeader(props: ChatHeaderProps) {
  const { sessionKey, row, phone } = props
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const projects = useProjectNames()
  const title = headerTitle(row, props.firstMessage)
  const shortId = sessionKey.split(':').at(-1) || sessionKey
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(title)
  const [error, setError] = useState<string | null>(null)
  const [confirmReset, setConfirmReset] = useState(false)
  const project = row ? sessionProject(row) : null
  const panelLabel = props.panelOpen ? t('webPanel.hide') : t('webPanel.show')

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const name = value.trim()
    try {
      await rpc.call('sessions.rename', { key: sessionKey, name })
      queryClient.setQueryData<SessionRow[]>(SESSIONS_QUERY_KEY, (prev) =>
        prev?.map((r) =>
          r.key === sessionKey ? { ...r, display_name: name, displayName: name } : r,
        ),
      )
      setEditing(false)
      setError(null)
      void queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY })
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  return (
    <header className="chat-header @container/chat sticky top-0 z-[var(--z-sticky)] flex min-h-14 items-center gap-2 px-3 py-2 sm:px-4">
      {/* Phones open navigation from the shell's top bar; a second ☰ here duplicated it. */}
      <div className="flex min-w-0 flex-1 items-center gap-2">
        {editing ? (
          <form onSubmit={(e) => void submit(e)} className="flex min-w-0 flex-1 flex-col">
            <input
              autoFocus
              aria-label={t('webChat.headerRename')}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  e.stopPropagation()
                  setEditing(false)
                  setError(null)
                }
              }}
              aria-invalid={error ? true : undefined}
              className={cn(fieldVariants({ size: 'sm' }), 'max-w-md font-medium')}
            />
            {error ? (
              <span role="alert" className="text-small text-danger">
                {error}
              </span>
            ) : null}
          </form>
        ) : (
          <button
            type="button"
            onClick={() => {
              setValue(title)
              setEditing(true)
            }}
            title={t('webChat.headerRename')}
            className="chat-title-button target"
          >
            <h1 className="truncate">{title}</h1>
          </button>
        )}
        {!phone && !editing ? (
          <div className="hidden min-w-0 shrink items-center gap-1.5 @xl/chat:flex">
            {row ? (
              <>
                <span
                  className="chat-meta-chip"
                  aria-label={t('webChat.chipAgent', { value: sessionAgent(row) })}
                >
                  <AgentAvatar size={16} />
                  <span className="truncate">{sessionAgent(row)}</span>
                </span>
                {project ? (
                  <span
                    className="chat-meta-chip"
                    aria-label={t('webChat.chipProject', {
                      value: projects.data?.get(project) ?? project,
                    })}
                  >
                    <FolderOpen aria-hidden className="ml-1" />
                    <span className="truncate">{projects.data?.get(project) ?? project}</span>
                  </span>
                ) : null}
                <span className="chat-meta-chip" data-plain>
                  <ChannelIcon channel={sessionChannel(row)} className="inline-flex" />
                </span>
              </>
            ) : null}
            <SimpleTooltip side="bottom" label={t('webChat.headerCopyId')}>
              <button
                type="button"
                onClick={() => {
                  void navigator.clipboard
                    ?.writeText(sessionKey)
                    .then(() => toast.success(t('webChat.headerIdCopied')))
                }}
                className="chat-meta-chip chat-id-chip target max-w-32"
              >
                <span className="sr-only">{`${t('webChat.headerCopyId')}: `}</span>
                <span className="truncate">{shortId}</span>
                <Copy aria-hidden />
              </button>
            </SimpleTooltip>
          </div>
        ) : null}
      </div>
      <SimpleTooltip side="bottom" label={`${panelLabel} (${formatCombo(PANEL_TOGGLE_KEY)})`}>
        <Button
          id={PANEL_TOGGLE_ID}
          size="icon"
          variant="ghost"
          aria-label={panelLabel}
          aria-expanded={props.panelOpen}
          aria-controls={props.panelOpen ? PANEL_ID : undefined}
          data-active={props.panelOpen || undefined}
          onClick={(e) => props.onTogglePanels(e.currentTarget)}
          className={cn(ICON_BUTTON, 'chat-panel-toggle')}
        >
          {props.panelDock === 'bottom' ? <PanelBottom /> : <PanelRight />}
        </Button>
      </SimpleTooltip>
      <DropdownMenu>
        <SimpleTooltip side="bottom" label={t('webChat.headerMenu')}>
          <DropdownMenuTrigger asChild>
            <Button
              size="icon"
              variant="ghost"
              aria-label={t('webChat.headerMenu')}
              className={ICON_BUTTON}
            >
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
        </SimpleTooltip>
        <DropdownMenuContent align="end" className="chat-menu">
          <DropdownMenuItem
            className="chat-menu-item"
            onSelect={() => {
              setValue(title)
              setEditing(true)
            }}
          >
            <Pencil aria-hidden />
            {t('webShell.actionRename')}
          </DropdownMenuItem>
          <DropdownMenuItem
            className="chat-menu-item"
            disabled={props.running}
            onSelect={props.onCompact}
          >
            <Scissors aria-hidden />
            {t('webChat.headerCompact')}
          </DropdownMenuItem>
          <DropdownMenuItem
            className="chat-menu-item"
            disabled={props.running}
            onSelect={() => setConfirmReset(true)}
          >
            <RotateCcw aria-hidden />
            {t('webChat.headerReset')}
          </DropdownMenuItem>
          <DropdownMenuItem
            className="chat-menu-item"
            onSelect={() => void exportSessionMarkdown(rpc, sessionKey)}
          >
            <Download aria-hidden />
            {t('webShell.actionExport')}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            className="chat-menu-item"
            onSelect={() => void navigator.clipboard?.writeText(sessionKey)}
          >
            <Copy aria-hidden />
            {t('webShell.actionCopyKey')}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <ResetSessionDialog
        open={confirmReset}
        onOpenChange={setConfirmReset}
        title={title}
        onConfirm={props.onReset}
      />
    </header>
  )
}
