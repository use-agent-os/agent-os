import '@/test/web'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readStoredToken } from '@/lib/credentials'
import { RpcError } from '@/lib/ws-rpc'
import { useTheme } from '@/stores/theme'
import { renderRoutes } from '@/test/render-web'
import { renderShell, resetShellState, sessionRow, shellRpc } from '@/test/web-shell'
import { webRoutes } from '@/web/app/WebApp'
import { DISMISSED_VERSION_KEY } from '@/web/shell/updates'

beforeEach(() => {
  resetShellState()
  useTheme.getState().setPreference('dark')
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Appearance', () => {
  it('applies the theme at once, with no save bar', async () => {
    const user = userEvent.setup()
    renderShell('/settings/appearance')
    const light = await screen.findByRole('radio', { name: 'Light' })
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    await user.click(light)
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
    expect(useTheme.getState().preference).toBe('light')
    expect(light).toBeChecked()
    expect(screen.queryByText(/unsaved change/i)).not.toBeInTheDocument()
  })
})

describe('Gateway', () => {
  function gatewayRpc() {
    return shellRpc()
      .handle('status', { status: 'running', version: '2026.11.7', uptime_ms: 3_600_000 })
      .handle('gateway.restart', { ok: true })
  }

  it('shows the connection details', async () => {
    renderShell('/settings/gateway', gatewayRpc())
    expect(await screen.findByText('ws://127.0.0.1:18791/ws')).toBeInTheDocument()
    expect(await screen.findByText('1 h 0 min')).toBeInTheDocument()
  })

  it('reconnects with force', async () => {
    const user = userEvent.setup()
    const rpc = gatewayRpc()
    const reconnect = vi.spyOn(rpc, 'reconnect')
    renderShell('/settings/gateway', rpc)
    await user.click(await screen.findByRole('button', { name: 'Reconnect' }))
    expect(reconnect).toHaveBeenCalledWith({ force: true })
  })

  it('restarts the gateway only after confirming', async () => {
    const user = userEvent.setup()
    const rpc = gatewayRpc()
    renderShell('/settings/gateway', rpc)
    await user.click(await screen.findByRole('button', { name: 'Restart gateway' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Restart the gateway?' })
    expect(rpc.callsTo('gateway.restart')).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Restart' }))
    await waitFor(() => expect(rpc.callsTo('gateway.restart')).toEqual([{ reason: 'settings' }]))
    expect(await screen.findByText(/Restart requested/)).toBeInTheDocument()
  })

  it('connects with new credentials and stores the token for this tab', async () => {
    const user = userEvent.setup()
    const rpc = gatewayRpc()
    const connect = vi.spyOn(rpc, 'connect')
    renderShell('/settings/gateway', rpc)
    const token = await screen.findByLabelText('Gateway token')
    expect(token).toHaveAttribute('type', 'password')
    await user.type(token, 'secret-token')
    await user.click(screen.getByRole('button', { name: 'Connect' }))
    expect(connect).toHaveBeenCalledWith('ws://127.0.0.1:18791/ws', 'secret-token')
    expect(readStoredToken()).toBe('secret-token')
  })
})

describe('Health', () => {
  it('groups findings with numbered fix steps and copyable commands', async () => {
    const rpc = shellRpc().handle('doctor.status', {
      status: 'action_required',
      ready: false,
      summary: '1 issue blocks readiness',
      counts: { error: 1, warn: 0, info: 0, ok: 2 },
      findings: [
        {
          id: 'provider.missing',
          severity: 'error',
          surface: 'provider',
          title: 'No provider configured',
          detail: 'The agent cannot answer.',
          evidence: { configPath: '/home/me/.agentos/config.yaml' },
          fixSteps: [{ label: 'Configure a provider', command: 'agentos setup' }],
        },
      ],
    })
    renderShell('/settings/health', rpc)
    const group = await screen.findByRole('region', { name: 'Action required' })
    expect(within(group).getByText('No provider configured')).toBeInTheDocument()
    expect(within(group).getByText('agentos setup')).toBeInTheDocument()
    expect(within(group).getByRole('button', { name: 'Copy command' })).toBeInTheDocument()
    expect(rpc.callsTo('doctor.status')).toEqual([{ agentId: 'main', deep: true }])
  })
})

describe('Logs', () => {
  it('shows the tail and filters it by search', async () => {
    const user = userEvent.setup()
    const rpc = shellRpc()
      .handle('logs.status', { gateway_file_log: { enabled: true } })
      .handle('logs.tail', (params: Record<string, unknown>) =>
        params.cursor === 0
          ? { cursor: 2, lines: ['INFO gateway started', 'ERROR provider timed out'] }
          : { cursor: 2, lines: [] },
      )
    renderShell('/settings/logs', rpc)
    const log = await screen.findByRole('log', { name: 'Gateway log' })
    expect(await within(log).findByText('ERROR provider timed out')).toBeInTheDocument()
    expect(screen.getByText('File log on')).toBeInTheDocument()
    await user.type(screen.getByRole('searchbox', { name: 'Search logs' }), 'started')
    expect(within(log).queryByText('ERROR provider timed out')).not.toBeInTheDocument()
    expect(rpc.callsTo('logs.tail')[0]).toEqual({ limit: 500, cursor: 0, level: null })
  })
})

describe('Debug', () => {
  function debugRpc() {
    return shellRpc()
      .handle('status', { status: 'running', version: '2026.11.7' })
      .handle('health', { ok: true })
      .handle('doctor.status', { status: 'ready' })
      .handle('models.list', { models: [] })
      .handle('last-heartbeat', { at: 1 })
  }

  it('shows snapshots and events, and no manual RPC tool without diagnostics', async () => {
    const rpc = debugRpc()
    renderShell('/settings/debug', rpc)
    for (const method of ['status', 'health', 'doctor.status', 'models.list', 'last-heartbeat']) {
      expect(await screen.findByRole('region', { name: method })).toBeInTheDocument()
    }
    rpc.emit('cron.run.finished', { id: 'job-1' })
    const events = await screen.findByRole('list', { name: 'Gateway events, newest first' })
    expect(within(events).getByText('cron.run.finished')).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Method' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Send' })).not.toBeInTheDocument()
  })

  it('sends a manual call and shows the formatted response or the error', async () => {
    const user = userEvent.setup()
    const rpc = debugRpc().handle('boom', () => {
      const error = new RpcError('nope')
      error.code = 'METHOD_NOT_FOUND'
      throw error
    })
    renderRoutes(webRoutes, {
      initialEntries: ['/settings/debug'],
      rpc,
      bootstrap: { features: { diagnostics: true } },
    })
    await user.type(await screen.findByRole('textbox', { name: 'Method' }), 'status')
    expect(screen.getByRole('textbox', { name: 'Parameters (JSON)' })).toHaveValue('{}')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    const result = await screen.findByTestId('debug-rpc-result')
    expect(result.textContent).toBe(
      JSON.stringify({ status: 'running', version: '2026.11.7' }, null, 2),
    )
    expect(rpc.callsTo('status').at(-1)).toEqual({})

    await user.clear(screen.getByRole('textbox', { name: 'Method' }))
    await user.type(screen.getByRole('textbox', { name: 'Method' }), 'boom')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() =>
      expect(screen.getByTestId('debug-rpc-result')).toHaveTextContent('METHOD_NOT_FOUND: nope'),
    )
  })
})

