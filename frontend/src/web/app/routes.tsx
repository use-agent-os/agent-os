import { lazy, Suspense, type ReactNode } from 'react'
import { Navigate, useLocation, type RouteObject } from 'react-router'
import type { MessageKey } from '@/i18n'
import '@/i18n/en/webShell'
import { McpOAuthCallback } from '@/web/features/settings/McpOAuthCallback'
import { LEGACY_PAGE_PATHS, LEGACY_SETTINGS_PATHS, legacyRedirect } from './legacy-redirect'
import { NotFound, RoutePending } from './fallbacks'
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

// Destinations load in their own chunks.
const ChatPage = lazy(async () => ({
  default: (await import('@/web/features/chat/ChatPage')).ChatPage,
}))
const SettingsPage = lazy(async () => ({
  default: (await import('@/web/features/settings/SettingsPage')).SettingsPage,
}))
const NewSessionPage = lazy(async () => ({
  default: (await import('@/web/features/chat/NewSessionPage')).NewSessionPage,
}))
const HomePage = lazy(async () => ({
  default: (await import('@/web/features/home/HomePage')).HomePage,
}))
const UsagePage = lazy(async () => ({
  default: (await import('@/web/features/usage/UsagePage')).UsagePage,
}))
const InboxPage = lazy(async () => ({
  default: (await import('@/web/features/inbox/InboxPage')).InboxPage,
}))
const SessionsPage = lazy(async () => ({
  default: (await import('@/web/features/sessions/SessionsPage')).SessionsPage,
}))
const ProjectsPage = lazy(async () => ({
  default: (await import('@/web/features/projects/ProjectsPage')).ProjectsPage,
}))
const AutomationsPage = lazy(async () => ({
  default: (await import('@/web/features/automations/AutomationsPage')).AutomationsPage,
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
  element: ReactNode,
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
  destination(
    'new',
    'new',
    'webShell.pageNew',
    <Lazy>
      <NewSessionPage />
    </Lazy>,
  ),
  destination(
    'home',
    'home',
    'webShell.pageHome',
    <Lazy>
      <HomePage />
    </Lazy>,
  ),
  destination(
    'sessions',
    'sessions',
    'webShell.pageSessions',
    <Lazy>
      <SessionsPage />
    </Lazy>,
  ),
  destination(
    'projects',
    'projects',
    'webShell.pageProjects',
    <Lazy>
      <ProjectsPage />
    </Lazy>,
  ),
  destination(
    'projects/:projectId',
    'projects',
    'webShell.pageProjects',
    <Lazy>
      <ProjectsPage />
    </Lazy>,
  ),
  destination(
    'usage',
    'usage',
    'webShell.pageUsage',
    <Lazy>
      <UsagePage />
    </Lazy>,
  ),
  destination(
    'automations',
    'automations',
    'webShell.pageAutomations',
    <Lazy>
      <AutomationsPage />
    </Lazy>,
  ),
  destination(
    'inbox',
    'inbox',
    'webShell.pageInbox',
    <Lazy>
      <InboxPage />
    </Lazy>,
  ),
  destination(
    'settings',
    'settings',
    'webShell.pageSettings',
    <Lazy>
      <SettingsPage />
    </Lazy>,
  ),
  destination(
    'settings/:section',
    'settings',
    'webShell.pageSettings',
    <Lazy>
      <SettingsPage />
    </Lazy>,
  ),
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
