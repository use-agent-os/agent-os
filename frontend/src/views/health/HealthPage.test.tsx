import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { HealthPage } from './HealthPage'

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
    await waitFor(() =>
      expect(screen.getByText('Gateway health report unavailable')).toBeInTheDocument(),
    )
    expect(screen.getByText('Health report unavailable')).toBeInTheDocument()
  })

  it('refetches when Refresh is clicked', async () => {
    mockRpc.call.mockResolvedValue({ status: 'ready', ready: true, findings: [] })
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledTimes(1))
    screen.getByRole('button', { name: /refresh/i }).click()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledTimes(2))
  })
})
