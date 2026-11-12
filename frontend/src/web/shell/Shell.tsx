import { Menu, Plus } from 'lucide-react'
import { lazy, Suspense, useCallback, useEffect, useRef } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router'
import { pendingGatewayUrlRequest } from '@/lib/credentials'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { listenForPushNavigation } from '@/services/web-push-navigation'
import { Button } from '@/web/components/ui/button'
import { PaletteHost } from '@/web/features/palette/PaletteHost'
import '@/web/styles/shell.css'
import { useConnectGate } from './connection-gate'
import { useEdgeSwipe } from './edge-swipe'
import { Rail } from './Rail'
import { ShellToaster } from './ShellToaster'
import { useShell } from './shell-store'
import { useDocumentTitle } from './use-document-title'
import { useGatewayCacheSync } from './use-gateway-cache-sync'
import { PHONE_QUERY, useMediaQuery } from './use-media-query'
import { useRouteHandle } from './use-destination'

// The list pane (sessions, filters, row menus) and the phone drawer load in
// their own chunk so the rail and the route paint without waiting for them.
const ListPane = lazy(async () => ({ default: (await import('./ListPane')).ListPane }))
const MobileDrawer = lazy(async () => ({ default: (await import('./MobileDrawer')).MobileDrawer }))

// Notices and the approval tray render nothing most of the time; they load
// just after first paint.
const ConnectionNotices = lazy(async () => ({
  default: (await import('./ConnectionNotices')).ConnectionNotices,
}))
const ApprovalTray = lazy(async () => ({ default: (await import('./ApprovalTray')).ApprovalTray }))

// The first-run prompt carries the Settings copy; keep it out of the entry.
const SetupPrompt = lazy(async () => ({ default: (await import('./SetupPrompt')).SetupPrompt }))

// Rare screens: only fetched when a tab actually needs them.
const ConnectScreen = lazy(async () => ({
  default: (await import('./ConnectScreen')).ConnectScreen,
}))
const GatewayUrlConfirmLazy = lazy(async () => ({
  default: (await import('./GatewayUrlConfirm')).GatewayUrlConfirm,
}))

function GatewayUrlConfirm() {
  if (!pendingGatewayUrlRequest()) return null
  return (
    <Suspense fallback={null}>
      <GatewayUrlConfirmLazy />
    </Suspense>
  )
}

function ListPaneFallback() {
  const width = useShell((s) => s.paneWidth)
  return (
    <div
      aria-hidden
      style={{ width }}
      className="h-full shrink-0 border-r border-border bg-surface-1"
    />
  )
}

/**
 * The application shell (D6): `rail | list pane | main` on desktop; a header
 * with a navigation drawer on phones (<= 768 px). Until the gateway accepts
 * this tab's credentials, the Connect screen stands in for all of it.
 */
export function Shell() {
  useGatewayCacheSync()
  const gate = useConnectGate()
  if (gate) {
    return (
      <>
        <Suspense fallback={null}>
          <ConnectScreen reason={gate} />
        </Suspense>
        <GatewayUrlConfirm />
        <ShellToaster />
      </>
    )
  }
  return <Workspace />
}

function Workspace() {
  const phone = useMediaQuery(PHONE_QUERY)
  const paneOpen = useShell((s) => s.paneOpen)
  const setDrawerOpen = useShell((s) => s.setDrawerOpen)
  const activeAgent = useShell((s) => s.activeAgent)
  const navigate = useNavigate()
  const location = useLocation()
  const handle = useRouteHandle()
  const mainRef = useRef<HTMLElement>(null)
  useDocumentTitle()

  // A clicked approval notification (public/sw.js) routes this console to it.
  useEffect(() => listenForPushNavigation((path) => navigate(path)), [navigate])

  // Navigating closes the drawer.
  useEffect(() => {
    setDrawerOpen(false)
  }, [location.pathname, setDrawerOpen])

  const openDrawer = useCallback(() => setDrawerOpen(true), [setDrawerOpen])
  useEdgeSwipe(phone, openDrawer)

  return (
    // `.app-shell` (shell.css): the viewport; only its panes scroll.
    <div className="app-shell flex h-dvh min-h-0 bg-surface-0 text-text">
      {phone ? null : (
        <>
          <Rail />
          {paneOpen ? (
            <Suspense fallback={<ListPaneFallback />}>
              <ListPane />
            </Suspense>
          ) : null}
        </>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        {phone ? (
          <header className="flex h-12 shrink-0 items-center gap-1.5 border-b border-border bg-surface-1 px-1.5 pt-[env(safe-area-inset-top)] shadow-highlight">
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('webShell.drawerOpen')}
              onClick={openDrawer}
              className="text-text-muted hover:text-text"
            >
              <Menu />
            </Button>
            <span aria-hidden className="shell-brand-mark size-4 shrink-0" />
            <span className="min-w-0 flex-1 truncate text-section">
              {handle ? t(handle.titleKey) : t('webShell.appName')}
            </span>
            {/* Quiet on purpose: the page's own primary action leads. */}
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('webShell.railNewSession')}
              className="text-accent hover:bg-accent-tint"
              onClick={() =>
                navigate(activeAgent ? `/new?agent=${encodeURIComponent(activeAgent)}` : '/new')
              }
            >
              <Plus />
            </Button>
          </header>
        ) : null}
        <Suspense fallback={null}>
          <ConnectionNotices />
        </Suspense>
        <Suspense fallback={null}>
          <SetupPrompt />
        </Suspense>
        <main
          id="main-content"
          ref={mainRef}
          tabIndex={-1}
          className="min-h-0 flex-1 overflow-auto outline-none"
        >
          <Outlet />
        </main>
      </div>
      {phone ? (
        <Suspense fallback={null}>
          <MobileDrawer />
        </Suspense>
      ) : null}
      <Suspense fallback={null}>
        <ApprovalTray />
      </Suspense>
      {/* Toasts stack above the docked bar and the tray (shell.css). */}
      <ShellToaster />
      <PaletteHost />
      <GatewayUrlConfirm />
    </div>
  )
}
