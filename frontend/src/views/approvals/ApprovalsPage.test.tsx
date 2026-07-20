import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { ApprovalsPage } from './ApprovalsPage'
import { useApprovals, type Approval } from '@/services/approval-monitor'
import { approvalMonitor, saveApprovalMode } from '@/services/approval-monitor'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}))

// Drive resolution + settings save + the re-poll through the singleton service;
// spy on the mutation surface but keep the real useApprovals store + helpers.
vi.mock('@/services/approval-monitor', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/approval-monitor')>()
  return {
    ...actual,
    approvalMonitor: {
      resolve: vi.fn().mockResolvedValue(undefined),
      pollNow: vi.fn().mockResolvedValue(undefined),
    },
    saveApprovalMode: vi.fn().mockResolvedValue(undefined),
  }
})

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

const resolveSpy = vi.mocked(approvalMonitor.resolve)
const pollNowSpy = vi.mocked(approvalMonitor.pollNow)
const saveModeSpy = vi.mocked(saveApprovalMode)

function setStore(pending: Approval[], mode = 'prompt') {
  useApprovals.setState({ pending, count: pending.length, mode })
}

function renderPage() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ApprovalsPage />
    </QueryClientProvider>,
  )
}

describe('ApprovalsPage', () => {
  beforeEach(() => {
    mockRpc.call.mockReset().mockResolvedValue({ permissions: { default_mode: '' } })
    mockRpc.waitForConnection.mockReset().mockResolvedValue(undefined)
    resolveSpy.mockReset().mockResolvedValue(undefined)
    pollNowSpy.mockReset().mockResolvedValue(undefined)
    saveModeSpy.mockReset().mockResolvedValue(undefined)
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.warning).mockClear()
    vi.mocked(toast.error).mockClear()
    setStore([], 'prompt')
    localStorage.clear()
  })
  afterEach(() => {
    setStore([], 'prompt')
    localStorage.clear()
  })

  it('renders the header, strategy radiogroup, and pending count from the store', async () => {
    setStore(
      [
        {
          id: 'a1',
          namespace: 'exec',
          toolName: 'shell',
          command: 'rm -rf /tmp/x',
          sessionKey: 's-1',
        },
      ],
      'prompt',
    )
    renderPage()
    expect(screen.getByRole('heading', { name: 'Approvals' })).toBeInTheDocument()
    // Strategy radiogroup with all three options.
    const group = screen.getByRole('radiogroup', { name: /approval strategy/i })
    expect(group).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /ask every time/i })).toBeChecked()
    // Pending stat reflects the store count.
    expect(screen.getByText('shell')).toBeInTheDocument()
    expect(screen.getByText('rm -rf /tmp/x')).toBeInTheDocument()
  })

  it('re-polls the monitor on mount so the durable pending list is fresh', async () => {
    renderPage()
    await waitFor(() => expect(pollNowSpy).toHaveBeenCalled())
  })

  it('shows the empty state when there are no pending approvals', () => {
    setStore([], 'prompt')
    renderPage()
    expect(screen.getByText(/no pending approvals/i)).toBeInTheDocument()
  })

  it('reflects the active strategy from the store (auto-approve preselected)', () => {
    setStore([], 'auto-approve')
    renderPage()
    expect(screen.getByRole('radio', { name: /auto approve/i })).toBeChecked()
  })

  it('reads config.get for the effective execution mode after waitForConnection', async () => {
    mockRpc.call.mockResolvedValue({ permissions: { default_mode: 'bypass' } })
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('config.get'))
    // Global bypass surfaces in the effective-mode readout.
    await waitFor(() => expect(screen.getByText('Global BYPASS')).toBeInTheDocument())
  })

  it('saves the strategy on change, toasts, and re-polls (approvals.js:291-299)', async () => {
    setStore([], 'prompt')
    renderPage()
    screen.getByRole('radio', { name: /auto approve/i }).click()
    await waitFor(() => expect(saveModeSpy).toHaveBeenCalledWith('auto-approve'))
    await waitFor(() => expect(pollNowSpy).toHaveBeenCalled())
    // approvals.js:297 — auto-approve is the warn-toned outcome.
    expect(toast.warning).toHaveBeenCalled()
  })

  it('uses the info-toned toast for non-auto-approve strategy saves (approvals.js:297)', async () => {
    setStore([], 'prompt')
    renderPage()
    screen.getByRole('radio', { name: /auto deny/i }).click()
    await waitFor(() => expect(saveModeSpy).toHaveBeenCalledWith('auto-deny'))
    expect(toast.success).toHaveBeenCalled()
    expect(toast.warning).not.toHaveBeenCalled()
  })

  it('reverts + error-toasts when saving the strategy fails', async () => {
    saveModeSpy.mockRejectedValueOnce(new Error('nope'))
    setStore([], 'prompt')
    renderPage()
    screen.getByRole('radio', { name: /auto deny/i }).click()
    await waitFor(() => expect(toast.error).toHaveBeenCalled())
    // The failed option is not left selected; prompt (original) stays checked.
    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /ask every time/i })).toBeChecked(),
    )
  })

  it('resolves an approval with "once" when Approve once is clicked', async () => {
    const item: Approval = { id: 'a1', namespace: 'exec', toolName: 't', command: 'ls' }
    setStore([item])
    renderPage()
    screen.getByRole('button', { name: /approve once/i }).click()
    await waitFor(() => expect(resolveSpy).toHaveBeenCalledWith(item, 'once'))
  })

  it('offers "Always allow" only for exec items with a command', () => {
    setStore([{ id: 'a1', namespace: 'plugin', toolName: 'p', args: { path: '/x' } }])
    const { rerender } = renderPage()
    expect(screen.queryByRole('button', { name: /always allow/i })).not.toBeInTheDocument()
    setStore([{ id: 'a2', namespace: 'exec', toolName: 't', command: 'ls' }])
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <ApprovalsPage />
      </QueryClientProvider>,
    )
    expect(screen.getByRole('button', { name: /always allow/i })).toBeInTheDocument()
  })

  it('resolves with "bypass" and "deny" for those actions', async () => {
    const item: Approval = { id: 'a1', namespace: 'exec', toolName: 't', command: 'ls' }
    setStore([item])
    renderPage()
    screen.getByRole('button', { name: /bypass/i }).click()
    await waitFor(() => expect(resolveSpy).toHaveBeenCalledWith(item, 'bypass'))
    screen.getByRole('button', { name: /^deny$/i }).click()
    await waitFor(() => expect(resolveSpy).toHaveBeenCalledWith(item, 'deny'))
  })

  it('renders the detail JSON block when a card has no command', () => {
    setStore([{ id: 'a1', namespace: 'plugin', toolName: 'p', args: { path: '/etc' } }])
    renderPage()
    expect(screen.getByText(/"path": "\/etc"/)).toBeInTheDocument()
  })

  it('refreshes the durable list on Refresh', async () => {
    renderPage()
    pollNowSpy.mockClear()
    screen.getByRole('button', { name: /refresh/i }).click()
    await waitFor(() => expect(pollNowSpy).toHaveBeenCalled())
  })
})
