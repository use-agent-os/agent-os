import {
  Copy,
  Download,
  MoreHorizontal,
  PanelRight,
  PanelRightOpen,
  Pencil,
  RotateCcw,
  Scissors,
} from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { cn } from '@/lib/utils'
import { useQueryClient } from '@tanstack/react-query'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import '@/i18n/en/webShell'
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
  sessionTitle,
  type SessionRow,
} from '@/web/shell/session-list'

const CHIP =
  'inline-flex h-6 min-w-0 items-center gap-1 rounded-full border border-border bg-surface-1/70 px-2 text-small text-text-muted'
const ICON_BUTTON =
  'text-text-muted hover:text-text aria-pressed:bg-surface-3 aria-pressed:text-text'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export interface ChatHeaderProps {
  sessionKey: string
  row: SessionRow | undefined
  running: boolean
  phone: boolean
  detailsOpen: boolean
  onToggleDetails(): void
  onTogglePanels(): void
  onCompact(): void
  onReset(): void
}

/** The conversation header: title (rename in place), chips, toggles and ⋯ menu. */
export function ChatHeader(props: ChatHeaderProps) {
  const { sessionKey, row, phone } = props
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const projects = useProjectNames()
  const title = row ? sessionTitle(row) : sessionKey
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(title)
  const [error, setError] = useState<string | null>(null)
  const [confirmReset, setConfirmReset] = useState(false)
  const project = row ? sessionProject(row) : null

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
    <header className="chat-header sticky top-0 z-[var(--z-sticky)] flex min-h-14 items-center gap-1 px-3 py-2 sm:px-4">
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
              if (phone) props.onToggleDetails()
              else {
                setValue(title)
                setEditing(true)
              }
            }}
            title={phone ? undefined : t('webChat.headerRename')}
            className="target min-w-0 truncate rounded-control px-2 py-1 text-left text-section transition-colors duration-[var(--duration-fast)] hover:bg-surface-2"
          >
            <h1 className="truncate">{title}</h1>
          </button>
        )}
        {!phone && row ? (
          <div className="hidden min-w-0 items-center gap-1.5 md:flex">
            <span
              className={CHIP}
              aria-label={t('webChat.chipAgent', { value: sessionAgent(row) })}
            >
              {sessionAgent(row)}
            </span>
            <span className={CHIP}>
              <ChannelIcon channel={sessionChannel(row)} />
            </span>
            {project ? (
              <span
                className={cn(CHIP, 'truncate')}
                aria-label={t('webChat.chipProject', {
                  value: projects.data?.get(project) ?? project,
                })}
              >
                {projects.data?.get(project) ?? project}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
      <SimpleTooltip side="bottom" label={t('webChat.headerDetails')}>
        <Button
          size="icon"
          variant="ghost"
          aria-label={t('webChat.headerDetails')}
          aria-pressed={props.detailsOpen}
          onClick={props.onToggleDetails}
          className={cn(ICON_BUTTON, phone && 'hidden')}
        >
          <PanelRightOpen />
        </Button>
      </SimpleTooltip>
      <SimpleTooltip side="bottom" label={t('webChat.headerPanels')}>
        <Button
          size="icon"
          variant="ghost"
          aria-label={t('webChat.headerPanels')}
          onClick={props.onTogglePanels}
          className={ICON_BUTTON}
        >
          <PanelRight />
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
        <DropdownMenuContent align="end">
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
