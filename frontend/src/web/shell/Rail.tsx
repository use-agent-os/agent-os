import { House, LayoutGrid, MessagesSquare, Plus, Search } from 'lucide-react'
import { lazy, Suspense, type ReactNode } from 'react'
import { useNavigate } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { cn } from '@/lib/utils'
import { Button } from '@/web/components/ui/button'
import { SimpleTooltip } from '@/web/components/ui/tooltip'
import { InboxButton } from './InboxButton'
import { RailLink } from './RailLink'
import { useShell, type ListView } from './shell-store'

// The two menus load just after first paint, keeping Radix's menu machinery
// out of the entry chunk. Their placeholders have the same box and name.
const AgentSwitcher = lazy(async () => ({
  default: (await import('./AgentSwitcher')).AgentSwitcher,
}))
const StatusMenu = lazy(async () => ({ default: (await import('./StatusMenu')).StatusMenu }))

function MenuPlaceholder({ label }: { label: string }) {
  return <Button variant="ghost" size="icon" aria-label={label} aria-busy="true" />
}

function RailButton({
  label,
  pressed,
  onClick,
  children,
}: {
  label: string
  pressed?: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <SimpleTooltip label={label} side="right">
      <Button
        variant="ghost"
        size="icon"
        aria-label={label}
        aria-pressed={pressed}
        onClick={onClick}
        className={cn(pressed && 'bg-surface-3 text-accent')}
      >
        {children}
      </Button>
    </SimpleTooltip>
  )
}

/**
 * The navigation rail (shell spec "Navigation rail"): agent switcher, New
 * session, Sessions, Pages, Search; Home, Inbox and status at the bottom.
 */
export function Rail({ onNavigate }: { onNavigate?: () => void }) {
  const navigate = useNavigate()
  const view = useShell((s) => s.view)
  const paneOpen = useShell((s) => s.paneOpen)
  const selectView = useShell((s) => s.selectView)
  const activeAgent = useShell((s) => s.activeAgent)
  const setPaletteOpen = useShell((s) => s.setPaletteOpen)
  const selected = (v: ListView) => paneOpen && view === v

  const newSession = () => {
    navigate(activeAgent ? `/new?agent=${encodeURIComponent(activeAgent)}` : '/new')
    onNavigate?.()
  }

  return (
    <nav
      aria-label={t('webShell.railLabel')}
      className="flex h-full w-[var(--rail-width)] shrink-0 flex-col items-center gap-1 border-r border-border bg-surface-1 py-2"
    >
      <Suspense fallback={<MenuPlaceholder label={t('webShell.agentSwitcherLabel')} />}>
        <AgentSwitcher />
      </Suspense>
      <SimpleTooltip label={t('webShell.railNewSession')} side="right">
        <Button
          variant="primary"
          size="icon"
          aria-label={t('webShell.railNewSession')}
          onClick={newSession}
          className="mt-1 mb-2"
        >
          <Plus />
        </Button>
      </SimpleTooltip>
      <RailButton
        label={t('webShell.railSessions')}
        pressed={selected('sessions')}
        onClick={() => selectView('sessions')}
      >
        <MessagesSquare />
      </RailButton>
      <RailButton
        label={t('webShell.railPages')}
        pressed={selected('pages')}
        onClick={() => selectView('pages')}
      >
        <LayoutGrid />
      </RailButton>
      <RailButton label={t('webShell.railSearch')} onClick={() => setPaletteOpen(true)}>
        <Search />
      </RailButton>

      <div className="mt-auto flex flex-col items-center gap-1">
        <SimpleTooltip label={t('webShell.railHome')} side="right">
          <RailLink to="/home" onClick={onNavigate} aria-label={t('webShell.railHome')}>
            <House aria-hidden />
          </RailLink>
        </SimpleTooltip>
        <InboxButton onNavigate={onNavigate} />
        <Suspense fallback={<MenuPlaceholder label={t('webShell.railStatus')} />}>
          <StatusMenu />
        </Suspense>
      </div>
    </nav>
  )
}
