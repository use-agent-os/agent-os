import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useApprovals } from '@/services/approval-monitor'
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
    // The preflight runs first; with nothing running it confirms with "Update".
    const notice = await screen.findByTestId('engine-preflight')
    await within(notice).findByText('0 sessions are running')
    expect(applyEngine).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('engine-preflight-confirm'))
    expect(applyEngine).toHaveBeenCalledTimes(1)
  })

  it('says what the restart interrupts and asks before going ahead', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'status') return { version: '2026.8.23', uptime_ms: 1, active_sessions: 2 }
      if (method === 'sessions.list')
        return {
          sessions: [
            { key: 'a', active_task: { status: 'running' } },
            { key: 'b', run_status: 'queued' },
          ],
        }
      if (method === 'cron.list')
        return [{ id: 'j', name: 'Digest', enabled: true, next_run: Date.now() + 120_000 }]
      return {}
    })
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
    const notice = await screen.findByTestId('engine-preflight')
    await within(notice).findByText('2 sessions are running')
    expect(notice).toHaveTextContent('1 scheduled job is due in the next 10 minutes (Digest)')
    fireEvent.click(screen.getByRole('button', { name: 'Update anyway' }))
    expect(applyEngine).toHaveBeenCalledTimes(1)
  })

  it('blocks the update with the reason while an approval is pending', async () => {
    useApprovals.setState({ pending: [{ id: 'x', namespace: 'exec', command: 'ls' }], count: 1 })
    try {
      useUpdates.setState({
        engine: {
          ...IDLE_ENGINE,
          current: '2026.8.23',
          latest: '2026.9.11',
          availability: 'outdated',
        },
      })
      renderPane()
      fireEvent.click(await screen.findByRole('button', { name: 'Update engine' }))
      const notice = await screen.findByTestId('engine-preflight')
      await within(notice).findByText('1 approval is pending')
      const confirm = screen.getByTestId('engine-preflight-confirm')
      expect(confirm).toBeDisabled()
      expect(notice).toHaveTextContent('1 approval is waiting. Answer it before updating.')
      expect(screen.getByRole('button', { name: 'Show what needs you' })).toBeInTheDocument()
      fireEvent.click(confirm)
      expect(applyEngine).not.toHaveBeenCalled()
    } finally {
      useApprovals.setState({ pending: [], count: 0 })
    }
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

function setExternal() {
  useSettings.setState((s) => ({
    settings: { ...s.settings, gateway: { ...s.settings.gateway, mode: 'external' } },
  }))
}

describe('AboutPane · external gateway', () => {
  it('shows only the remote running version and the latest, with no update button', async () => {
    setExternal()
    rpcCall.mockImplementation(async (method: string) =>
      method === 'status' ? { version: '2026.11.2', uptime_ms: 5000 } : {},
    )
    useUpdates.setState({
      // The engine on this Mac is current; it is not the one running.
      engine: {
        ...IDLE_ENGINE,
        current: '2026.11.3',
        latest: '2026.11.3',
        availability: 'up-to-date',
        checkedAt: 1,
      },
      app: { ...idleAppState('2026.11.3'), phase: 'up-to-date', latest: '2026.11.3' },
    })
    renderPane()
    const card = await screen.findByRole('region', { name: 'Gateway' })
    expect(await within(card).findByText('2026.11.2')).toBeInTheDocument()
    expect(within(card).getByText('2026.11.3')).toBeInTheDocument()
    expect(within(card).queryByText('Installed')).toBeNull()
    expect(within(card).getByText('Update available')).toBeInTheDocument()
    expect(within(card).getByText(/update it on its host/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Update engine' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Update all' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Engine' })).toBeNull()
  })

  it('reads as up to date when the remote gateway runs the latest release', async () => {
    setExternal()
    rpcCall.mockImplementation(async (method: string) =>
      method === 'status' ? { version: '2026.11.3+abc', uptime_ms: 5000 } : {},
    )
    useUpdates.setState({
      // Even an outdated local engine changes nothing here.
      engine: {
        ...IDLE_ENGINE,
        current: '2026.11.2',
        latest: '2026.11.3',
        availability: 'outdated',
        checkedAt: 1,
      },
      app: { ...idleAppState('2026.11.3'), phase: 'up-to-date', latest: '2026.11.3' },
    })
    renderPane()
    const card = await screen.findByRole('region', { name: 'Gateway' })
    await within(card).findByText('2026.11.3+abc')
    expect(within(card).getByText('Up to date')).toBeInTheDocument()
    expect(within(card).queryByText(/update it on its host/i)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Update engine' })).toBeNull()
  })

  it('takes the latest from the app check when the local engine was never checked', async () => {
    setExternal()
    rpcCall.mockImplementation(async (method: string) =>
      method === 'status' ? { version: '2026.11.2' } : {},
    )
    useUpdates.setState({
      engine: { ...IDLE_ENGINE },
      app: { ...idleAppState('2026.11.3'), phase: 'up-to-date', latest: '2026.11.3' },
    })
    renderPane()
    const card = await screen.findByRole('region', { name: 'Gateway' })
    await within(card).findByText('2026.11.2')
    expect(within(card).getByText('2026.11.3')).toBeInTheDocument()
    expect(within(card).getByText('Update available')).toBeInTheDocument()
  })
})

/** Scoped to the App card: the Engine card has a Latest row of its own. */
function appCard(): HTMLElement {
  return screen.getByRole('region', { name: 'App' })
}

function appRow(label: string): HTMLElement {
  const row = within(appCard())
    .getByText(label, { selector: '.stg-row__label > *' })
    .closest('.stg-row')
  if (!row) throw new Error(`App card row ${label} not found`)
  return row as HTMLElement
}

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

  it('says why a refused restart has to wait', () => {
    useUpdates.setState({
      app: {
        ...idleAppState('2026.9.9'),
        phase: 'downloaded',
        latest: '2026.9.12',
        percent: 100,
        blocked: 'engine-updating',
      },
    })
    renderPane()
    expect(screen.getByText(/engine is being updated/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Restart to update' })).toBeInTheDocument()
  })

  it('shows the version being downloaded next to the current one (#3527)', () => {
    useUpdates.setState({
      app: { ...idleAppState('2026.9.28'), phase: 'downloading', latest: '2026.9.29', percent: 42 },
    })
    renderPane()
    expect(appRow('Version')).toHaveTextContent('2026.9.28')
    const latest = appRow('Latest')
    expect(latest).toHaveTextContent('2026.9.29')
    expect(latest.querySelector('.stg-value')).toHaveAttribute('data-tone', 'warn')
    expect(screen.getByTestId('app-download-percent')).toHaveTextContent('42%')
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '42')
  })

  it.each(['available', 'downloaded'] as const)(
    'flags the pending build in the Latest row while %s',
    (phase) => {
      useUpdates.setState({
        app: { ...idleAppState('2026.9.9'), phase, latest: '2026.9.12', checkedAt: 1 },
      })
      renderPane()
      const latest = appRow('Latest')
      expect(latest).toHaveTextContent('2026.9.12')
      expect(latest.querySelector('.stg-value')).toHaveAttribute('data-tone', 'warn')
      expect(screen.queryByTestId('app-download-percent')).not.toBeInTheDocument()
    },
  )

  it('keeps the newer build flagged after its download failed', () => {
    useUpdates.setState({
      app: {
        ...idleAppState('2026.9.28'),
        phase: 'error',
        latest: '2026.9.29',
        checkedAt: 1,
        error: 'net::ERR_CONNECTION_RESET',
      },
    })
    renderPane()
    const latest = appRow('Latest')
    expect(latest).toHaveTextContent('2026.9.29')
    expect(latest.querySelector('.stg-value')).toHaveAttribute('data-tone', 'warn')
  })

  it('shows the checked Latest version without a warning when up to date', () => {
    useUpdates.setState({
      app: { ...idleAppState('2026.9.9'), phase: 'up-to-date', latest: '2026.9.9', checkedAt: 1 },
    })
    renderPane()
    const latest = appRow('Latest')
    expect(latest).toHaveTextContent('2026.9.9')
    expect(latest.querySelector('.stg-value')).not.toHaveAttribute('data-tone')
  })

  it('says the app has not been checked yet before the first check', () => {
    renderPane()
    expect(appRow('Latest')).toHaveTextContent('Not checked yet')
  })

  it('leaves out the Latest row when the build cannot self-update', () => {
    useUpdates.setState({ app: { ...idleAppState('0.1.0'), phase: 'unsupported' } })
    renderPane()
    expect(appRow('Version')).toHaveTextContent('0.1.0')
    expect(appCard().textContent).not.toContain('Latest')
  })

  it('offers Update all when both are outdated, through the engine preflight', async () => {
    const updateAll = vi.fn(async () => {})
    useUpdates.setState({
      updateAll,
      engine: { ...IDLE_ENGINE, latest: '2026.9.12', availability: 'outdated' },
      app: { ...idleAppState('2026.9.9'), phase: 'available', latest: '2026.9.12' },
    })
    renderPane()
    fireEvent.click(screen.getByRole('button', { name: 'Update all' }))
    expect(updateAll).not.toHaveBeenCalled()
    await within(await screen.findByTestId('engine-preflight')).findByText('0 sessions are running')
    fireEvent.click(screen.getByTestId('engine-preflight-confirm'))
    expect(updateAll).toHaveBeenCalledTimes(1)
    expect(applyEngine).not.toHaveBeenCalled()
  })
})
