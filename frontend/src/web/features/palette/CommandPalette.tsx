import { Command } from 'cmdk'
import { CornerDownLeft, FileText, MessageSquare, Settings, Zap } from 'lucide-react'
import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { useShortcutOverlay } from '@/components/KeyboardShortcuts'
import { t } from '@/i18n'
import '@/i18n/en/webPalette'
import '@/i18n/en/webSettings'
import '@/i18n/en/webShell'
import { useConnection } from '@/stores/connection'
import { useTheme } from '@/stores/theme'
import { Dialog, DialogContent, DialogTitle } from '@/web/components/ui/dialog'
import { ALL_SETTINGS_SECTIONS } from '@/web/features/settings/registry'
import { PAGES } from '@/web/shell/pages'
import { exportSessionMarkdown } from '@/web/shell/session-actions'
import { useSessionsList } from '@/web/shell/session-data'
import { sessionIsRunning, sessionTitle } from '@/web/shell/session-list'
import { pathToSessionKey, sessionKeyToPath } from '@/web/shell/session-url'
import { usePhone } from '@/web/shell/use-media-query'
import { useShell } from '@/web/shell/shell-store'
import { buildActions } from './actions'

const MAX_SESSIONS = 50

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

const itemClass = [
  'target flex cursor-default items-center gap-2 rounded-md px-2 py-1.5 text-ui outline-none select-none data-[selected=true]:bg-surface-3 data-[disabled=true]:opacity-60',
  // Focus stays in the input; the highlighted option needs a 3:1 marker, not just surface-3.
  'data-[selected=true]:outline-solid data-[selected=true]:outline-2 data-[selected=true]:-outline-offset-2 data-[selected=true]:outline-ring',
].join(' ')
const groupClass =
  '[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-small [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-text-muted'

