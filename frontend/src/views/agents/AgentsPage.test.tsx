import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { AgentsPage } from './AgentsPage'

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

type Handler = (...args: unknown[]) => void
function makeRpc() {
  const listeners = new Map<string, Set<Handler>>()
  return {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn(),
    on: vi.fn((event: string, handler: Handler) => {
      if (!listeners.has(event)) listeners.set(event, new Set())
      listeners.get(event)!.add(handler)
      return () => listeners.get(event)?.delete(handler)
    }),
    emit(event: string, ...args: unknown[]) {
      listeners.get(event)?.forEach((h) => h(...args))
    },
    listenerCount(event: string) {
      return listeners.get(event)?.size ?? 0
    },
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

const BUILTIN = {
  id: 'main',
  name: 'Main',
  type: 'builtin',
  model: 'gpt-x',
  tools: ['read', 'write'],
}
const CUSTOM = {
  id: 'data-analyst',
  name: 'Data Analyst',
  type: 'custom',
  description: 'Crunches numbers',
  model: 'claude-x',
  tools: ['read', 'sql', 'plot'],
  skills: ['charts'],
  workspace: '/ws',
}

function wireRpc(
  opts: {
    agents?: unknown[]
    listReject?: boolean
    createReject?: { code?: string; message?: string }
    updateReject?: { code?: string; message?: string }
    deleteReject?: boolean
  } = {},
) {
  mockRpc.call.mockImplementation((method: string) => {
    switch (method) {
      case 'agents.list':
        return opts.listReject
          ? Promise.reject(new Error('list down'))
          : Promise.resolve({ agents: opts.agents ?? [BUILTIN, CUSTOM] })
      case 'agents.create': {
        if (opts.createReject) {
          const e = new Error(opts.createReject.message ?? 'create failed') as Error & {
            code?: string
          }
          e.code = opts.createReject.code
          return Promise.reject(e)
        }
        return Promise.resolve({})
      }
      case 'agents.update': {
        if (opts.updateReject) {
          const e = new Error(opts.updateReject.message ?? 'update failed') as Error & {
            code?: string
          }
          e.code = opts.updateReject.code
          return Promise.reject(e)
        }
        return Promise.resolve({})
      }
      case 'agents.delete':
        return opts.deleteReject ? Promise.reject(new Error('delete failed')) : Promise.resolve({})
      default:
        return Promise.resolve({})
    }
  })
}

function renderPage() {
  return render(
    <MemoryRouter>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <AgentsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const listCalls = () => mockRpc.call.mock.calls.filter(([m]) => m === 'agents.list').length

describe('AgentsPage', () => {
  beforeEach(() => {
    mockRpc = makeRpc()
    navigateSpy.mockReset()
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.warning).mockClear()
    vi.mocked(toast.error).mockClear()
    vi.mocked(toast.info).mockClear()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('calls agents.list after waitForConnection', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('agents.list', {}))
    expect(mockRpc.waitForConnection).toHaveBeenCalled()
  })

  it('renders the stat row from the agents payload', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Total agents')).toHaveTextContent('2'))
    // distinct models: gpt-x, claude-x → 2
    expect(screen.getByLabelText('Models in use')).toHaveTextContent('2')
    // tools wired: 2 + 3 = 5
    expect(screen.getByLabelText('Tools wired')).toHaveTextContent('5')
  })

  it('renders a card per agent with type chip and tool chips', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Agent main')).toBeInTheDocument())
    const custom = screen.getByLabelText('Agent data-analyst')
    expect(within(custom).getByText('custom')).toBeInTheDocument()
    expect(within(custom).getByText('Crunches numbers')).toBeInTheDocument()
    expect(within(custom).getByText('sql')).toBeInTheDocument()
  })

  it('builtin cards expose Customize (no Delete); custom cards expose Edit + Delete', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Agent main')).toBeInTheDocument())
    const builtin = screen.getByLabelText('Agent main')
    expect(within(builtin).getByRole('button', { name: /customize/i })).toBeInTheDocument()
    expect(within(builtin).queryByRole('button', { name: /^delete$/i })).not.toBeInTheDocument()
    const custom = screen.getByLabelText('Agent data-analyst')
    expect(within(custom).getByRole('button', { name: /^edit$/i })).toBeInTheDocument()
    expect(within(custom).getByRole('button', { name: /^delete$/i })).toBeInTheDocument()
  })

  it('shows the empty state when no agents are configured', async () => {
    wireRpc({ agents: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No agents configured/i)).toBeInTheDocument())
  })

  it('toasts when agents.list fails', async () => {
    wireRpc({ listReject: true })
    renderPage()
    await waitFor(() => expect(toast.error).toHaveBeenCalled())
  })

  it('opening New agent, filling id, and submitting calls agents.create and invalidates', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(listCalls()).toBe(1))
    fireEvent.click(screen.getByRole('button', { name: /new agent/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/Agent ID/i), {
      target: { value: 'data-2' },
    })
    fireEvent.change(within(dialog).getByLabelText(/Display name/i), {
      target: { value: 'Data Two' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /create agent/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('agents.create', {
        id: 'data-2',
        name: 'Data Two',
      }),
    )
    // success → refetch list (a second agents.list)
    await waitFor(() => expect(listCalls()).toBeGreaterThanOrEqual(2))
  })

  it('blocks create submit and shows a validation error when the id is blank', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(listCalls()).toBe(1))
    fireEvent.click(screen.getByRole('button', { name: /new agent/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /create agent/i }))
    expect(await within(dialog).findByText(/Agent ID is required/i)).toBeInTheDocument()
    expect(mockRpc.call).not.toHaveBeenCalledWith('agents.create', expect.anything())
  })

  it('warns (not errors) when agents.create reports agent.exists', async () => {
    wireRpc({ createReject: { code: 'agent.exists' } })
    renderPage()
    await waitFor(() => expect(listCalls()).toBe(1))
    fireEvent.click(screen.getByRole('button', { name: /new agent/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/Agent ID/i), { target: { value: 'main' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /create agent/i }))
    await waitFor(() => expect(toast.warning).toHaveBeenCalled())
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('editing a custom agent sends only changed fields to agents.update and invalidates', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Agent data-analyst')).toBeInTheDocument())
    const custom = screen.getByLabelText('Agent data-analyst')
    fireEvent.click(within(custom).getByRole('button', { name: /^edit$/i }))
    const dialog = await screen.findByRole('dialog')
    // id field is disabled (never editable post-create)
    expect(within(dialog).getByLabelText(/Agent ID/i)).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText(/Display name/i), {
      target: { value: 'Data Analyst 2' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /save changes/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('agents.update', {
        id: 'data-analyst',
        name: 'Data Analyst 2',
      }),
    )
    await waitFor(() => expect(listCalls()).toBeGreaterThanOrEqual(2))
  })

  it('surfaces a friendly message when agents.update hits builtin_immutable', async () => {
    wireRpc({ updateReject: { code: 'agent.builtin_immutable' } })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Agent data-analyst')).toBeInTheDocument())
    const custom = screen.getByLabelText('Agent data-analyst')
    fireEvent.click(within(custom).getByRole('button', { name: /^edit$/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/Display name/i), { target: { value: 'Zed' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(toast.error).toHaveBeenCalled())
  })

  // agents.js:432-437 — no-op-save short-circuit: an unchanged form must NOT
  // call agents.update; it toasts 'Nothing to save' and keeps the dialog open.
  it('saving an unchanged edit does not call agents.update and toasts "Nothing to save"', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Agent data-analyst')).toBeInTheDocument())
    const custom = screen.getByLabelText('Agent data-analyst')
    fireEvent.click(within(custom).getByRole('button', { name: /^edit$/i }))
    const dialog = await screen.findByRole('dialog')
    // Change nothing, click Save.
    fireEvent.click(within(dialog).getByRole('button', { name: /save changes/i }))
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith('Nothing to save', expect.anything()),
    )
    expect(mockRpc.call).not.toHaveBeenCalledWith('agents.update', expect.anything())
    // Dialog stays open.
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  // agents.js:307-312,499-506 — dirty-guard on close.
  it('closing a dirty edit via Escape shows the discard confirm; dismissing keeps edits', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Agent data-analyst')).toBeInTheDocument())
    const custom = screen.getByLabelText('Agent data-analyst')
    fireEvent.click(within(custom).getByRole('button', { name: /^edit$/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/Display name/i), {
      target: { value: 'Edited name' },
    })
    fireEvent.keyDown(dialog, { key: 'Escape' })
    // Discard confirm appears; the edit dialog is NOT yet gone.
    const confirm = await screen.findByRole('alertdialog')
    expect(within(confirm).getByText(/Discard unsaved changes/i)).toBeInTheDocument()
    // Dismiss the confirm — the edit dialog stays, edits intact.
    fireEvent.click(within(confirm).getByRole('button', { name: /keep editing|cancel/i }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(within(screen.getByRole('dialog')).getByLabelText(/Display name/i)).toHaveValue(
      'Edited name',
    )
  })

  it('confirming discard on a dirty edit closes the dialog', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Agent data-analyst')).toBeInTheDocument())
    const custom = screen.getByLabelText('Agent data-analyst')
    fireEvent.click(within(custom).getByRole('button', { name: /^edit$/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/Display name/i), {
      target: { value: 'Edited name' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /^cancel$/i }))
    const confirm = await screen.findByRole('alertdialog')
    fireEvent.click(within(confirm).getByRole('button', { name: /^discard$/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('closing a non-dirty edit via Escape closes immediately with no discard prompt', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Agent data-analyst')).toBeInTheDocument())
    const custom = screen.getByLabelText('Agent data-analyst')
    fireEvent.click(within(custom).getByRole('button', { name: /^edit$/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('deleting requires confirmation then calls agents.delete and invalidates', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Agent data-analyst')).toBeInTheDocument())
    const custom = screen.getByLabelText('Agent data-analyst')
    fireEvent.click(within(custom).getByRole('button', { name: /^delete$/i }))
    // A confirmation dialog appears; delete not yet called.
    const confirm = await screen.findByRole('alertdialog')
    expect(mockRpc.call).not.toHaveBeenCalledWith('agents.delete', expect.anything())
    fireEvent.click(within(confirm).getByRole('button', { name: /^delete agent$/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('agents.delete', { id: 'data-analyst' }),
    )
    await waitFor(() => expect(listCalls()).toBeGreaterThanOrEqual(2))
  })

  it('cancelling the delete confirmation does not call agents.delete', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Agent data-analyst')).toBeInTheDocument())
    const custom = screen.getByLabelText('Agent data-analyst')
    fireEvent.click(within(custom).getByRole('button', { name: /^delete$/i }))
    const confirm = await screen.findByRole('alertdialog')
    fireEvent.click(within(confirm).getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(mockRpc.call).not.toHaveBeenCalledWith('agents.delete', expect.anything())
  })

  it('Customize on a builtin opens the create dialog pre-seeded with <id>-copy', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Agent main')).toBeInTheDocument())
    const builtin = screen.getByLabelText('Agent main')
    fireEvent.click(within(builtin).getByRole('button', { name: /customize/i }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByLabelText(/Agent ID/i)).toHaveValue('main-copy')
  })

  it('Chat button navigates to /chat with the agent query', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Agent data-analyst')).toBeInTheDocument())
    const custom = screen.getByLabelText('Agent data-analyst')
    fireEvent.click(within(custom).getByRole('button', { name: /chat/i }))
    expect(navigateSpy).toHaveBeenCalledWith('/chat?agent=data-analyst')
  })

  it('refreshes on the Refresh button', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(listCalls()).toBe(1))
    fireEvent.click(screen.getByRole('button', { name: /^refresh$/i }))
    await waitFor(() => expect(listCalls()).toBe(2))
  })

  it('sets the document title', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(document.title).toBe('Agents - AgentOS Control'))
  })
})
