import { lazy, Suspense, type ReactNode } from 'react'
import { Navigate, useLocation, type RouteObject } from 'react-router'
import { t, type MessageKey } from '@/i18n'
import '@/i18n/en/webShell'
import { McpOAuthCallback } from '@/web/features/settings/McpOAuthCallback'
import { LEGACY_PAGE_PATHS, LEGACY_SETTINGS_PATHS, legacyRedirect } from './legacy-redirect'
import { NotFound, RebuildingPlaceholder, RoutePending } from './fallbacks'
import { RouteErrorBoundary } from './RouteErrorBoundary'

/** Per-route metadata the shell reads (document title, current destination). */
export interface RouteHandle {
  titleKey: MessageKey
  /** The rail / Pages destination this route belongs to. */
  destination: Destination
}

export type Destination =
  'chat' | 'new' | 'home' | 'sessions' | 'projects' | 'usage' | 'automations' | 'inbox' | 'settings'

/** Redirect a legacy URL (by path and query) before rendering a current route. */
function LegacyAware({ children }: { children: ReactNode }) {
  const { pathname, search, hash } = useLocation()
  const target = legacyRedirect(pathname, search)
  if (target) return <Navigate replace to={`${target}${hash}`} />
  return <>{children}</>
}

function LegacyRedirect() {
  const { pathname, search, hash } = useLocation()
  const target = legacyRedirect(pathname, search) ?? '/chat'
  return <Navigate replace to={`${target}${hash}`} />
}

function Rebuilding({ titleKey }: { titleKey: MessageKey }) {
  return <RebuildingPlaceholder name={t(titleKey)} />
}

// Destinations load in their own chunks.
const ChatPage = lazy(async () => ({
  default: (await import('@/web/features/chat/ChatPage')).ChatPage,
}))
const ChatIndex = lazy(async () => ({
  default: (await import('@/web/features/chat/ChatPage')).ChatIndex,
}))

function Lazy({ children }: { children: ReactNode }) {
  return <Suspense fallback={<RoutePending />}>{children}</Suspense>
}

function destination(
  path: string,
  dest: Destination,
  titleKey: MessageKey,
  element: ReactNode = <Rebuilding titleKey={titleKey} />,
): RouteObject {
  const handle: RouteHandle = { titleKey, destination: dest }
  return { path, element: <LegacyAware>{element}</LegacyAware>, handle }
}

/** The current routes, relative to the console base path (D5). */
export const DESTINATION_ROUTES: RouteObject[] = [
  destination(
    'chat',
    'chat',
    'webShell.pageChat',
    <Lazy>
      <ChatIndex />
    </Lazy>,
  ),
  destination(
    'chat/:agent/*',
    'chat',
    'webShell.pageChat',
    <Lazy>
      <ChatPage />
    </Lazy>,
  ),
  destination('new', 'new', 'webShell.pageNew'),
  destination('home', 'home', 'webShell.pageHome'),
  destination('sessions', 'sessions', 'webShell.pageSessions'),
  destination('projects', 'projects', 'webShell.pageProjects'),
  destination('projects/:projectId', 'projects', 'webShell.pageProjects'),
  destination('usage', 'usage', 'webShell.pageUsage'),
  destination('automations', 'automations', 'webShell.pageAutomations'),
  destination('inbox', 'inbox', 'webShell.pageInbox'),
  destination('settings', 'settings', 'webShell.pageSettings'),
  destination('settings/:section', 'settings', 'webShell.pageSettings'),
]

/** Every legacy top-level path that only redirects. */
export const LEGACY_PATHS: string[] = [
  ...Object.keys(LEGACY_PAGE_PATHS),
  ...Object.keys(LEGACY_SETTINGS_PATHS),
]

export const webRouteChildren: RouteObject[] = [
  { index: true, element: <Navigate replace to="chat" /> },
  ...DESTINATION_ROUTES.map((route) => ({ ...route, errorElement: <RouteErrorBoundary /> })),
  // MCP OAuth finishes at this exact path, query intact, before any redirect.
  {
    path: 'mcp/oauth/callback',
    element: <McpOAuthCallback />,
    handle: { titleKey: 'webShell.pageSettings', destination: 'settings' } satisfies RouteHandle,
  },
  ...LEGACY_PATHS.map((path) => ({ path, element: <LegacyRedirect /> })),
  {
    path: '*',
    element: <NotFound />,
    handle: { titleKey: 'webShell.pageNotFound', destination: 'home' } satisfies RouteHandle,
  },
]
