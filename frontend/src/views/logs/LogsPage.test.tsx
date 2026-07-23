import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { LogsPage } from './LogsPage'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
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

const STATUS_OK = {
  gateway_file_log: { enabled: true, path: '/tmp/debug.log' },
  raw_turn_call_log: { enabled: false, source: 'off', directory: { path: '~/.agentos/logs' } },
  diagnostics_enabled: { effective: false, detail: 'standard' },
}

// A tail response builder: entries + optional cursor.
function tail(lines: unknown[], cursor?: number) {
  return cursor === undefined ? { lines } : { lines, cursor }
}

// Route the two RPC methods. logs.status resolves once; logs.tail is served
// from a FIFO queue, defaulting to an empty tail so extra 3000ms polls never
// throw or change the rendered set.
function wireRpc(opts: { status?: unknown; statusReject?: boolean; tails?: unknown[] }) {
  const queue = [...(opts.tails ?? [])]
  mockRpc.call.mockImplementation((method: string) => {
    if (method === 'logs.status') {
      return opts.statusReject
        ? Promise.reject(new Error('no status'))
        : Promise.resolve(opts.status ?? STATUS_OK)
    }
    if (method === 'logs.tail') {
      return Promise.resolve(queue.length ? queue.shift() : tail([]))
    }
    return Promise.resolve({})
  })
}

function renderPage() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <LogsPage />
    </QueryClientProvider>,
  )
}

