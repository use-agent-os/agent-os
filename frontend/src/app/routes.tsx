import { useEffect } from 'react'
import { type RouteObject, useLocation } from 'react-router'
import { StubView } from '@/views/StubView'
import { HealthPage } from '@/views/health/HealthPage'
import { ApprovalsPage } from '@/views/approvals/ApprovalsPage'
import { LogsPage } from '@/views/logs/LogsPage'
import { McpPage } from '@/views/mcp/McpPage'
import { OverviewPage } from '@/views/overview/OverviewPage'
import { ChannelsPage } from '@/views/channels/ChannelsPage'
import { AgentsPage } from '@/views/agents/AgentsPage'
import { SessionsPage } from '@/views/sessions/SessionsPage'
import { UsagePage } from '@/views/usage/UsagePage'
import { SkillsPage } from '@/views/skills/SkillsPage'
import { CronPage } from '@/views/cron/CronPage'
import { SettingsPage } from '@/views/settings/SettingsPage'
import { ChatPage } from '@/views/chat/ChatPage'
import { RouteErrorBoundary } from './RouteErrorBoundary'

export const VIEWS: ReadonlyArray<{ path: string; title: string }> = [
  { path: 'overview', title: 'Overview' },
  { path: 'health', title: 'Health' },
  { path: 'chat', title: 'Chat' },
  { path: 'sessions', title: 'Sessions' },
  { path: 'agents', title: 'Agents' },
  { path: 'cron', title: 'Cron' },
  { path: 'usage', title: 'Usage' },
  { path: 'settings', title: 'Agent Setup' },
  { path: 'config', title: 'Config' },
  { path: 'setup', title: 'Setup' },
  { path: 'channels', title: 'Channels' },
  { path: 'mcp', title: 'MCP Servers' },
  { path: 'approvals', title: 'Approvals' },
  { path: 'skills', title: 'Skills' },
  { path: 'logs', title: 'Logs' },
]

/**
 * Parity: js/router.js:32 — evaluated per resolve, not once at module load.
 * Mobile (<=768px) lands on chat, desktop on overview. Legacy re-reads
 * matchMedia inside `_resolve()` on every navigation, so a viewport change
 * that crosses the breakpoint before the index is (re)visited is honored.
 */
export function defaultViewPath(): string {
  try {
    return window.matchMedia('(max-width: 768px)').matches ? 'chat' : 'overview'
  } catch {
    return 'overview'
  }
}

function viewElement(path: string) {
  const view = VIEWS.find((v) => v.path === path)
  if (path === 'overview') return <OverviewPage />
  if (path === 'health') return <HealthPage />
  if (path === 'approvals') return <ApprovalsPage />
  if (path === 'logs') return <LogsPage />
  if (path === 'channels') return <ChannelsPage />
  if (path === 'mcp') return <McpPage />
  if (path === 'agents') return <AgentsPage />
  if (path === 'sessions') return <SessionsPage />
  if (path === 'usage') return <UsagePage />
  if (path === 'settings' || path === 'config' || path === 'setup') return <SettingsPage />
  if (path === 'skills') return <SkillsPage />
  if (path === 'cron') return <CronPage />
  if (path === 'chat') return <ChatPage />
  return <StubView title={view?.title ?? 'Overview'} />
}

/**
 * Parity: js/router.js:29-66 — the index route renders the *default view in
 * place* while LEAVING the address bar at the base path (legacy never rewrites
 * the URL here; it only picks which view to render and highlights that view's
 * nav item). We therefore render the default view's element directly instead of
 * issuing a <Navigate replace>. AppShell reads defaultViewPath() to highlight
 * the matching nav item, since NavLink cannot mark itself active at the base URL.
 */
function IndexView() {
  return viewElement(defaultViewPath())
}

function NotFound() {
  // Parity: js/router.js:48-55 — path rendered as text, never HTML.
  // Parity: js/router.js:54 — legacy shows the basename-relative path (`rel`),
  // i.e. the path with the base_path stripped. useLocation().pathname is
  // basename-relative under react-router (main.tsx sets basename from
  // BASE_URL), so this restores that legacy display AND — unlike
  // window.location.pathname, which createMemoryRouter never updates — actually
  // reflects the routed path so a hostile path reaches the DOM (as text).
  // Parity: js/router.js:68 — an unmatched route has no meta.title, so the
  // legacy title resolves to 'Not Found - AgentOS Control'.
  const { pathname } = useLocation()
  useEffect(() => {
    document.title = 'Not Found - AgentOS Control'
  }, [])
  return <div className="p-8 text-muted-foreground">{'Page not found: ' + pathname}</div>
}

const unguardedRouteChildren: RouteObject[] = [
  { index: true, element: <IndexView /> },
  ...VIEWS.map((v) => {
    if (v.path === 'overview') return { path: v.path, element: <OverviewPage /> }
    if (v.path === 'health') return { path: v.path, element: <HealthPage /> }
    if (v.path === 'approvals') return { path: v.path, element: <ApprovalsPage /> }
    if (v.path === 'logs') return { path: v.path, element: <LogsPage /> }
    if (v.path === 'channels') return { path: v.path, element: <ChannelsPage /> }
    if (v.path === 'mcp') return { path: v.path, element: <McpPage /> }
    if (v.path === 'agents') return { path: v.path, element: <AgentsPage /> }
    if (v.path === 'sessions') return { path: v.path, element: <SessionsPage /> }
    if (v.path === 'usage') return { path: v.path, element: <UsagePage /> }
    if (v.path === 'settings' || v.path === 'config' || v.path === 'setup') {
      return { path: v.path, element: <SettingsPage /> }
    }
    if (v.path === 'skills') return { path: v.path, element: <SkillsPage /> }
    if (v.path === 'cron') return { path: v.path, element: <CronPage /> }
    if (v.path === 'chat') return { path: v.path, element: <ChatPage /> }
    return { path: v.path, element: <StubView title={v.title} /> }
  }),
  { path: 'mcp/oauth/callback', element: <McpPage /> },
  { path: '*', element: <NotFound /> },
]

// Keep the shell mounted when an individual view fails. React Router otherwise
// falls back to its stack-heavy developer page, which strands users without
// navigation or a recovery path.
export const routeChildren: RouteObject[] = unguardedRouteChildren.map((route) => ({
  ...route,
  errorElement: <RouteErrorBoundary />,
}))
