import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { IDLE_ENGINE, idleAppState } from '@shared/updates'
import { useSettings } from '~/stores/settings'
import { useUpdates } from '~/stores/updates'
import { AboutPane } from './AboutPane'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({ useRpc: () => ({ call: rpcCall }) }))

const applyEngine = vi.fn(async () => {})
const downloadApp = vi.fn(async () => {})
const installApp = vi.fn(async () => {})

function renderPane() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <AboutPane />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  rpcCall.mockReset()
  applyEngine.mockClear()
  downloadApp.mockClear()
  installApp.mockClear()
  useConnection.getState().setState('connected')
  useSettings.setState((s) => ({
    settings: { ...s.settings, gateway: { ...s.settings.gateway, mode: 'managed' } },
  }))
  useUpdates.setState({
    engine: { ...IDLE_ENGINE },
    app: idleAppState('2026.9.9'),
    loaded: true,
    applyEngine,
    downloadApp,
    installApp,
  })
  rpcCall.mockImplementation(async (method: string) => {
    if (method === 'status') return { version: '2026.8.23', uptime_ms: 5000, active_sessions: 0 }
    if (method === 'updates.verifyData')
      return { ok: true, checked: ['a.db'], problems: [], snapshot: null }
    return {}
  })
})

describe('AboutPane · engine', () => {
  it('offers the engine update when a newer release is available', async () => {
    useUpdates.setState({
      engine: {
        ...IDLE_ENGINE,
        current: '2026.8.23',
        latest: '2026.9.11',
        availability: 'outdated',
        checkedAt: 1,
      },
    })
    renderPane()
    expect(await screen.findByText('2026.9.11')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Update engine' }))
    expect(applyEngine).toHaveBeenCalledTimes(1)
  })

  it('asks before interrupting active sessions', async () => {
    rpcCall.mockImplementation(async (method: string) =>
      method === 'status' ? { version: '2026.8.23', uptime_ms: 1, active_sessions: 2 } : {},
    )
    useUpdates.setState({
      engine: {
        ...IDLE_ENGINE,
        current: '2026.8.23',
        latest: '2026.9.11',
        availability: 'outdated',
      },
    })
    renderPane()
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('status'))
    await screen.findAllByText('2026.8.23')
    fireEvent.click(screen.getByRole('button', { name: 'Update engine' }))
    expect(applyEngine).not.toHaveBeenCalled()
    expect(screen.getByText(/2 sessions are active/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Update anyway' }))
    expect(applyEngine).toHaveBeenCalledTimes(1)
  })

  it('points at the terminal for a non-delegated install', () => {
    useUpdates.setState({
      engine: {
        ...IDLE_ENGINE,
        phase: 'error',
        error: 'This install cannot be upgraded by the app.',
        manualCommand: 'pipx install --force "use-agent-os[recommended]"',
      },
    })
    renderPane()
    expect(screen.getByTestId('engine-manual-command')).toHaveTextContent(
      'pipx install --force "use-agent-os[recommended]"',
    )
  })

  it('shows progress while installing and hides the update button', () => {
    useUpdates.setState({
      engine: {
        ...IDLE_ENGINE,
        phase: 'installing',
        availability: 'outdated',
        latest: '2026.9.11',
        log: ['Upgrading use-agent-os via uv-tool from pypi…'],
      },
    })
    renderPane()
    expect(screen.getByText('Installing the new engine…')).toBeInTheDocument()
    expect(screen.getByTestId('engine-log')).toHaveTextContent('Upgrading use-agent-os')
    expect(screen.queryByRole('button', { name: 'Update engine' })).toBeNull()
  })

  it('verifies the restarted gateway over RPC and runs the data check', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'status') return { version: '2026.9.11', uptime_ms: 1, active_sessions: 0 }
      if (method === 'updates.verifyData')
        return { ok: true, checked: ['a.db'], problems: [], snapshot: null }
      return {}
    })
    useUpdates.setState({
      engine: {
        ...IDLE_ENGINE,
        phase: 'done',
        current: '2026.9.11',
        availability: 'up-to-date',
        result: {
          old: '2026.8.23',
          new: '2026.9.11',
          gatewayRestarted: true,
          snapshot: '/snap/pre-upgrade-1',
          source: 'pypi',
        },
      },
    })
    renderPane()
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('updates.verifyData'))
    expect(await screen.findByText(/The gateway is running it\./)).toBeInTheDocument()
    expect(await screen.findByText('Data check passed.')).toBeInTheDocument()
    expect(screen.getByText('/snap/pre-upgrade-1')).toBeInTheDocument()
  })

  it('warns when the connected gateway is older than this app supports', async () => {
    renderPane()
    expect(await screen.findByText(/This app needs engine/)).toBeInTheDocument()
  })
})

describe('AboutPane · app', () => {
  it('explains that a dev build cannot self-update', () => {
    useUpdates.setState({ app: { ...idleAppState('0.1.0'), phase: 'unsupported' } })
    renderPane()
    expect(screen.getByText(/packaged, signed build/)).toBeInTheDocument()
  })

  it('downloads an available build, then offers the restart', () => {
    useUpdates.setState({
      app: { ...idleAppState('2026.9.9'), phase: 'available', latest: '2026.9.12' },
    })
    const view = renderPane()
    fireEvent.click(screen.getByRole('button', { name: 'Download' }))
    expect(downloadApp).toHaveBeenCalledTimes(1)

    useUpdates.setState({
      app: { ...idleAppState('2026.9.9'), phase: 'downloaded', latest: '2026.9.12', percent: 100 },
    })
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <AboutPane />
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Restart to update' }))
    expect(installApp).toHaveBeenCalledTimes(1)
  })

  it('offers Update all when both are outdated', () => {
    const updateAll = vi.fn(async () => {})
    useUpdates.setState({
      updateAll,
      engine: { ...IDLE_ENGINE, latest: '2026.9.12', availability: 'outdated' },
      app: { ...idleAppState('2026.9.9'), phase: 'available', latest: '2026.9.12' },
    })
    renderPane()
    fireEvent.click(screen.getByRole('button', { name: 'Update all' }))
    expect(updateAll).toHaveBeenCalledTimes(1)
  })
})
