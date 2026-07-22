import { fireEvent, render as rtlRender, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { Toolbar } from './Toolbar'
import {
  approvalMonitor,
  ELEVATED_MODE_KEY,
  ELEVATED_MODE_VERSION_KEY,
  useApprovals,
} from '@/services/approval-monitor'
import { ROUTER_FX_PREF_KEY, type RouterFxPref } from './transcript/routerFx'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

const mockRpc = {
  waitForConnection: vi.fn().mockResolvedValue(undefined),
  call: vi.fn(),
}
vi.mock('@/app/providers', () => ({
  useRpc: () => mockRpc,
  useBootstrap: () => ({
    version: '1',
    ws_url: 'ws://127.0.0.1:18791/ws',
    auth_mode: 'none',
    base_path: '/control',
    config_path: '/tmp/agentos.toml',
    features: {},
  }),
}))

const SESSION = 'agent:main:webchat:default'

function render(ui: React.ReactElement) {
  return rtlRender(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {ui}
    </QueryClientProvider>,
  )
}

function configWith(overrides: Record<string, unknown> = {}) {
  return {
    agentos_router: { enabled: false, rollout_phase: 'observe' },
    permissions: { default_mode: '' },
    ...overrides,
  }
}

function rpcRouter(config: Record<string, unknown>, usage: unknown = { sessions: [] }) {
  mockRpc.call.mockImplementation((method: string) => {
    if (method === 'config.get') return Promise.resolve(config)
    if (method === 'usage.status') return Promise.resolve(usage)
    if (method === 'config.patch.safe') return Promise.resolve({})
    return Promise.resolve({})
  })
}

function fetchOk(body: unknown = { mode: 'bypass', resolvedPending: 0 }, status = 200) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response)
}

