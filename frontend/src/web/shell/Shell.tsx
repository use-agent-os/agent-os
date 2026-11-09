import { Menu, Plus } from 'lucide-react'
import { lazy, Suspense, useCallback, useEffect, useRef } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router'
import { pendingGatewayUrlRequest } from '@/lib/credentials'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { listenForPushNavigation } from '@/services/web-push-navigation'
import { Button } from '@/web/components/ui/button'
import { Toaster } from '@/web/components/ui/toaster'
import { PaletteHost } from '@/web/features/palette/PaletteHost'
import { ApprovalTray } from './ApprovalTray'
import { ConnectionNotices } from './ConnectionNotices'
import { SetupPrompt } from './SetupPrompt'
import { useConnectGate } from './connection-gate'
import { useEdgeSwipe } from './edge-swipe'
import { Rail } from './Rail'
import { useShell } from './shell-store'
import { useDocumentTitle } from './use-document-title'
import { PHONE_QUERY, useMediaQuery } from './use-media-query'
import { useRouteHandle } from './use-destination'

// The list pane (sessions, filters, row menus) and the phone drawer load in
// their own chunk so the rail and the route paint without waiting for them.
const ListPane = lazy(async () => ({ default: (await import('./ListPane')).ListPane }))
const MobileDrawer = lazy(async () => ({ default: (await import('./MobileDrawer')).MobileDrawer }))

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
  const gate = useConnectGate()
  if (gate) {
    return (
      <>
        <Suspense fallback={null}>
          <ConnectScreen reason={gate} />
        </Suspense>
        <GatewayUrlConfirm />
        <Toaster />
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
    <div className="flex h-dvh min-h-0 w-full overflow-hidden bg-surface-0 text-text">
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
          <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border bg-surface-1 px-2 pt-[env(safe-area-inset-top)]">
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('webShell.drawerOpen')}
              onClick={openDrawer}
            >
              <Menu />
            </Button>
            <span className="min-w-0 flex-1 truncate font-medium">
              {handle ? t(handle.titleKey) : t('webShell.appName')}
            </span>
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('webShell.railNewSession')}
              onClick={() =>
                navigate(activeAgent ? `/new?agent=${encodeURIComponent(activeAgent)}` : '/new')
              }
            >
              <Plus />
            </Button>
          </header>
        ) : null}
        <ConnectionNotices />
        <SetupPrompt />
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
      <ApprovalTray />
      <PaletteHost />
      <GatewayUrlConfirm />
      <Toaster />
    </div>
  )
}