describe('Updates', () => {
  it('lists running sessions in the preflight before updates.apply', async () => {
    const user = userEvent.setup()
    const rpc = shellRpc([sessionRow('agent:main:busy', { active_task: { status: 'running' } })])
      .handle('updates.check', { current: '2026.11.7', latest: '2026.12.1', status: 'outdated' })
      .handle('cron.list', { jobs: [] })
      .handle('updates.apply', { started: true, status: 'running' })
      .handle('updates.status', { status: 'running' })
    // The shell's own update banner stays out of the way.
    localStorage.setItem(DISMISSED_VERSION_KEY, '2026.12.1')
    renderShell('/settings/updates', rpc)
    expect(await screen.findByText('Version 2026.12.1 is available')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Update now' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(await within(dialog).findByTestId('preflight-sessions')).toHaveTextContent(
      'agent:main:busy',
    )
    expect(rpc.callsTo('updates.apply')).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Update and restart' }))
    await waitFor(() => expect(rpc.callsTo('updates.apply')).toEqual([{ source: 'auto' }]))
    expect(await screen.findByText(/Updating…/)).toBeInTheDocument()
  })
})

describe('About', () => {
  it('shows the version and the bundled third-party licenses', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) =>
      String(input).endsWith('THIRD_PARTY_LICENSES.txt')
        ? new Response('react 19.0.0\nMIT License', { status: 200 })
        : new Response('', { status: 404 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    renderShell('/settings/about')
    expect(await screen.findByTestId('about-version')).toHaveTextContent('2026.11.7')
    const ledger = await screen.findByLabelText('Third-party licenses')
    expect(ledger).toHaveTextContent('react 19.0.0')
    expect(ledger).toHaveTextContent('MIT License')
  })

  it('offers Retry when the ledger cannot be loaded', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('', { status: 404 })),
    )
    renderShell('/settings/about')
    expect(await screen.findByText('Could not load the license list')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })
})
