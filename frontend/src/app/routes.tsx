import { lazy as reactLazy, Suspense, useEffect } from 'react'
import { type RouteObject, useLocation } from 'react-router'
import { RouteErrorBoundary } from './RouteErrorBoundary'

type LazyRoute = NonNullable<RouteObject['lazy']>

interface ViewRoute {
  path: string
  title: string
  lazy: LazyRoute
}

const loadOverview: LazyRoute = async () => ({
  Component: (await import('@/views/overview/OverviewPage')).OverviewPage,
})
const loadHealth: LazyRoute = async () => ({
  Component: (await import('@/views/health/HealthPage')).HealthPage,
})
const loadChat: LazyRoute = async () => ({
  Component: (await import('@/views/chat/ChatPage')).ChatPage,
})
const loadSessions: LazyRoute = async () => ({
  Component: (await import('@/views/sessions/SessionsPage')).SessionsPage,
})
const loadAgents: LazyRoute = async () => ({
  Component: (await import('@/views/agents/AgentsPage')).AgentsPage,
})
const loadCron: LazyRoute = async () => ({
  Component: (await import('@/views/cron/CronPage')).CronPage,
})
const loadUsage: LazyRoute = async () => ({
  Component: (await import('@/views/usage/UsagePage')).UsagePage,
})
const loadSettings: LazyRoute = async () => ({
  Component: (await import('@/views/settings/SettingsPage')).SettingsPage,
})
const loadChannels: LazyRoute = async () => ({
  Component: (await import('@/views/channels/ChannelsPage')).ChannelsPage,
})
const loadMcp: LazyRoute = async () => ({
  Component: (await import('@/views/mcp/McpPage')).McpPage,
})
const loadApprovals: LazyRoute = async () => ({
  Component: (await import('@/views/approvals/ApprovalsPage')).ApprovalsPage,
})
const loadSkills: LazyRoute = async () => ({
  Component: (await import('@/views/skills/SkillsPage')).SkillsPage,
})
const loadLogs: LazyRoute = async () => ({
  Component: (await import('@/views/logs/LogsPage')).LogsPage,
})

const VIEW_ROUTES: ReadonlyArray<ViewRoute> = [
  { path: 'overview', title: 'Overview', lazy: loadOverview },
  { path: 'health', title: 'Health', lazy: loadHealth },
  { path: 'chat', title: 'Chat', lazy: loadChat },
  { path: 'sessions', title: 'Sessions', lazy: loadSessions },
  { path: 'agents', title: 'Agents', lazy: loadAgents },
  { path: 'cron', title: 'Cron', lazy: loadCron },
  { path: 'usage', title: 'Usage', lazy: loadUsage },
  { path: 'settings', title: 'Agent Setup', lazy: loadSettings },
  { path: 'config', title: 'Config', lazy: loadSettings },
  { path: 'setup', title: 'Setup', lazy: loadSettings },
  { path: 'channels', title: 'Channels', lazy: loadChannels },
  { path: 'mcp', title: 'MCP Servers', lazy: loadMcp },
  { path: 'approvals', title: 'Approvals', lazy: loadApprovals },
  { path: 'skills', title: 'Skills', lazy: loadSkills },
  { path: 'logs', title: 'Logs', lazy: loadLogs },
]

export const VIEWS: ReadonlyArray<{ path: string; title: string }> = VIEW_ROUTES.map(
  ({ path, title }) => ({ path, title }),
)

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

// The index route must choose again whenever it is entered, so it cannot use a
// route.lazy function whose resolved module React Router caches. React.lazy
// still keeps both heavy views outside the entry bundle while preserving the
// legacy per-navigation desktop/mobile decision.
const IndexOverview = reactLazy(async () => ({
  default: (await import('@/views/overview/OverviewPage')).OverviewPage,
}))
const IndexChat = reactLazy(async () => ({
  default: (await import('@/views/chat/ChatPage')).ChatPage,
}))

function IndexView() {
  const Component = defaultViewPath() === 'chat' ? IndexChat : IndexOverview
  return (
    <Suspense fallback={<RoutePending />}>
      <Component />
    </Suspense>
  )
}

function NotFound() {
  // Parity: js/router.js:48-55 — path rendered as text, never HTML.
  // useLocation().pathname is basename-relative under react-router.
  const { pathname } = useLocation()
  useEffect(() => {
    document.title = 'Not Found - AgentOS Control'
  }, [])
  return <div className="p-8 text-muted-foreground">{'Page not found: ' + pathname}</div>
}

function RoutePending() {
  return (
    <div className="p-8 text-muted-foreground" aria-hidden="true">
      Opening view…
    </div>
  )
}

function guarded(route: RouteObject): RouteObject {
  return {
    ...route,
    HydrateFallback: route.lazy ? RoutePending : undefined,
    errorElement: <RouteErrorBoundary />,
  }
}

export const routeChildren: RouteObject[] = [
  guarded({ index: true, Component: IndexView }),
  ...VIEW_ROUTES.map(({ path, lazy }) => guarded({ path, lazy })),
  guarded({ path: 'mcp/oauth/callback', lazy: loadMcp }),
  guarded({ path: '*', Component: NotFound }),
]