/** The ⌘K palette (command-palette spec "Command palette"). */
export function CommandPalette({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const rpc = useRpc()
  const sessions = useSessionsList()
  const connection = useConnection((s) => s.state)
  const setPreference = useTheme((s) => s.setPreference)
  const overview = useShortcutOverlay()
  const phone = usePhone()
  const [query, setQuery] = useState('')

  const currentKey = pathname.startsWith('/chat/') ? pathToSessionKey(pathname) : null
  const currentRow = currentKey ? sessions.rows.find((r) => r.key === currentKey) : undefined

  const close = () => onOpenChange(false)
  const run = (fn: () => void) => {
    close()
    fn()
  }

  const actions = buildActions({
    connection,
    sessionKey: currentKey,
    sessionRunning: currentRow ? sessionIsRunning(currentRow) : false,
    newSession: () => {
      const agent = useShell.getState().activeAgent
      navigate(agent ? `/new?agent=${encodeURIComponent(agent)}` : '/new')
    },
    toggleSidebar: () => {
      const shell = useShell.getState()
      if (phone) shell.setDrawerOpen(!shell.drawerOpen)
      else shell.togglePane()
    },
    toggleDetails: () => useShell.getState().toggleDetails(),
    setTheme: setPreference,
    openShortcuts: overview.open,
    retryConnection: () => rpc.reconnect({ force: true }),
    openInbox: () => navigate('/inbox'),
    compact: (key) => {
      toast.info(t('webPalette.compactStarted'))
      rpc
        .call('sessions.contextCompact', { key })
        .catch((e: unknown) =>
          toast.error(t('webPalette.compactFailed', { message: errorMessage(e) })),
        )
    },
    exportSession: (key) => void exportSessionMarkdown(rpc, key),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        hideClose
        aria-describedby={undefined}
        className="top-[15vh] max-w-xl translate-y-0 gap-0 overflow-hidden p-0"
        onCloseAutoFocus={(e) => {
          // Back to wherever focus was when the palette opened (the composer,
          // a row) — a shortcut-opened dialog has no trigger to return to.
          const target = useShell.getState().paletteReturnFocus
          if (target && target.isConnected) {
            e.preventDefault()
            target.focus()
          }
        }}
      >
        <DialogTitle className="sr-only">{t('webPalette.title')}</DialogTitle>
        <Command label={t('webPalette.title')} loop>
          <Command.Input
            value={query}
            onValueChange={setQuery}
            placeholder={t('webPalette.placeholder')}
            className="h-12 w-full border-b border-border bg-transparent px-4 text-base outline-none placeholder:text-text-muted focus:border-ring"
          />
          <Command.List className="scroll-contained max-h-[min(60dvh,420px)] p-2">
            <Command.Empty className="px-2 py-6 text-center text-text-muted">
              {t('webPalette.empty', { query })}
            </Command.Empty>

            <Command.Group heading={t('webPalette.groupSessions')} className={groupClass}>
              {sessions.isError ? (
                <div role="alert" className="px-2 py-1.5 text-danger">
                  {t('webPalette.sessionsFailed')}
                </div>
              ) : sessions.isPending ? (
                <div className="px-2 py-1.5 text-text-muted">{t('webPalette.sessionsLoading')}</div>
              ) : (
                sessions.rows.slice(0, MAX_SESSIONS).map((row) => (
                  <Command.Item
                    key={row.key}
                    value={`session ${row.key}`}
                    keywords={[sessionTitle(row), row.key]}
                    onSelect={() => run(() => navigate(sessionKeyToPath(row.key)))}
                    className={itemClass}
                  >
                    <MessageSquare className="size-4 shrink-0 text-text-muted" aria-hidden />
                    <span className="truncate">{sessionTitle(row)}</span>
                  </Command.Item>
                ))
              )}
            </Command.Group>

            <Command.Group heading={t('webPalette.groupPages')} className={groupClass}>
              {PAGES.map((page) => (
                <Command.Item
                  key={page.id}
                  value={`page ${page.id}`}
                  keywords={[t(page.titleKey), t(page.descriptionKey)]}
                  onSelect={() => run(() => navigate(page.path))}
                  className={itemClass}
                >
                  <page.icon className="size-4 shrink-0 text-text-muted" aria-hidden />
                  <span>{t(page.titleKey)}</span>
                  <span className="truncate text-small text-text-muted">
                    {t(page.descriptionKey)}
                  </span>
                </Command.Item>
              ))}
            </Command.Group>

            <Command.Group heading={t('webPalette.groupSettings')} className={groupClass}>
              {ALL_SETTINGS_SECTIONS.map((section) => (
                <Command.Item
                  key={section.id}
                  value={`settings ${section.id}`}
                  keywords={[t(section.titleKey), t(section.descriptionKey), ...section.keywords]}
                  onSelect={() => run(() => navigate(`/settings/${section.id}`))}
                  className={itemClass}
                >
                  <Settings className="size-4 shrink-0 text-text-muted" aria-hidden />
                  <span>{t(section.titleKey)}</span>
                  <span className="truncate text-small text-text-muted">
                    {t(section.descriptionKey)}
                  </span>
                </Command.Item>
              ))}
            </Command.Group>

            <Command.Group heading={t('webPalette.groupActions')} className={groupClass}>
              {actions.map((action) => (
                <Command.Item
                  key={action.id}
                  value={`action ${action.id}`}
                  keywords={[t(action.labelKey), ...action.keywords]}
                  disabled={action.gate.disabled}
                  onSelect={() => run(action.run)}
                  className={itemClass}
                >
                  {action.id === 'export' ? (
                    <FileText className="size-4 shrink-0 text-text-muted" aria-hidden />
                  ) : (
                    <Zap className="size-4 shrink-0 text-text-muted" aria-hidden />
                  )}
                  <span>{t(action.labelKey)}</span>
                  {action.gate.reason ? (
                    <span className="ml-auto truncate text-small text-text-muted">
                      {action.gate.reason}
                    </span>
                  ) : null}
                </Command.Item>
              ))}
            </Command.Group>
          </Command.List>
          <div className="flex items-center gap-1 border-t border-border px-3 py-2 text-small text-text-muted">
            <CornerDownLeft className="size-3.5" aria-hidden />
          </div>
        </Command>
      </DialogContent>
    </Dialog>
  )
}
