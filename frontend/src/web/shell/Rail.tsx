import { House, LayoutGrid, MessagesSquare, Plus, Search } from 'lucide-react'
import { lazy, Suspense, type CSSProperties, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
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

/**
 * Props for a rail group whose active item carries one pill and an edge bar
 * that glide between items (shell.css `.rail-track`): items sit on a fixed
 * step, so the position is just the active index.
 */
function track(active: number) {
  return {
    className: 'rail-track',
    'data-active': active >= 0 || undefined,
    style: { '--rail-i': Math.max(active, 0) } as CSSProperties,
  }
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
        className="rail-item"
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
  const { pathname } = useLocation()
  const viewIndex = selected('sessions') ? 0 : selected('pages') ? 1 : -1
  const pageIndex = pathname.startsWith('/home') ? 0 : pathname.startsWith('/inbox') ? 1 : -1

  const newSession = () => {
    navigate(activeAgent ? `/new?agent=${encodeURIComponent(activeAgent)}` : '/new')
    onNavigate?.()
  }

  return (
    <nav
      aria-label={t('webShell.railLabel')}
      className="flex h-full w-[var(--rail-width)] shrink-0 flex-col items-center border-r border-border bg-surface-1 pt-3 pb-2"
    >
      <span aria-hidden className="rail-brand">
        <span className="shell-brand-mark" />
      </span>
      <Suspense fallback={<MenuPlaceholder label={t('webShell.agentSwitcherLabel')} />}>
        <AgentSwitcher />
      </Suspense>
      <SimpleTooltip label={t('webShell.railNewSession')} side="right">
        <Button
          variant="primary"
          size="icon"
          aria-label={t('webShell.railNewSession')}
          onClick={newSession}
          className="rail-new mt-2"
        >
          <Plus />
        </Button>
      </SimpleTooltip>
      <span aria-hidden className="rail-sep" />
      <div {...track(viewIndex)}>
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
      </div>

      <div {...track(pageIndex)} data-bottom>
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
