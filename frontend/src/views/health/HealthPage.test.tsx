import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { HealthPage } from './HealthPage'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
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
    features: { diagnostics: true },
  }),
}))

function renderPage() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <HealthPage />
    </QueryClientProvider>,
  )
}

describe('HealthPage', () => {
  beforeEach(() => {
    // Reset call queues so *Once chains from one test never leak into the next.
    mockRpc.call.mockReset()
    mockRpc.waitForConnection.mockReset().mockResolvedValue(undefined)
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.error).mockClear()
  })
  afterEach(() => {
    localStorage.clear()
  })

  it('calls doctor.status deep for agent main and renders grouped findings', async () => {
    mockRpc.call.mockResolvedValue({
      status: 'degraded',
      ready: true,
      summary: 'Mostly fine',
      impactCounts: { blocks_ready: 0, degrades: 1, optional: 0, none: 3 },
      findings: [
        {
          id: 'memory.slow',
          severity: 'warn',
          readinessImpact: 'degrades',
          surface: 'memory',
          title: 'Memory is slow',
          detail: 'latency high',
          fixSteps: [{ label: 'Restart memory', command: 'agentos gateway restart' }],
        },
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Ready with warnings')).toBeInTheDocument())
    expect(mockRpc.call).toHaveBeenCalledWith('doctor.status', { agentId: 'main', deep: true })
    expect(screen.getByText('Degraded capabilities')).toBeInTheDocument()
    expect(screen.getByText('Memory is slow')).toBeInTheDocument()
    expect(screen.getByText('agentos gateway restart')).toBeInTheDocument()
  })

  it('renders the synthetic gateway.unavailable finding on RPC failure', async () => {
    mockRpc.call.mockRejectedValue(new Error('boom'))
    renderPage()
    // health.js:92-95 + :108 — the string appears twice: once as the rail
    // summary (health-score__summary) and once as the finding title. Legacy set
    // the same "Gateway health report unavailable" sentence on both.
    await waitFor(() =>
      expect(screen.getAllByText('Gateway health report unavailable')).toHaveLength(2),
    )
    // Header summary line stays the distinct wording (health.js:89).
    expect(screen.getByText('Health report unavailable')).toBeInTheDocument()
  })

  it('shows "Gateway health report unavailable" in the readiness rail, not the raw status token', async () => {
    mockRpc.call.mockRejectedValue(new Error('boom'))
    renderPage()
    await waitFor(() => expect(screen.getByText('Health report unavailable')).toBeInTheDocument())
    // The rail readiness summary carries the human sentence; the bare
    // "Unavailable" status token must not stand in for it (health.js:92-95).
    const rail = document.querySelector('.health-score__summary')
    expect(rail?.textContent).toBe('Gateway health report unavailable')
  })

  it('resets to the loading state on Refresh before the refetch settles (health.js:64-74)', async () => {
    let resolveSecond: ((v: unknown) => void) | undefined
    mockRpc.call
      .mockResolvedValueOnce({
        status: 'ready',
        ready: true,
        summary: 'All good',
        findings: [],
      })
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSecond = resolve
          }),
      )
    renderPage()
    // First load settles into the report ('All good' shows in both the header
    // summary line and the rail readiness summary).
    await waitFor(() => expect(screen.getAllByText('All good').length).toBeGreaterThan(0))

    // Refresh: legacy _load blanks the report to the loading placeholders
    // immediately, BEFORE the second deep call settles.
    screen.getByRole('button', { name: /refresh/i }).click()
    await waitFor(() => expect(screen.getByText('Checking readiness')).toBeInTheDocument())
    expect(screen.getByText('Loading health report')).toBeInTheDocument()
    expect(document.querySelector('.health-status__rail.is-loading')).not.toBeNull()
    // Stale report is gone while the refetch is in flight.
    expect(screen.queryByText('All good')).not.toBeInTheDocument()

    // Let the refetch settle and confirm the fresh report renders.
    resolveSecond?.({ status: 'ready', ready: true, summary: 'Still good', findings: [] })
    await waitFor(() => expect(screen.getAllByText('Still good').length).toBeGreaterThan(0))
  })

  it('uses config-target fix steps when the stored wsUrl equals the default (health.js:227-238)', async () => {
    // Legacy saveConnectionSettings stores the default URL itself (app.js:210):
    // a stored-but-equal URL must still count as "uses default".
    localStorage.setItem('agentos.wsUrl', 'ws://127.0.0.1:18791/ws')
    mockRpc.call.mockRejectedValue(new Error('boom'))
    renderPage()
    await waitFor(() =>
      expect(screen.getAllByText('Gateway health report unavailable').length).toBeGreaterThan(0),
    )
    expect(screen.getByText('agentos doctor --config /tmp/agentos.toml --json')).toBeInTheDocument()
    expect(screen.getByText('agentos gateway start --config /tmp/agentos.toml')).toBeInTheDocument()
    // Config context row present in the synthetic error report.
    expect(screen.getByText('Config')).toBeInTheDocument()
  })

  it('uses gateway-target fix steps when the stored wsUrl differs from the default', async () => {
    localStorage.setItem('agentos.wsUrl', 'ws://127.0.0.1:19999/ws')
    mockRpc.call.mockRejectedValue(new Error('boom'))
    renderPage()
    await waitFor(() =>
      expect(screen.getAllByText('Gateway health report unavailable').length).toBeGreaterThan(0),
    )
    expect(
      screen.getByText('agentos doctor --gateway ws://127.0.0.1:19999/ws --json'),
    ).toBeInTheDocument()
    expect(screen.queryByText('Config')).not.toBeInTheDocument()
  })

  it('renders the error immediately without retrying (health.js:64-77: one deep call per load)', async () => {
    mockRpc.call.mockRejectedValue(new Error('boom'))
    // App-level defaults (providers.tsx) set retry: 1 — the health query must
    // override so the deep doctor.status call is never silently duplicated.
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 5_000 } } })}
      >
        <HealthPage />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByText('Health report unavailable')).toBeInTheDocument())
    expect(mockRpc.call).toHaveBeenCalledTimes(1)
  })

  it('reloads fresh on every view entry instead of serving a cached report', async () => {
    mockRpc.call.mockResolvedValue({ status: 'ready', ready: true, findings: [] })
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 5_000 } },
    })
    const first = render(
      <QueryClientProvider client={client}>
        <HealthPage />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledTimes(1))
    first.unmount()
    // gcTime 0 drops the cache on unmount (next macrotask).
    await new Promise((resolve) => setTimeout(resolve, 0))
    render(
      <QueryClientProvider client={client}>
        <HealthPage />
      </QueryClientProvider>,
    )
    // Legacy re-entered through _load: loading state, then a fresh deep call.
    expect(screen.getByText('Checking readiness')).toBeInTheDocument()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledTimes(2))
  })

  it('refetches when Refresh is clicked', async () => {
    mockRpc.call.mockResolvedValue({ status: 'ready', ready: true, findings: [] })
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledTimes(1))
    screen.getByRole('button', { name: /refresh/i }).click()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledTimes(2))
  })

  // M16 — copy-feedback toasts mirror the legacy UI.toast contract as closely
  // as the sonner seam allows: 1600ms ok / 2500ms err durations and a stable
  // per-outcome id so identical visible toasts dedupe instead of stacking.
  function reportWithCopyStep() {
    return {
      status: 'action_required',
      ready: false,
      summary: 'Needs setup',
      findings: [
        {
          id: 'x.fix',
          severity: 'error',
          readinessImpact: 'blocks_ready',
          surface: 'x',
          title: 'Fix me',
          fixSteps: [{ label: 'Do it', command: 'agentos doctor --json' }],
        },
      ],
    }
  }

  async function clickFirstCopyButton() {
    await waitFor(() => expect(screen.getByLabelText('Copy command')).toBeInTheDocument())
    screen.getByLabelText('Copy command').click()
  }

  it('copy success fires a 1600ms ok toast with a stable id (legacy UI.toast ok/1600)', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    mockRpc.call.mockResolvedValue(reportWithCopyStep())
    renderPage()
    await clickFirstCopyButton()
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('agentos doctor --json'))
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        'Copied command',
        expect.objectContaining({ id: 'health-copy-ok', duration: 1600 }),
      ),
    )
  })

  it('copy failure fires a 2500ms err toast with a stable id (legacy UI.toast err/2500)', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    Object.assign(navigator, { clipboard: { writeText } })
    mockRpc.call.mockResolvedValue(reportWithCopyStep())
    renderPage()
    await clickFirstCopyButton()
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'Copy failed: denied',
        expect.objectContaining({ id: 'health-copy-err', duration: 2500 }),
      ),
    )
  })

  it('re-copying reuses the same ok toast id so identical toasts dedupe', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    mockRpc.call.mockResolvedValue(reportWithCopyStep())
    renderPage()
    await clickFirstCopyButton()
    screen.getByLabelText('Copy command').click()
    await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(2))
    const ids = vi.mocked(toast.success).mock.calls.map(([, opts]) => opts?.id)
    expect(ids).toEqual(['health-copy-ok', 'health-copy-ok'])
  })
})