describe('LogsPage', () => {
  beforeEach(() => {
    vi.useRealTimers()
    mockRpc.call.mockReset()
    mockRpc.waitForConnection.mockReset().mockResolvedValue(undefined)
    vi.mocked(toast.warning).mockClear()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('calls logs.status and logs.tail after waitForConnection and renders status pills', async () => {
    wireRpc({ tails: [tail(['login INFO ok'])] })
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('logs.status', {}))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('logs.tail', {
        limit: 500,
        cursor: 0,
        level: null,
      }),
    )
    expect(mockRpc.waitForConnection).toHaveBeenCalled()
    await waitFor(() => expect(screen.getByText(/File log on/i)).toBeInTheDocument())
    expect(screen.getByText(/Raw turn-call off/i)).toBeInTheDocument()
  })

  it('keeps status, metrics, filters, and stream inside one observability console', async () => {
    wireRpc({ tails: [tail([{ level: 'info', message: 'ready' }])] })
    renderPage()
    const consoleRegion = await screen.findByLabelText('Live log console')
    expect(within(consoleRegion).getByText('Observability stream')).toBeInTheDocument()
    expect(within(consoleRegion).getByLabelText('Log summary')).toBeInTheDocument()
    expect(within(consoleRegion).getByRole('log')).toBeInTheDocument()
    expect(
      within(consoleRegion).getByRole('searchbox', { name: 'Filter log messages' }),
    ).toBeInTheDocument()
  })

  it('renders "Log status unavailable" when logs.status rejects', async () => {
    wireRpc({ statusReject: true })
    renderPage()
    await waitFor(() => expect(screen.getByText(/Log status unavailable/i)).toBeInTheDocument())
  })

  it('shows the loading placeholder before the first tail resolves', () => {
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'logs.status') return Promise.resolve(STATUS_OK)
      return new Promise(() => {}) // logs.tail never settles
    })
    renderPage()
    expect(screen.getByText(/Loading logs/i)).toBeInTheDocument()
  })

  it('shows "No logs yet." when the first tail is empty', async () => {
    wireRpc({ tails: [tail([])] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No logs yet\./i)).toBeInTheDocument())
  })

  it('renders tail lines and the stat row (in-view vs loaded + error/warn counts)', async () => {
    wireRpc({
      tails: [
        tail([
          { level: 'error', message: 'disk failure', timestamp: '2026-01-01T00:00:00.000000Z' },
          { level: 'warn', message: 'low memory' },
          { level: 'info', message: 'started' },
        ]),
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('disk failure')).toBeInTheDocument())
    expect(screen.getByText('low memory')).toBeInTheDocument()
    const inView = screen.getByLabelText(/in view/i)
    expect(within(inView).getByText('3')).toBeInTheDocument()
    expect(within(inView).getByText(/of 3 loaded/i)).toBeInTheDocument()
    const errors = screen.getByLabelText(/^errors$/i)
    expect(within(errors).getByText('1')).toBeInTheDocument()
    const warns = screen.getByLabelText(/^warnings$/i)
    expect(within(warns).getByText('1')).toBeInTheDocument()
  })

  it('advances the cursor from data.cursor on the next 3000ms poll (limit 500)', async () => {
    vi.useFakeTimers()
    wireRpc({
      tails: [
        tail([{ level: 'info', message: 'a' }], 42),
        tail([{ level: 'info', message: 'b' }], 99),
      ],
    })
    renderPage()
    await vi.waitFor(() => expect(screen.getByText('a')).toBeInTheDocument())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    await vi.waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('logs.tail', {
        limit: 500,
        cursor: 42,
        level: null,
      }),
    )
    vi.useRealTimers()
  })

  it('does not overlap polls while one is in flight (pollInFlightRef guard)', async () => {
    // logs.js:174-176 — `if (!_el || _pollInFlight) return`. The 3000ms interval
    // only starts polling *after* the first tail resolves (LogsPage.tsx awaits
    // the initial poll before setInterval), so to exercise the guard we let the
    // first poll settle, then hold the SECOND poll (fired by the first interval
    // tick) open. A further interval tick lands while that poll is still in
    // flight; the guard must short-circuit it, so no third logs.tail is issued
    // until the held poll resolves.
    vi.useFakeTimers()
    let resolveHeld: ((v: unknown) => void) | undefined
    let tailCalls = 0
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'logs.status') return Promise.resolve(STATUS_OK)
      if (method === 'logs.tail') {
        tailCalls += 1
        // First poll resolves immediately so the interval is established.
        if (tailCalls === 1) return Promise.resolve(tail([{ level: 'info', message: 'first' }]))
        // Second poll (first interval tick) is held open to keep a poll in flight.
        if (tailCalls === 2) {
          return new Promise((resolve) => {
            resolveHeld = resolve
          })
        }
        return Promise.resolve(tail([{ level: 'info', message: 'later' }]))
      }
      return Promise.resolve({})
    })
    renderPage()

    // First poll resolves -> interval established.
    await vi.waitFor(() => expect(tailCalls).toBe(1))

    // First interval tick fires the second poll, which hangs (in flight).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(tailCalls).toBe(2)

    // A further interval tick lands while the second poll is still pending. If
    // the guard were absent this would issue a third overlapping logs.tail; the
    // pollInFlightRef short-circuit must block it, so tailCalls stays at 2.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(tailCalls).toBe(2)

    // Resolve the held poll; the guard clears and the next interval tick is free
    // to issue a real third call.
    await act(async () => {
      resolveHeld?.(tail([{ level: 'info', message: 'unblock' }]))
      await Promise.resolve()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(tailCalls).toBe(3)
    vi.useRealTimers()
  })

  it('filters lines by the search input', async () => {
    wireRpc({
      tails: [
        tail([
          { level: 'info', message: 'alpha login' },
          { level: 'info', message: 'beta logout' },
        ]),
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('alpha login')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText(/filter log messages/i), { target: { value: 'beta' } })
    await waitFor(() => expect(screen.queryByText('alpha login')).not.toBeInTheDocument())
    // The matched term is split into a <mark> + trailing text node.
    expect(screen.getByText('beta', { selector: 'mark' })).toBeInTheDocument()
    expect(screen.getByText(/logout/)).toBeInTheDocument()
  })

  it('toggling a level chip re-filters the visible lines (TRACE hidden by default)', async () => {
    wireRpc({
      tails: [
        tail([
          { level: 'trace', message: 'trace noise' },
          { level: 'info', message: 'info line' },
        ]),
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('info line')).toBeInTheDocument())
    expect(screen.queryByText('trace noise')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /toggle trace level/i }))
    await waitFor(() => expect(screen.getByText('trace noise')).toBeInTheDocument())
  })

  it('shows the no-match placeholder when the filter excludes every line', async () => {
    wireRpc({ tails: [tail([{ level: 'info', message: 'only line' }])] })
    renderPage()
    await waitFor(() => expect(screen.getByText('only line')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText(/filter log messages/i), {
      target: { value: 'zzz-nomatch' },
    })
    await waitFor(() =>
      expect(screen.getByText(/No lines match the current filter\./i)).toBeInTheDocument(),
    )
  })

  it('auto-follow is on by default and can be toggled off', async () => {
    wireRpc({ tails: [tail([{ level: 'info', message: 'x' }])] })
    renderPage()
    await waitFor(() => expect(screen.getByText('x')).toBeInTheDocument())
    const toggle = screen.getByLabelText(/auto-follow/i) as HTMLInputElement
    expect(toggle.checked).toBe(true)
    fireEvent.click(toggle)
    expect(toggle.checked).toBe(false)
  })

  it('scrolls the stream to the bottom when auto-follow is on and new lines render', async () => {
    wireRpc({ tails: [tail([{ level: 'info', message: 'x' }])] })
    renderPage()
    const display = screen.getByRole('log')
    Object.defineProperty(display, 'scrollHeight', { value: 999, configurable: true })
    await waitFor(() => expect(screen.getByText('x')).toBeInTheDocument())
    // The autoscroll effect pushes scrollTop to scrollHeight after lines render.
    await waitFor(() => expect(display.scrollTop).toBe(999))
  })

  it('disabling auto-follow stops the autoscroll when new lines append', async () => {
    // logs.js:86-90,332-335 — the autoscroll effect is gated on _autoFollow, so
    // with the toggle off a fresh batch of lines must NOT move scrollTop.
    vi.useFakeTimers()
    wireRpc({
      tails: [tail([]), tail([{ level: 'info', message: 'appended' }])],
    })
    renderPage()
    // First (empty) tail lands: no lines yet, nothing scrolled.
    await vi.waitFor(() => expect(screen.getByText(/No logs yet\./i)).toBeInTheDocument())

    const display = screen.getByRole('log')
    Object.defineProperty(display, 'scrollHeight', { value: 999, configurable: true })
    display.scrollTop = 0

    // Turn auto-follow off before any lines render.
    const toggle = screen.getByLabelText(/auto-follow/i) as HTMLInputElement
    fireEvent.click(toggle)
    expect(toggle.checked).toBe(false)

    // The next 3000ms poll appends a line; with auto-follow off the layout
    // effect early-returns, so scrollTop is left untouched at 0.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    await vi.waitFor(() => expect(screen.getByText('appended')).toBeInTheDocument())
    expect(display.scrollTop).toBe(0)
    vi.useRealTimers()
  })

  it('highlights the search term inside matching messages', async () => {
    wireRpc({ tails: [tail([{ level: 'info', message: 'error and Error' }])] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/error and Error/i)).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText(/filter log messages/i), { target: { value: 'error' } })
    await waitFor(() => expect(document.querySelectorAll('mark').length).toBeGreaterThanOrEqual(2))
  })

  it('export builds the filtered log text and triggers a download', async () => {
    wireRpc({
      tails: [
        tail([{ level: 'error', message: 'boom', timestamp: '2026-01-01T00:00:00.000000Z' }]),
      ],
    })
    const createObjectURL = vi.fn(() => 'blob:mock')
    const revokeObjectURL = vi.fn()
    Object.assign(URL, { createObjectURL, revokeObjectURL })
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => undefined)
    renderPage()
    await waitFor(() => expect(screen.getByText('boom')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /export/i }))
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(clickSpy).toHaveBeenCalled()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock')
    clickSpy.mockRestore()
  })

  it('toasts once on a failing tail poll and not again until a poll succeeds', async () => {
    vi.useFakeTimers()
    let failing = true
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'logs.status') return Promise.resolve(STATUS_OK)
      if (failing) return Promise.reject(new Error('tail boom'))
      return Promise.resolve(tail([{ level: 'info', message: 'recovered' }]))
    })
    renderPage()
    await vi.waitFor(() => expect(toast.warning).toHaveBeenCalledTimes(1))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(toast.warning).toHaveBeenCalledTimes(1)
    failing = false
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    await vi.waitFor(() => expect(screen.getByText('recovered')).toBeInTheDocument())
    vi.useRealTimers()
  })

  it('sets the document title', async () => {
    wireRpc({ tails: [tail([])] })
    renderPage()
    await waitFor(() => expect(document.title).toBe('Logs - AgentOS Control'))
  })
})