describe('Toolbar', () => {
  beforeEach(() => {
    localStorage.clear()
    useApprovals.setState({ elevatedMode: '' })
    mockRpc.call.mockReset()
    mockRpc.waitForConnection.mockReset().mockResolvedValue(undefined)
    rpcRouter(configWith())
    vi.mocked(toast.info).mockClear()
    vi.mocked(toast.warning).mockClear()
    vi.mocked(toast.error).mockClear()
    vi.mocked(toast.success).mockClear()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders the elevated pill neutral by default', async () => {
    render(<Toolbar sessionKey={SESSION} />)
    expect(await screen.findByRole('button', { name: /approval prompts/i })).toBeInTheDocument()
  })

  it('enabling bypass confirms, POSTs /api/elevated-mode, and persists storage version 2', async () => {
    const fetchSpy = fetchOk()
    vi.stubGlobal('fetch', fetchSpy)
    render(<Toolbar sessionKey={SESSION} />)

    // Click the pill -> a destructive confirm appears (chat.js:1376-1382).
    fireEvent.click(await screen.findByRole('button', { name: /approval prompts/i }))
    const confirm = await screen.findByRole('button', { name: /enable bypass/i })
    fireEvent.click(confirm)

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        '/api/elevated-mode',
        expect.objectContaining({ method: 'POST' }),
      )
    })
    // The POST body carries the session key + the bypass mode (chat.js:2283).
    const body = JSON.parse((fetchSpy.mock.calls[0]![1] as RequestInit).body as string)
    expect(body).toMatchObject({ sessionKey: SESSION, mode: 'bypass' })
    // Shared storage is written under version 2 (chat.js:19,2252-2253).
    expect(localStorage.getItem(ELEVATED_MODE_KEY)).toBe('bypass')
    expect(localStorage.getItem(ELEVATED_MODE_VERSION_KEY)).toBe('2')
    // The pill reflects the active session override.
    expect(await screen.findByRole('button', { name: /session bypass/i })).toHaveClass('is-active')
  })

  it('clearing an active session override POSTs mode=off and removes storage', async () => {
    // Seed an active bypass into the shared store + storage.
    useApprovals.setState({ elevatedMode: 'bypass' })
    localStorage.setItem(ELEVATED_MODE_KEY, 'bypass')
    localStorage.setItem(ELEVATED_MODE_VERSION_KEY, '2')
    const fetchSpy = fetchOk({ mode: 'off', resolvedPending: 0 })
    vi.stubGlobal('fetch', fetchSpy)
    render(<Toolbar sessionKey={SESSION} />)

    // No confirm on clear (chat.js:1372-1374): clicking an active pill clears it.
    fireEvent.click(await screen.findByRole('button', { name: /session bypass/i }))

    await waitFor(() => {
      const body = JSON.parse((fetchSpy.mock.calls[0]![1] as RequestInit).body as string)
      expect(body).toMatchObject({ sessionKey: SESSION, mode: 'off' })
    })
    expect(localStorage.getItem(ELEVATED_MODE_KEY)).toBeNull()
    expect(await screen.findByRole('button', { name: /approval prompts/i })).toBeInTheDocument()
  })

  it('latches "Bypass N/A" and clears storage when the POST returns 403', async () => {
    const fetchSpy = fetchOk({ error: 'owner privileges required' }, 403)
    vi.stubGlobal('fetch', fetchSpy)
    render(<Toolbar sessionKey={SESSION} />)

    fireEvent.click(await screen.findByRole('button', { name: /approval prompts/i }))
    fireEvent.click(await screen.findByRole('button', { name: /enable bypass/i }))

    expect(await screen.findByRole('button', { name: /bypass n\/a/i })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(localStorage.getItem(ELEVATED_MODE_KEY)).toBeNull()
  })

  it('reflects the global default mode from config.get on the pill', async () => {
    rpcRouter(configWith({ permissions: { default_mode: 'on' } }))
    render(<Toolbar sessionKey={SESSION} />)
    expect(await screen.findByRole('button', { name: /global on/i })).toHaveClass('is-active')
  })

  it('loads the Pilot Router checked state from config.get (enabled + full)', async () => {
    rpcRouter(configWith({ agentos_router: { enabled: true, rollout_phase: 'full' } }))
    render(<Toolbar sessionKey={SESSION} />)
    await waitFor(() => {
      expect(screen.getByRole('checkbox', { name: /pilot router/i })).toBeChecked()
    })
  })

  it('toggling Pilot Router calls config.patch.safe with the enabled patches', async () => {
    render(<Toolbar sessionKey={SESSION} />)
    const toggle = await screen.findByRole('checkbox', { name: /pilot router/i })
    await waitFor(() => expect(toggle).not.toBeChecked())

    fireEvent.click(toggle)

    await waitFor(() => {
      expect(mockRpc.call).toHaveBeenCalledWith('config.patch.safe', {
        patches: {
          'agentos_router.enabled': true,
          'agentos_router.rollout_phase': 'full',
        },
      })
    })
  })

  it('reverts the Pilot Router toggle when config.patch.safe fails', async () => {
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'config.get') return Promise.resolve(configWith())
      if (method === 'usage.status') return Promise.resolve({ sessions: [] })
      if (method === 'config.patch.safe') return Promise.reject(new Error('boom'))
      return Promise.resolve({})
    })
    render(<Toolbar sessionKey={SESSION} />)
    const toggle = await screen.findByRole('checkbox', { name: /pilot router/i })
    await waitFor(() => expect(toggle).not.toBeChecked())

    fireEvent.click(toggle)

    await waitFor(() => expect(vi.mocked(toast.error)).toHaveBeenCalled())
    expect(toggle).not.toBeChecked()
  })

  it('re-polls approvals immediately when the elevated-mode POST resolvedPending', async () => {
    // chat.js:2305-2308 — resolvedPending>0 auto-approved pending items, so the
    // badge/modal must refresh now rather than on the next ~1.5s tick.
    const pollSpy = vi.spyOn(approvalMonitor, 'pollNow').mockResolvedValue(undefined)
    const fetchSpy = fetchOk({ mode: 'bypass', resolvedPending: 3 })
    vi.stubGlobal('fetch', fetchSpy)
    render(<Toolbar sessionKey={SESSION} />)

    fireEvent.click(await screen.findByRole('button', { name: /approval prompts/i }))
    fireEvent.click(await screen.findByRole('button', { name: /enable bypass/i }))

    await waitFor(() => expect(pollSpy).toHaveBeenCalledTimes(1))
  })

  it('does NOT re-poll approvals when resolvedPending is 0', async () => {
    const pollSpy = vi.spyOn(approvalMonitor, 'pollNow').mockResolvedValue(undefined)
    const fetchSpy = fetchOk({ mode: 'bypass', resolvedPending: 0 })
    vi.stubGlobal('fetch', fetchSpy)
    render(<Toolbar sessionKey={SESSION} />)

    fireEvent.click(await screen.findByRole('button', { name: /approval prompts/i }))
    fireEvent.click(await screen.findByRole('button', { name: /enable bypass/i }))

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        '/api/elevated-mode',
        expect.objectContaining({ method: 'POST' }),
      )
    })
    expect(pollSpy).not.toHaveBeenCalled()
  })

  it('reflects the stored Visual-effects pref (OFF) on mount', async () => {
    // chat.js:1483-1485 — hydrate the toggle from the stored `agentos-router-fx`.
    localStorage.setItem(ROUTER_FX_PREF_KEY, JSON.stringify({ enabled: false }))
    render(<Toolbar sessionKey={SESSION} />)
    const toggle = await screen.findByRole('checkbox', { name: /visual effects/i })
    expect(toggle).not.toBeChecked()
  })

  it('Visual effects defaults ON when no pref is stored', async () => {
    render(<Toolbar sessionKey={SESSION} />)
    const toggle = await screen.findByRole('checkbox', { name: /visual effects/i })
    expect(toggle).toBeChecked()
  })

  it('toggling Visual effects OFF writes the agentos-router-fx pref shape', async () => {
    // chat.js:1425-1426/3411-3416 — write `{enabled}` under the exact key.
    render(<Toolbar sessionKey={SESSION} />)
    const toggle = await screen.findByRole('checkbox', { name: /visual effects/i })
    expect(toggle).toBeChecked()

    fireEvent.click(toggle)

    await waitFor(() => expect(toggle).not.toBeChecked())
    expect(JSON.parse(localStorage.getItem(ROUTER_FX_PREF_KEY)!)).toEqual({ enabled: false })
    expect(vi.mocked(toast.info)).toHaveBeenCalledWith('Visual effects: OFF')
  })

  it('delegates to the controller setter (live pref) when threaded in', async () => {
    // The controller mutates its LIVE `_routerFx` object + persists, so an
    // already-mounted engine picks up the change (chat.js:1425). Here we assert
    // the toolbar routes the toggle to that setter rather than persisting itself.
    const setEnabled = vi.fn((enabled: boolean) => {
      const pref: RouterFxPref = { enabled, variant: 'default' }
      // Mimic the controller's setRouterFxEnabled: mutate + save.
      localStorage.setItem(ROUTER_FX_PREF_KEY, JSON.stringify({ enabled: pref.enabled }))
    })
    render(<Toolbar sessionKey={SESSION} routerFxEnabled onRouterFxToggle={setEnabled} />)
    const toggle = await screen.findByRole('checkbox', { name: /visual effects/i })
    expect(toggle).toBeChecked()

    fireEvent.click(toggle)

    await waitFor(() => expect(toggle).not.toBeChecked())
    expect(setEnabled).toHaveBeenCalledWith(false)
    expect(JSON.parse(localStorage.getItem(ROUTER_FX_PREF_KEY)!)).toEqual({ enabled: false })
  })

  it('reflects the live routerFxEnabled prop on mount over localStorage', async () => {
    // chat.js:1484-1485 — the toggle mirrors the live `_routerFx.enabled`.
    localStorage.setItem(ROUTER_FX_PREF_KEY, JSON.stringify({ enabled: true }))
    render(<Toolbar sessionKey={SESSION} routerFxEnabled={false} onRouterFxToggle={vi.fn()} />)
    const toggle = await screen.findByRole('checkbox', { name: /visual effects/i })
    expect(toggle).not.toBeChecked()
  })

  it('updates the mounted Visual-effects switch when the live preference prop refreshes', async () => {
    const view = render(<Toolbar sessionKey={SESSION} routerFxEnabled onRouterFxToggle={vi.fn()} />)
    const toggle = await screen.findByRole('checkbox', { name: /visual effects/i })
    expect(toggle).toBeChecked()

    view.rerender(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Toolbar sessionKey={SESSION} routerFxEnabled={false} onRouterFxToggle={vi.fn()} />
      </QueryClientProvider>,
    )

    await waitFor(() => expect(toggle).not.toBeChecked())
  })

  it('renders the usage readout from usage.status for the current session', async () => {
    rpcRouter(configWith(), {
      sessions: [
        {
          session: SESSION,
          input_tokens: 1200,
          output_tokens: 3400,
          cost_usd: 0.0123,
          model: 'gpt-x',
        },
      ],
    })
    render(<Toolbar sessionKey={SESSION} />)
    const readout = await screen.findByRole('group', { name: /session usage/i })
    await waitFor(() => expect(readout).toHaveTextContent(/gpt-x/))
    expect(readout).toHaveTextContent('Session usage')
    expect(readout.querySelector('.chat-toolbar-usage-model-value')).toHaveAttribute(
      'title',
      'gpt-x',
    )
    expect(readout.querySelectorAll('.chat-toolbar-usage-metric')).toHaveLength(3)
    // Input + output tokens surface in the readout.
    expect(readout).toHaveTextContent(/1,?200/)
    expect(readout).toHaveTextContent(/3,?400/)
  })
})
