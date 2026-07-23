import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { McpPage } from './McpPage'
import { ROBINHOOD_MCP_URL } from './logic'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

function makeRpc() {
  return {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn(),
  }
}

let mockRpc = makeRpc()

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

const ROBINHOOD = {
  name: 'robinhood-trading',
  transport: 'streamable_http',
  url: ROBINHOOD_MCP_URL,
  command: null,
  args: [],
  env: {},
  headers: {},
  oauth: true,
  tool_timeout_seconds: 30,
}

const LOCAL = {
  name: 'local-docs',
  transport: 'stdio',
  url: null,
  command: 'uvx',
  args: ['docs-server'],
  env: {},
  headers: {},
  oauth: false,
  tool_timeout_seconds: 30,
}

function wireRpc({ enabled = true }: { enabled?: boolean } = {}) {
  mockRpc.call.mockImplementation((method: string) => {
    switch (method) {
      case 'config.get':
        return Promise.resolve({ mcp: { enabled, servers: [ROBINHOOD, LOCAL] } })
      case 'mcp.status':
        return Promise.resolve({
          enabled,
          servers: [
            {
              name: 'robinhood-trading',
              authenticated: true,
              connected: true,
              tools: ['get_quote', 'place_order'],
            },
            { name: 'local-docs', connected: false, tools: [] },
          ],
        })
      case 'mcp.connect':
        return Promise.resolve({ connected: true, authorizationRequired: false, tools: [] })
      default:
        return Promise.resolve({})
    }
  })
}

