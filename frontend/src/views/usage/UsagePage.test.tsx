import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { UsagePage } from './UsagePage'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}))

const navigateSpy = vi.fn()
vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router')
  return { ...actual, useNavigate: () => navigateSpy }
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

// Recent (within 7d) sessions so the default 7d range keeps them visible.
const now = Date.now()
const SESSIONS = [
  {
    session: 'agent:main:chat:big',
    updated_at: now - 2 * 86_400_000,
    input_tokens: 800,
    output_tokens: 200,
    cost_usd: 1.5,
    cost_source: 'provider_billed',
    model: 'openai/gpt-4',
  },
  {
    session: 'agent:bot:chat:small',
    updated_at: now - 1 * 86_400_000,
    input_tokens: 100,
    output_tokens: 100,
    cost_usd: 4,
    cost_source: 'agentos_estimate',
    modelBreakdown: [
      {
        model: 'openai/gpt-4',
        inputTokens: 60,
        outputTokens: 40,
        costUsd: 3,
        costSource: 'agentos_estimate',
      },
      {
        model: 'anthropic/claude',
        inputTokens: 40,
        outputTokens: 60,
        costUsd: 1,
        costSource: 'agentos_estimate',
      },
    ],
  },
]

function wire(sessions: unknown[] = SESSIONS, reject = false) {
  mockRpc.call.mockImplementation((method: string) => {
    if (method === 'usage.status') {
      return reject ? Promise.reject(new Error('boom')) : Promise.resolve({ sessions })
    }
    return Promise.resolve({})
  })
}

