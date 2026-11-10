import '@/test/web'
import { act, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readStoredToken, storeToken } from '@/lib/credentials'
import { useConnection } from '@/stores/connection'
import { renderRoutes } from '@/test/render-web'
import { resetShellState, shellRpc } from '@/test/web-shell'
import { webRoutes } from '@/web/app/WebApp'
import { cancelConnectAttempt, CONNECT_TIMEOUT_MS } from '@/web/shell/connection-gate'

beforeEach(() => {
  resetShellState()
  cancelConnectAttempt()
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

function gatewayPage(authMode: string, rpc = shellRpc()) {
  rpc.handle('status', { status: 'running', version: '2026.11.7' })
  return renderRoutes(webRoutes, {
    initialEntries: ['/settings/gateway'],
    rpc,
    bootstrap: { auth_mode: authMode },
  })
}

describe('Settings › Gateway connection', () => {
  it('changes the address without a token when the gateway does not use one', async () => {
    const user = userEvent.setup()
    storeToken('stale')
    const rpc = shellRpc()
    const connect = vi.spyOn(rpc, 'connect')
    gatewayPage('none', rpc)
    const url = await screen.findByLabelText('WebSocket URL')
    expect(screen.getByText(/this gateway does not require a token/)).toBeInTheDocument()
    await user.clear(url)
    await user.type(url, 'ws://10.0.0.5:18791/ws')
    const submit = screen.getByRole('button', { name: 'Connect' })
    expect(submit).toBeEnabled()
    await user.click(submit)
    expect(connect).toHaveBeenCalledWith('ws://10.0.0.5:18791/ws', undefined)
    expect(readStoredToken()).toBe('')
    // Saved only once the new address answers.
    expect(localStorage.getItem('agentos.wsUrl')).toBeNull()
    act(() => rpc.emit('_hello', {}))
    expect(localStorage.getItem('agentos.wsUrl')).toBe('ws://10.0.0.5:18791/ws')
  })

  it('still asks for a token on a token gateway', async () => {
    gatewayPage('token')
    await screen.findByLabelText('WebSocket URL')
    expect(screen.getByRole('button', { name: 'Connect' })).toBeDisabled()
    expect(screen.getByText('Enter a token to connect.')).toBeInTheDocument()
  })

  it('says when the new address cannot be reached and keeps the old one saved', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    localStorage.setItem('agentos.wsUrl', 'ws://10.0.0.1:18791/ws')
    gatewayPage('none')
    const url = await screen.findByLabelText('WebSocket URL')
    await user.clear(url)
    await user.type(url, 'ws://nowhere.invalid/ws')
    await user.click(screen.getByRole('button', { name: 'Connect' }))
    await act(() => vi.advanceTimersByTimeAsync(CONNECT_TIMEOUT_MS))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach nowhere.invalid')
    expect(localStorage.getItem('agentos.wsUrl')).toBe('ws://10.0.0.1:18791/ws')
  })

  it('shows the new address and the outcome once the switch has connected', async () => {
    const user = userEvent.setup()
    const rpc = shellRpc()
    rpc.currentUrl = 'ws://127.0.0.1:28811/ws'
    vi.spyOn(rpc, 'connect').mockImplementation((...args: unknown[]) => {
      rpc.currentUrl = String(args[0])
      rpc.setState('connecting')
      useConnection.getState().setState('connecting')
    })
    gatewayPage('token', rpc)
    const details = await screen.findByText('ws://127.0.0.1:28811/ws')
    expect(details).toBeInTheDocument()
    await user.type(screen.getByLabelText('Gateway token'), 'tokB')
    const url = screen.getByLabelText('WebSocket URL')
    await user.clear(url)
    await user.type(url, 'ws://127.0.0.1:28812/ws')
    await user.click(screen.getByRole('button', { name: 'Connect' }))
    expect(screen.getByText('Connecting with the new credentials…')).toBeInTheDocument()
    act(() => {
      rpc.emit('_hello', {})
      rpc.setState('connected')
      useConnection.getState().setState('connected')
    })
    expect(await screen.findByText('Connected to 127.0.0.1:28812.')).toBeInTheDocument()
    expect(screen.queryByText('Connecting with the new credentials…')).toBeNull()
    expect(screen.getAllByText('ws://127.0.0.1:28812/ws').length).toBeGreaterThan(0)
    expect(screen.queryByText('ws://127.0.0.1:28811/ws')).toBeNull()
    // A new token can be entered for another switch.
    await user.type(screen.getByLabelText('Gateway token'), 'tokC')
    expect(screen.getByRole('button', { name: 'Connect' })).toBeEnabled()
  })

  it('disconnects on request and offers Reconnect', async () => {
    const user = userEvent.setup()
    const rpc = shellRpc()
    const disconnect = vi.spyOn(rpc, 'disconnect').mockImplementation(() => {
      useConnection.getState().setState('disconnected')
    })
    gatewayPage('none', rpc)
    await user.click(await screen.findByRole('button', { name: 'Disconnect' }))
    expect(disconnect).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/Disconnected\. Use Reconnect/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeEnabled()
  })
})