function renderPage(path = '/mcp') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <McpPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('McpPage', () => {
  beforeEach(() => {
    mockRpc = makeRpc()
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.error).mockClear()
  })

  it('loads config and live MCP status after the RPC connection is ready', async () => {
    wireRpc()
    renderPage()
    expect(await screen.findByRole('heading', { name: 'MCP Servers' })).toBeInTheDocument()
    expect(mockRpc.waitForConnection).toHaveBeenCalled()
    expect(mockRpc.call).toHaveBeenCalledWith('config.get')
    expect(mockRpc.call).toHaveBeenCalledWith('mcp.status')
  })

  it('renders a compact operational summary, Robinhood integration, and server rows', async () => {
    wireRpc()
    renderPage()
    const summary = await screen.findByLabelText('MCP summary')
    expect(within(summary).getByText('Configured').parentElement).toHaveTextContent('2')
    expect(within(summary).getByText('Connected').parentElement).toHaveTextContent('1')
    expect(within(summary).getByText('Live tools').parentElement).toHaveTextContent('2')
    expect(screen.getByLabelText('Robinhood MCP')).toHaveTextContent('2 live tools')
    expect(screen.getByRole('heading', { name: 'local-docs' })).toBeInTheDocument()
  })

  it('keeps configuration usable when live MCP status is unavailable', async () => {
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'config.get') {
        return Promise.resolve({ mcp: { enabled: true, servers: [ROBINHOOD, LOCAL] } })
      }
      if (method === 'mcp.status') return Promise.reject(new Error('Method not found: mcp.status'))
      return Promise.resolve({})
    })

    renderPage()

    expect(await screen.findByRole('heading', { name: 'MCP Servers' })).toBeInTheDocument()
    expect(screen.getByText('Live MCP status is unavailable.')).toBeInTheDocument()
    expect(screen.queryByText('MCP configuration unavailable')).not.toBeInTheDocument()
    const summary = screen.getByLabelText('MCP summary')
    expect(within(summary).getByText('Configured').parentElement).toHaveTextContent('2')
    expect(within(summary).getByText('Connected').parentElement).toHaveTextContent('—')
    expect(within(summary).getByText('Live tools').parentElement).toHaveTextContent('—')
    expect(screen.getByRole('heading', { name: 'local-docs' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Connect' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument()
  })

  it('renders the disabled empty workspace without inventing configured servers', async () => {
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'config.get') {
        return Promise.resolve({ mcp: { enabled: false, servers: [] } })
      }
      if (method === 'mcp.status') return Promise.resolve({ enabled: false, servers: [] })
      return Promise.resolve({})
    })

    renderPage()

    expect(await screen.findByRole('heading', { name: 'No MCP servers' })).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Enable MCP runtime' })).toHaveAttribute(
      'aria-checked',
      'false',
    )
    expect(screen.getByText('No custom servers configured yet')).toBeInTheDocument()
  })

  it('pauses the runtime, saves config, and disconnects configured servers', async () => {
    wireRpc()
    renderPage()
    const runtime = await screen.findByRole('switch', { name: 'Enable MCP runtime' })
    expect(runtime).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(runtime)
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('config.patch', {
        patches: { 'mcp.enabled': false, 'mcp.servers': [ROBINHOOD, LOCAL] },
      }),
    )
    expect(mockRpc.call).toHaveBeenCalledWith('mcp.disconnect', { name: 'robinhood-trading' })
    expect(mockRpc.call).toHaveBeenCalledWith('mcp.disconnect', { name: 'local-docs' })
  })

  it('adds a remote server and connects it after saving', async () => {
    wireRpc()
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Add server' }))
    const dialog = screen.getByRole('dialog', { name: 'Add server' })
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'research' } })
    fireEvent.change(within(dialog).getByLabelText('Server URL'), {
      target: { value: 'https://research.example/mcp' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save and connect' }))

    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith(
        'config.patch',
        expect.objectContaining({
          patches: expect.objectContaining({
            'mcp.enabled': true,
            'mcp.servers': expect.arrayContaining([
              expect.objectContaining({
                name: 'research',
                url: 'https://research.example/mcp',
              }),
            ]),
          }),
        }),
      ),
    )
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('mcp.connect', { name: 'research' }),
    )
  })

  it('opens Robinhood as an editable, prefilled connection', async () => {
    wireRpc()
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Manage connection' }))
    const dialog = screen.getByRole('dialog', { name: 'Edit server' })
    expect(within(dialog).getByLabelText('Name')).toHaveValue('robinhood-trading')
    expect(within(dialog).getByLabelText('Server URL')).toHaveValue(ROBINHOOD_MCP_URL)
    expect(within(dialog).getByLabelText('Authenticate with OAuth')).toBeChecked()
  })

  it('cleans up the old runtime registration before connecting a renamed server', async () => {
    wireRpc()
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Edit local-docs' }))
    const dialog = screen.getByRole('dialog', { name: 'Edit server' })
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'docs-v2' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save and connect' }))

    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('mcp.oauth.clear', { name: 'local-docs' }),
    )
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('mcp.connect', { name: 'docs-v2' }),
    )

    const cleanupIndex = mockRpc.call.mock.calls.findIndex(
      ([method, params]) =>
        method === 'mcp.oauth.clear' &&
        (params as { name?: string } | undefined)?.name === 'local-docs',
    )
    const patchIndex = mockRpc.call.mock.calls.findIndex(([method]) => method === 'config.patch')
    expect(cleanupIndex).toBeGreaterThanOrEqual(0)
    expect(patchIndex).toBeGreaterThan(cleanupIndex)
  })

  it('disconnects a live server and removes a confirmed server', async () => {
    wireRpc()
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('mcp.disconnect', { name: 'robinhood-trading' }),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Remove local-docs' }))
    const confirm = screen.getByRole('alertdialog', { name: 'Remove MCP server?' })
    fireEvent.click(within(confirm).getByRole('button', { name: 'Remove server' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('mcp.oauth.clear', { name: 'local-docs' }),
    )
    expect(mockRpc.call).toHaveBeenCalledWith(
      'config.patch',
      expect.objectContaining({
        patches: expect.objectContaining({ 'mcp.servers': [ROBINHOOD] }),
      }),
    )
  })

  it('completes a valid OAuth callback through the MCP RPC surface', async () => {
    wireRpc()
    renderPage('/mcp/oauth/callback?code=code-1&state=state-1')
    expect(screen.getByRole('heading', { name: 'Completing authorization' })).toBeInTheDocument()
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('mcp.oauth.complete', {
        code: 'code-1',
        state: 'state-1',
      }),
    )
    expect(toast.success).toHaveBeenCalledWith('MCP authorization complete.')
  })

  it('shows a useful callback error when the provider omits required values', async () => {
    wireRpc()
    renderPage('/mcp/oauth/callback?error=access_denied&error_description=User%20cancelled')
    expect(screen.getByRole('heading', { name: 'Authorization not completed' })).toBeInTheDocument()
    expect(screen.getByText('User cancelled')).toBeInTheDocument()
    expect(mockRpc.call).not.toHaveBeenCalledWith('mcp.oauth.complete', expect.anything())
  })
})