function renderPage() {
  return render(
    <MemoryRouter>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <UsagePage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('UsagePage', () => {
  beforeEach(() => {
    mockRpc.call.mockReset()
    mockRpc.waitForConnection.mockReset().mockResolvedValue(undefined)
    navigateSpy.mockReset()
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.error).mockClear()
    localStorage.clear()
  })
  afterEach(() => {
    localStorage.clear()
  })

  it('calls usage.status after waitForConnection', async () => {
    wire()
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('usage.status'))
    expect(mockRpc.waitForConnection).toHaveBeenCalled()
  })

  it('shows a structural loading state instead of presenting zeroes as loaded data', () => {
    mockRpc.call.mockImplementation((method: string) =>
      method === 'usage.status' ? new Promise(() => undefined) : Promise.resolve({}),
    )
    renderPage()
    expect(screen.getByRole('status', { name: 'Loading usage data' })).toBeInTheDocument()
    expect(screen.queryByText(/No usage data yet/i)).not.toBeInTheDocument()
  })

  it('renders the metric tiles from the payload', async () => {
    wire()
    renderPage()
    // total tokens = (800+200)+(100+100) = 1200
    await waitFor(() => expect(screen.getByLabelText('Total tokens')).toHaveTextContent('1,200'))
    // total cost = 1.5 + 4 = 5.5
    expect(screen.getByLabelText('Total cost')).toHaveTextContent('$5.5000')
    expect(screen.getByLabelText('Sessions')).toHaveTextContent('2')
    // avg = 5.5 / 2 = 2.75
    expect(screen.getByLabelText('Avg cost / session')).toHaveTextContent('$2.7500')
  })

  it('renders a table row per visible session with cost + source badge', async () => {
    wire()
    renderPage()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'agent:main:chat:big' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: 'agent:bot:chat:small' })).toBeInTheDocument()
    expect(screen.getAllByText('Actual').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Estimated').length).toBeGreaterThan(0)
  })

  it('a table session link navigates to /chat?session=<key> (encoded)', async () => {
    wire()
    renderPage()
    const link = await screen.findByRole('button', { name: 'agent:main:chat:big' })
    fireEvent.click(link)
    expect(navigateSpy).toHaveBeenCalledWith('/chat?session=agent%3Amain%3Achat%3Abig')
  })

  it('renders chart bars and a bar click navigates to chat', async () => {
    wire()
    renderPage()
    // The chart bar's title is "Open <key>"; the table link's name is the key.
    const bar = await screen.findByTitle('Open agent:main:chat:big')
    fireEvent.click(bar)
    expect(navigateSpy).toHaveBeenCalledWith('/chat?session=agent%3Amain%3Achat%3Abig')
  })

  it('renders the by-model breakdown grid', async () => {
    wire()
    renderPage()
    // gpt-4 appears across both sessions; claude only in the breakdown
    await waitFor(() => expect(screen.getByLabelText('By model breakdown')).toBeInTheDocument())
    const grid = screen.getByLabelText('By model breakdown')
    expect(within(grid).getAllByText('gpt-4').length).toBeGreaterThan(0)
    expect(within(grid).getByText('claude')).toBeInTheDocument()
  })

  it('expands a multi-model session row to show the per-model breakdown', async () => {
    wire()
    renderPage()
    const toggle = await screen.findByRole('button', { name: /auto · 2 models/i })
    fireEvent.click(toggle)
    // the expanded region is labelled "Model breakdown" and lists both models
    await waitFor(() => expect(screen.getByText(/Model breakdown/i)).toBeInTheDocument())
    const region = screen.getByRole('table', { name: 'Model breakdown' })
    expect(within(region).getByText('gpt-4')).toBeInTheDocument()
    expect(within(region).getByText('claude')).toBeInTheDocument()
  })

  it('switches the chart to cost mode', async () => {
    wire()
    renderPage()
    await screen.findByTitle('Open agent:main:chat:big')
    const metric = screen.getByRole('group', { name: 'Chart metric' })
    fireEvent.click(within(metric).getByRole('button', { name: /^Cost$/ }))
    await waitFor(() => expect(screen.getByText(/Top sessions by cost/i)).toBeInTheDocument())
  })

  it('persists the range selection to localStorage and re-filters', async () => {
    wire()
    renderPage()
    await screen.findByRole('button', { name: 'agent:main:chat:big' })
    fireEvent.click(screen.getByRole('button', { name: '30d' }))
    await waitFor(() => expect(localStorage.getItem('agentos-usage-range')).toBe('30'))
  })

  it('reads the persisted range on mount', async () => {
    localStorage.setItem('agentos-usage-range', 'all')
    wire()
    renderPage()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'true'),
    )
  })

  it('sorts the table when a sortable header is clicked', async () => {
    wire()
    renderPage()
    await screen.findByRole('button', { name: 'agent:main:chat:big' })
    const tableLinks = () =>
      within(screen.getByRole('table'))
        .getAllByRole('button')
        .map((b) => b.textContent)
        .filter((t) => t?.startsWith('agent:'))
    // default sort = updated_at desc → most recent (small, 1d ago) first.
    expect(tableLinks()[0]).toBe('agent:bot:chat:small')
    // click Input header → new column defaults to DESC (legacy _sortAsc=false):
    // big(800) > small(100) → big first.
    fireEvent.click(screen.getByRole('button', { name: /^Input/ }))
    await waitFor(() => expect(tableLinks()[0]).toBe('agent:main:chat:big'))
    // toggle same column → ASC: small first.
    fireEvent.click(screen.getByRole('button', { name: /^Input/ }))
    await waitFor(() => expect(tableLinks()[0]).toBe('agent:bot:chat:small'))
  })

  it('shows the empty table + chart states when there is no usage', async () => {
    wire([])
    renderPage()
    await waitFor(() => expect(screen.getByText(/No usage data yet/i)).toBeInTheDocument())
    expect(screen.getByText(/No data in the selected window/i)).toBeInTheDocument()
  })

  it('surfaces the undated-legacy hint when a dated range hides undated rows', async () => {
    wire([{ session: 'agent:x:chat:u', input_tokens: 5, output_tokens: 5, cost_usd: 1 }])
    renderPage()
    await waitFor(() =>
      expect(screen.getAllByText(/undated legacy session/i).length).toBeGreaterThan(0),
    )
  })

  it('refreshes usage on the Refresh button', async () => {
    wire()
    renderPage()
    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter(([m]) => m === 'usage.status').length).toBe(1),
    )
    fireEvent.click(screen.getByRole('button', { name: /^Refresh$/i }))
    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter(([m]) => m === 'usage.status').length).toBe(2),
    )
  })

  it('toasts when usage.status fails', async () => {
    wire(SESSIONS, true)
    renderPage()
    await waitFor(() => expect(toast.error).toHaveBeenCalled())
    expect(screen.getByRole('alert')).toHaveTextContent('Usage data is unavailable')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('sets the document title', async () => {
    wire()
    renderPage()
    await waitFor(() => expect(document.title).toBe('Usage - AgentOS Control'))
  })
})
