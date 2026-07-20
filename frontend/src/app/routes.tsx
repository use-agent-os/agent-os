import { Navigate, type RouteObject } from 'react-router'
import { StubView } from '@/views/StubView'
import { HealthPage } from '@/views/health/HealthPage'

export const VIEWS: ReadonlyArray<{ path: string; title: string }> = [
  { path: 'overview', title: 'Overview' },
  { path: 'health', title: 'Health' },
  { path: 'chat', title: 'Chat' },
  { path: 'sessions', title: 'Sessions' },
  { path: 'agents', title: 'Agents' },
  { path: 'cron', title: 'Cron' },
  { path: 'usage', title: 'Usage' },
  { path: 'config', title: 'Config' },
  { path: 'setup', title: 'Setup' },
  { path: 'channels', title: 'Channels' },
  { path: 'approvals', title: 'Approvals' },
  { path: 'skills', title: 'Skills' },
  { path: 'logs', title: 'Logs' },
]

function defaultPath(): string {
  // Parity: js/router.js:32 — mobile lands on chat, desktop on overview.
  try {
    return window.matchMedia('(max-width: 768px)').matches ? '/chat' : '/overview'
  } catch {
    return '/overview'
  }
}

function NotFound() {
  // Parity: js/router.js:48-55 — path rendered as text, never HTML.
  return (
    <div className="p-8 text-muted-foreground">{'Page not found: ' + window.location.pathname}</div>
  )
}

export const routeChildren: RouteObject[] = [
  { index: true, element: <Navigate to={defaultPath()} replace /> },
  ...VIEWS.map((v) =>
    v.path === 'health'
      ? { path: v.path, element: <HealthPage /> }
      : { path: v.path, element: <StubView title={v.title} /> },
  ),
  { path: '*', element: <NotFound /> },
]
