import { MoreHorizontal, PanelRight, PanelRightOpen, RotateCcw, Scissors } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import '@/i18n/en/webShell'
import { Button } from '@/web/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'
import { ChannelIcon } from '@/web/shell/channel-icon'
import { exportSessionMarkdown } from '@/web/shell/session-actions'
import { SESSIONS_QUERY_KEY, useProjectNames } from '@/web/shell/session-data'
import {
  sessionAgent,
  sessionChannel,
  sessionProject,
  sessionTitle,
  type SessionRow,
} from '@/web/shell/session-list'

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
    <header className="sticky top-0 z-[var(--z-sticky)] flex min-h-12 items-center gap-2 border-b border-border bg-surface-0/95 px-3 py-1.5 backdrop-blur">
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
              className="h-8 min-w-0 rounded-md border border-border-control bg-surface-1 px-2 font-medium touch:text-base"
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
            className="target min-w-0 truncate rounded px-1 text-left font-semibold hover:bg-surface-2"
          >
            <h1 className="truncate">{title}</h1>
          </button>
        )}
        {!phone && row ? (
          <div className="hidden min-w-0 items-center gap-1 md:flex">
            <span
              className="rounded-full bg-surface-2 px-2 text-small"
              aria-label={t('webChat.chipAgent', { value: sessionAgent(row) })}
            >
              {sessionAgent(row)}
            </span>
            <span className="inline-flex items-center gap-1 rounded-full bg-surface-2 px-2 text-small">
              <ChannelIcon channel={sessionChannel(row)} />
            </span>
            {project ? (
              <span
                className="truncate rounded-full bg-surface-2 px-2 text-small"
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
      <Button
        size="icon"
        variant="ghost"
        aria-label={t('webChat.headerDetails')}
        aria-pressed={props.detailsOpen}
        onClick={props.onToggleDetails}
        className={phone ? 'hidden' : undefined}
      >
        <PanelRightOpen />
      </Button>
      <Button
        size="icon"
        variant="ghost"
        aria-label={t('webChat.headerPanels')}
        onClick={props.onTogglePanels}
      >
        <PanelRight />
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button size="icon" variant="ghost" aria-label={t('webChat.headerMenu')}>
            <MoreHorizontal />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            onSelect={() => {
              setValue(title)
              setEditing(true)
            }}
          >
            {t('webShell.actionRename')}
          </DropdownMenuItem>
          <DropdownMenuItem disabled={props.running} onSelect={props.onCompact}>
            <Scissors aria-hidden />
            {t('webChat.headerCompact')}
          </DropdownMenuItem>
          <DropdownMenuItem disabled={props.running} onSelect={props.onReset}>
            <RotateCcw aria-hidden />
            {t('webChat.headerReset')}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => void exportSessionMarkdown(rpc, sessionKey)}>
            {t('webShell.actionExport')}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => void navigator.clipboard?.writeText(sessionKey)}>
            {t('webShell.actionCopyKey')}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </header>
  )
}
