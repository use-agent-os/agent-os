import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AppProviders } from './providers'

// Class instance methods delegate to shared spies so tests can assert on
// connect() regardless of which WsRpcClient instance AppProviders creates.
const connectMock = vi.fn()
vi.mock('@/lib/ws-rpc', () => ({
  WsRpcClient: class {
    connect = connectMock
    disconnect = vi.fn()
    on = vi.fn(() => () => {})
  },
}))

const BOOTSTRAP = {
  version: '1',
  ws_url: 'ws://127.0.0.1:18791/ws',
  auth_mode: 'token',
  base_path: '/control',
  config_path: '/tmp/agentos.toml',
  features: { diagnostics: true },
}

function stubFetchOk() {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => BOOTSTRAP }))
}

function renderProviders() {
  return render(
    <AppProviders>
      <div>child</div>
    </AppProviders>,
  )
}

describe('AppProviders connection settings', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    sessionStorage.clear()
  })

  it('reads the auth token from sessionStorage, not localStorage (app.js:201)', async () => {
    stubFetchOk()
    sessionStorage.setItem('agentos.wsToken', 'session-tok')
    // A token in localStorage must be ignored — legacy never read that tier.
    localStorage.setItem('agentos.wsToken', 'stale-local-tok')
    renderProviders()
    await waitFor(() =>
      expect(connectMock).toHaveBeenCalledWith('ws://127.0.0.1:18791/ws', 'session-tok'),
    )
  })

  it('connects without a token when sessionStorage has none (app.js:186-190)', async () => {
    stubFetchOk()
    localStorage.setItem('agentos.wsToken', 'stale-local-tok')
    renderProviders()
    await waitFor(() =>
      expect(connectMock).toHaveBeenCalledWith('ws://127.0.0.1:18791/ws', undefined),
    )
  })

  it('prefers the stored wsUrl override over bootstrap ws_url (app.js:197-203)', async () => {
    stubFetchOk()
    localStorage.setItem('agentos.wsUrl', 'ws://10.0.0.5:19999/ws')
    renderProviders()
    await waitFor(() =>
      expect(connectMock).toHaveBeenCalledWith('ws://10.0.0.5:19999/ws', undefined),
    )
  })

  it('renders the shell and connects with the location default when bootstrap fetch rejects', async () => {
    // Legacy bootstrap was server-inlined and could not fail; the shell always
    // rendered and autoConnect used the location-derived default WS URL.
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    renderProviders()
    await waitFor(() => expect(screen.getByText('child')).toBeInTheDocument())
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    expect(connectMock).toHaveBeenCalledWith(`${proto}//${location.host}/ws`, undefined)
  })

  it('renders the shell and connects when bootstrap responds non-ok', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 503 }))
    renderProviders()
    await waitFor(() => expect(screen.getByText('child')).toBeInTheDocument())
    expect(connectMock).toHaveBeenCalledTimes(1)
  })
})
