import { Command } from 'cmdk'
import { CornerDownLeft, FileText, MessageSquare, Search, Settings, Zap } from 'lucide-react'
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
import { Kbd } from '@/web/components/ui/kbd'
import { useGlide } from '@/web/components/ui/use-glide'
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

// The highlighted row's fill is the list's gliding pill (`.palette-track`);
// the row itself only adds the 3:1 ring, since focus stays in the input.
const itemClass = [
  'target relative flex cursor-default items-center gap-3 rounded-lg px-2 py-1.5 text-ui outline-none select-none data-[disabled=true]:opacity-60',
  'data-[selected=true]:outline-solid data-[selected=true]:outline-2 data-[selected=true]:-outline-offset-2 data-[selected=true]:outline-ring',
].join(' ')
const groupClass =
  '[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-label [&_[cmdk-group-heading]]:text-text-muted'
const tileClass =
  'relative grid size-7 shrink-0 place-items-center rounded-md border border-border bg-surface-2 text-text-muted [&_svg]:size-3.5'

/** The Enter hint that slides in on the highlighted row (shell.css). */
function EnterHint() {
  return (
    <span aria-hidden className="palette-enter ml-auto flex shrink-0 text-text-muted">
      <CornerDownLeft className="size-3.5" />
    </span>
  )
}

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
  const track = useGlide<HTMLDivElement>('[cmdk-item][aria-selected="true"]')

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
      navigate(agent ? `/new?agent=${encodeURIComponent(agent)}` : '/new', {
        viewTransition: true,
      })
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
    openInbox: () => navigate('/inbox', { viewTransition: true }),
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
        data-palette
        aria-describedby={undefined}
        className="top-[14vh] max-w-[620px] translate-y-0 gap-0 overflow-hidden rounded-2xl p-0"
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
          <div className="relative">
            <Search
              className="pointer-events-none absolute top-1/2 left-4 size-[18px] -translate-y-1/2 text-text-muted"
              aria-hidden
            />
            <Command.Input
              value={query}
              onValueChange={setQuery}
              placeholder={t('webPalette.placeholder')}
              className="h-14 w-full border-b border-border bg-transparent pr-16 pl-11 text-[15px] outline-none placeholder:text-text-muted focus:border-ring"
            />
            <Kbd className="absolute top-1/2 right-4 -translate-y-1/2 max-sm:hidden">
              {t('webPalette.keyEscape')}
            </Kbd>
          </div>
          <Command.List className="scroll-contained max-h-[min(60dvh,420px)] p-2">
            <Command.Empty className="flex flex-col items-center gap-2 px-2 py-10 text-center text-text-muted">
              <Search className="size-5" aria-hidden />
              {t('webPalette.empty', { query })}
            </Command.Empty>
            <div
              ref={track}
              className="palette-track glide-track [--glide-bg:var(--surface-3)] [--glide-radius:8px]"
            >
              <Command.Group heading={t('webPalette.groupSessions')} className={groupClass}>
                {sessions.isError ? (
                  <div role="alert" className="px-2 py-1.5 text-danger">
                    {t('webPalette.sessionsFailed')}
                  </div>
                ) : sessions.isPending ? (
                  <div className="px-2 py-1.5 text-text-muted">
                    {t('webPalette.sessionsLoading')}
                  </div>
                ) : (
                  sessions.rows.slice(0, MAX_SESSIONS).map((row) => (
                    <Command.Item
                      key={row.key}
                      value={`session ${row.key}`}
                      keywords={[sessionTitle(row), row.key]}
                      onSelect={() =>
                        run(() => navigate(sessionKeyToPath(row.key), { viewTransition: true }))
                      }
                      className={itemClass}
                    >
                      <span aria-hidden className={tileClass}>
                        <MessageSquare />
                        {sessionIsRunning(row) ? (
                          <span className="absolute -top-1 -right-1 flex rounded-full bg-surface-overlay p-0.5 text-accent">
                            <span data-live className="status-dot motion-status size-1.5" />
                          </span>
                        ) : null}
                      </span>
                      <span className="truncate">{sessionTitle(row)}</span>
                      <EnterHint />
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
                    onSelect={() => run(() => navigate(page.path, { viewTransition: true }))}
                    className={itemClass}
                  >
                    <span aria-hidden className={tileClass}>
                      <page.icon />
                    </span>
                    <span className="shrink-0">{t(page.titleKey)}</span>
                    <span className="truncate text-small text-text-muted">
                      {t(page.descriptionKey)}
                    </span>
                    <EnterHint />
                  </Command.Item>
                ))}
              </Command.Group>

              <Command.Group heading={t('webPalette.groupSettings')} className={groupClass}>
                {ALL_SETTINGS_SECTIONS.map((section) => (
                  <Command.Item
                    key={section.id}
                    value={`settings ${section.id}`}
                    keywords={[t(section.titleKey), t(section.descriptionKey), ...section.keywords]}
                    onSelect={() =>
                      run(() => navigate(`/settings/${section.id}`, { viewTransition: true }))
                    }
                    className={itemClass}
                  >
                    <span aria-hidden className={tileClass}>
                      <Settings />
                    </span>
                    <span className="shrink-0">{t(section.titleKey)}</span>
                    <span className="truncate text-small text-text-muted">
                      {t(section.descriptionKey)}
                    </span>
                    <EnterHint />
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
                    <span aria-hidden className={tileClass}>
                      {action.id === 'export' ? <FileText /> : <Zap />}
                    </span>
                    <span className="shrink-0">{t(action.labelKey)}</span>
                    {action.gate.reason ? (
                      <span className="ml-auto truncate text-small text-text-muted">
                        {action.gate.reason}
                      </span>
                    ) : (
                      <EnterHint />
                    )}
                  </Command.Item>
                ))}
              </Command.Group>
            </div>
          </Command.List>
          <div
            aria-hidden
            className="flex items-center gap-4 border-t border-border bg-surface-2/60 px-4 py-2 text-small text-text-muted max-sm:hidden"
          >
            <span className="flex items-center gap-1.5">
              <Kbd>↑</Kbd>
              <Kbd>↓</Kbd>
              {t('webPalette.hintNavigate')}
            </span>
            <span className="flex items-center gap-1.5">
              <Kbd>↵</Kbd>
              {t('webPalette.hintOpen')}
            </span>
            <span className="ml-auto flex items-center gap-1.5">
              <Kbd>{t('webPalette.keyEscape')}</Kbd>
              {t('webPalette.hintClose')}
            </span>
          </div>
        </Command>
      </DialogContent>
    </Dialog>
  )
}
