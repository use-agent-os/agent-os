// Shared harness for the web shell's tests: the real route tree under a
// FakeRpc that answers the shell's list calls.
import { useApprovals } from '@/services/approval-monitor'
import { useConnection } from '@/stores/connection'
import { webRoutes } from '@/web/app/WebApp'
import { useProvisionalSessions } from '@/web/shell/session-data'
import { resetShellForTests } from '@/web/shell/shell-store'
import { FakeRpc } from './fake-rpc'
import { renderRoutes } from './render-web'

export const NOW = Date.now()

export function sessionRow(
  key: string,
  extra: Record<string, unknown> = {},
): Record<string, unknown> & { key: string } {
  return { key, updated_at: NOW - 60_000, ...extra }
}

export function shellRpc(sessions: Record<string, unknown>[] = []): FakeRpc {
  return new FakeRpc()
    .handle('sessions.list', () => ({ sessions: structuredClone(sessions) }))
    .handle('agents.list', {
      agents: [
        { id: 'main', name: 'main' },
        { id: 'research', name: 'research' },
      ],
    })
    .handle('projects.list', { projects: [{ project_id: 'p1', name: 'Alpha' }] })
}

export function resetShellState(): void {
  localStorage.clear()
  sessionStorage.clear()
  resetShellForTests()
  useApprovals.getState().clear()
  useProvisionalSessions.setState({ rows: [] })
  useConnection.getState().setState('connected')
}

export function renderShell(path = '/home', rpc: FakeRpc = shellRpc()) {
  return renderRoutes(webRoutes, { initialEntries: [path], rpc })
}
