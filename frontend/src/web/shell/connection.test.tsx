import '@/test/web'
import { act, renderHook, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearPendingGatewayUrl, consumeUrlCredentials, readStoredToken } from '@/lib/credentials'
import { useRuntimeSignals, handshakeFailure, shutdownNotice } from '@/lib/runtime-signals'
import { RpcError } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'
import { useApprovals } from '@/services/approval-monitor'
import { FakeRpc } from '@/test/fake-rpc'
import { renderRoutes } from '@/test/render-web'
import { renderShell, resetShellState, shellRpc } from '@/test/web-shell'
import { webRoutes } from '@/web/app/WebApp'
import { sendOnce } from '@/web/features/chat/send'
import { useConnectAttempt } from './connection-gate'
import { RELOAD_GUARD_KEY, shouldAutoReload } from './ConnectionNotices'
import { useDisplayedConnection } from './use-displayed-connection'

const initialSignals = useRuntimeSignals.getState()

beforeEach(() => {
  resetShellState()
  useRuntimeSignals.setState(initialSignals, true)
  useConnectAttempt.setState({ active: false })
  clearPendingGatewayUrl()
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function tokenGateway(rpc: FakeRpc = shellRpc()) {
  useConnection.getState().setState('disconnected')
  return renderRoutes(webRoutes, {
    initialEntries: ['/home'],
    rpc,
    bootstrap: { auth_mode: 'token' },
  })
}

describe('Connect screen', () => {
  it('replaces the workspace when a token is required and none is stored', async () => {
    const rpc = shellRpc()
    const connect = vi.spyOn(rpc, 'connect')
    tokenGateway(rpc)
    expect(
      await screen.findByRole('heading', { name: 'Connect to your gateway' }),
    ).toBeInTheDocument()
    expect(screen.getByLabelText('Gateway token')).toHaveAttribute('type', 'password')
    expect(screen.queryByRole('navigation', { name: 'Main navigation' })).not.toBeInTheDocument()
    expect(rpc.callsTo('sessions.list')).toEqual([])
    expect(connect).not.toHaveBeenCalled()
  })

  it('keeps the screen and the entered URL when the gateway rejects the token', async () => {
    const user = userEvent.setup()
    const rpc = shellRpc()
    const connect = vi.spyOn(rpc, 'connect')
    tokenGateway(rpc)
    const url = await screen.findByLabelText('WebSocket URL')
    await user.clear(url)
    await user.type(url, 'ws://10.0.0.2:18791/ws')
    await user.type(screen.getByLabelText('Gateway token'), 'wrong')
    await user.click(screen.getByRole('button', { name: 'Connect' }))
    expect(connect).toHaveBeenCalledWith('ws://10.0.0.2:18791/ws', 'wrong')
    expect(readStoredToken()).toBe('wrong')
    act(() => {
      useConnection.getState().setState('disconnected')
      useRuntimeSignals.setState({
        handshake: handshakeFailure(
          Object.assign(new RpcError('bad token'), { code: 'UNAUTHORIZED' }),
          1,
        ),
      })
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('The gateway rejected this token')
    expect(screen.getByLabelText('WebSocket URL')).toHaveValue('ws://10.0.0.2:18791/ws')
    expect(rpc.callsTo('sessions.list')).toEqual([])
  })

  it('disables submission while sign-in is rate limited and does not retry', async () => {
    const rpc = shellRpc()
    const connect = vi.spyOn(rpc, 'connect')
    useRuntimeSignals.setState({
      handshake: handshakeFailure(
        Object.assign(new RpcError('slow down'), {
          code: 'UNAUTHORIZED',
          details: { code: 'AUTH_RATE_LIMITED' },
        }),
        1,
      ),
    })
    tokenGateway(rpc)
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign-in is rate limited')
    expect(screen.getByRole('button', { name: 'Connect' })).toBeDisabled()
    expect(screen.getByLabelText('Gateway token')).toBeDisabled()
    expect(connect).not.toHaveBeenCalled()
  })

  it('opens the workspace once connected', async () => {
    sessionStorage.setItem('agentos.wsToken', 'ok')
    tokenGateway()
    act(() => useConnection.getState().setState('connected'))
    expect(await screen.findByRole('navigation', { name: 'Main navigation' })).toBeInTheDocument()
  })
})

describe('gateway URL from a link', () => {
  function withLink(href: string) {
    window.history.replaceState(null, '', href)
    consumeUrlCredentials()
  }

  it('asks before connecting elsewhere and connects only on confirm', async () => {
    const user = userEvent.setup()
    withLink('/control/home?gatewayUrl=wss%3A%2F%2Fother.example%3A8443%2Fws')
    const rpc = shellRpc()
    const connect = vi.spyOn(rpc, 'connect')
    renderShell('/home', rpc)
    const dialog = await screen.findByRole('alertdialog', {
      name: 'Connect to a different gateway?',
    })
    expect(dialog).toHaveTextContent('other.example:8443')
    expect(connect).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Connect to other.example:8443' }))
    expect(connect).toHaveBeenCalledWith('wss://other.example:8443/ws', undefined)
    expect(localStorage.getItem('agentos.wsUrl')).toBe('wss://other.example:8443/ws')
  })

  it('does nothing when cancelled', async () => {
    const user = userEvent.setup()
    withLink('/control/home?gatewayUrl=wss%3A%2F%2Fother.example%2Fws')
    const rpc = shellRpc()
    const connect = vi.spyOn(rpc, 'connect')
    renderShell('/home', rpc)
    await user.click(await screen.findByRole('button', { name: 'Stay here' }))
    expect(connect).not.toHaveBeenCalled()
    expect(localStorage.getItem('agentos.wsUrl')).toBeNull()
  })

  it('is ignored inside a frame', async () => {
    withLink('/control/home?gatewayUrl=wss%3A%2F%2Fevil.example%2Fws')
    vi.spyOn(window, 'top', 'get').mockReturnValue({} as Window)
    renderShell('/home')
    await screen.findByRole('navigation', { name: 'Main navigation' })
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })
})

describe('status menu', () => {
  async function openStatus(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole('button', { name: /^Status and account/ }))
    return screen.findByTestId('truth-summary')
  }

  it('shows Degraded with Live and offers Open Health on doctor errors', async () => {
    const user = userEvent.setup()
    useRuntimeSignals.setState({
      everConnected: true,
      lastFrameAt: Date.now(),
      doctor: {
        status: 'degraded',
        ready: true,
        counts: { error: 1 },
        findings: [{ severity: 'error', title: 'bad' }],
        fetchedAt: Date.now(),
      },
    })
    const { router } = renderShell()
    const summary = await openStatus(user)
    expect(summary).toHaveTextContent('Degraded')
    expect(summary).toHaveTextContent(/Live/)
    await user.click(screen.getByRole('menuitem', { name: 'Open Health' }))
    expect(router.state.location.pathname).toBe('/settings/health')
  })

  it('shows Offline with a snapshot age and offers Retry connection', async () => {
    const user = userEvent.setup()
    vi.useFakeTimers({ shouldAdvanceTime: true })
    useRuntimeSignals.setState({ everConnected: true, lastFrameAt: Date.now() - 30_000 })
    const rpc = shellRpc()
    const reconnect = vi.spyOn(rpc, 'reconnect')
    renderShell('/home', rpc)
    await screen.findByRole('navigation', { name: 'Main navigation' })
    act(() => useConnection.getState().setState('disconnected'))
    await act(() => vi.advanceTimersByTimeAsync(2_100))
    const summary = await openStatus(user)
    expect(summary).toHaveTextContent('Offline')
    expect(summary).toHaveTextContent(/Snapshot/)
    await user.click(screen.getByRole('menuitem', { name: 'Retry connection' }))
    expect(reconnect).toHaveBeenCalledWith({ force: true })
  })
})

describe('reconnect grace', () => {
  it('never shows a blip shorter than 2 s', () => {
    vi.useFakeTimers()
    useConnection.getState().setState('connected')
    const { result } = renderHook(() => useDisplayedConnection())
    act(() => useConnection.getState().setState('connecting'))
    act(() => vi.advanceTimersByTime(1_000))
    expect(result.current).toBe('connected')
    act(() => useConnection.getState().setState('connected'))
    act(() => vi.advanceTimersByTime(5_000))
    expect(result.current).toBe('connected')
  })

  it('shows the drop once it outlasts the grace period', () => {
    vi.useFakeTimers()
    useConnection.getState().setState('connected')
    const { result } = renderHook(() => useDisplayedConnection())
    act(() => useConnection.getState().setState('disconnected'))
    act(() => vi.advanceTimersByTime(2_000))
    expect(result.current).toBe('disconnected')
  })

  it('delivers a send made during a drop once, after reconnect', async () => {
    const rpc = new FakeRpc()
    let attempts = 0
    rpc.handle('chat.send', (p: Record<string, unknown>) => {
      attempts++
      if (attempts === 1) throw new Error('socket closed')
      return { ok: true, key: p.idempotency_key }
    })
    const outcome = await sendOnce(rpc.asClient(), 'chat.send', { message: 'hi' })
    expect(outcome.status).toBe('sent')
    const keys = rpc.callsTo('chat.send').map((p) => p.idempotency_key)
    expect(keys).toHaveLength(2)
    expect(keys[0]).toBe(keys[1])
  })

  it('reports not sent with a Retry that reuses the key when the socket stays down', async () => {
    const rpc = new FakeRpc()
    rpc.handle('chat.send', () => {
      throw new Error('socket closed')
    })
    rpc.waitForConnection = () => new Promise(() => {})
    const outcome = await sendOnce(
      rpc.asClient(),
      'chat.send',
      { message: 'hi' },
      { reconnectTimeoutMs: 5 },
    )
    expect(outcome.status).toBe('not_sent')
    rpc.handle('chat.send', () => ({ ok: true }))
    rpc.waitForConnection = () => Promise.resolve()
    const retried = await (outcome as Extract<typeof outcome, { status: 'not_sent' }>).retry()
    expect(retried.status).toBe('sent')
    const keys = new Set(rpc.callsTo('chat.send').map((p) => p.idempotency_key))
    expect(keys.size).toBe(1)
  })
})

describe('restart and protocol mismatch', () => {
  it('shows Restarting gateway… without an error, then refreshes on hello', async () => {
    const rpc = shellRpc()
    const { queryClient } = renderShell('/home', rpc)
    await screen.findByRole('navigation', { name: 'Main navigation' })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    act(() => {
      useRuntimeSignals.setState({
        shutdown: shutdownNotice({ reason: 'restart', restart_expected_ms: 3000 }, Date.now()),
      })
      useConnection.getState().setState('disconnected')
    })
    expect(await screen.findByText('Restarting gateway…')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    act(() => {
      useConnection.getState().setState('connected')
      useRuntimeSignals.setState({ shutdown: null })
      rpc.emit('_hello', {})
    })
    await waitFor(() => expect(screen.queryByText('Restarting gateway…')).not.toBeInTheDocument())
    expect(invalidate).toHaveBeenCalled()
  })

  it('offers Reload on a protocol mismatch and reloads at most once per version per minute', async () => {
    const reload = vi.fn()
    useRuntimeSignals.setState({
      handshake: handshakeFailure(
        Object.assign(new RpcError('protocol'), {
          code: 'INVALID_REQUEST',
          details: { code: 'PROTOCOL_MISMATCH', reload_required: true },
        }),
        1,
      ),
    })
    expect(shouldAutoReload('2026.11.7', 1_000)).toBe(true)
    expect(shouldAutoReload('2026.11.7', 30_000)).toBe(false)
    expect(shouldAutoReload('2026.11.7', 70_000)).toBe(true)
    sessionStorage.removeItem(RELOAD_GUARD_KEY)
    renderShell('/home')
    expect(await screen.findByText('A newer console is available')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument()
    void reload
  })
})

describe('update availability', () => {
  function updateRpc() {
    return shellRpc()
      .handle('updates.check', { current: '2026.11.7', latest: '2026.11.9', status: 'outdated' })
      .handle('cron.list', { jobs: [] })
  }

  it('dismisses the notice per version while the menu badge stays', async () => {
    const user = userEvent.setup()
    renderShell('/home', updateRpc())
    expect(await screen.findByText(/AgentOS 2026.11.9 is available/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText(/is available/)).not.toBeInTheDocument()
    expect(localStorage.getItem('agentos.dismissedVersion')).toBe('2026.11.9')
    await user.click(await screen.findByRole('button', { name: /^Status and account/ }))
    expect(await screen.findByRole('menuitem', { name: 'Update available' })).toBeInTheDocument()
  })

  it('lists running sessions before updates.apply', async () => {
    const user = userEvent.setup()
    const rpc = updateRpc()
      .handle('sessions.list', {
        sessions: [
          { key: 'agent:main:webchat:a', active_task: { status: 'running' } },
          { key: 'agent:main:webchat:b', active_task: { status: 'running' } },
          { key: 'agent:main:webchat:c' },
        ],
      })
      .handle('updates.apply', { started: true, status: 'running' })
    useApprovals.getState().clear()
    renderShell('/home', rpc)
    await user.click(await screen.findByRole('button', { name: 'Update now' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Update to 2026.11.9?' })
    const list = await within(dialog).findByTestId('preflight-sessions')
    expect(
      within(list)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['agent:main:webchat:a', 'agent:main:webchat:b'])
    expect(rpc.callsTo('updates.apply')).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Update and restart' }))
    await waitFor(() => expect(rpc.callsTo('updates.apply')).toEqual([{ source: 'auto' }]))
  })
})
