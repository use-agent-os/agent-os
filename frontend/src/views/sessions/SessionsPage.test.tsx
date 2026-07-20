import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { SessionsPage } from './SessionsPage'

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

const SESSIONS = [
  {
    key: 'agent:main:chat:aaa',
    status: 'running',
    model: 'gpt-x',
    message_count: 12,
    updated_at: '3000',
  },
  {
    key: 'agent:bot:chat:bbb',
    status: 'done',
    model: 'claude',
    display_name: 'Bug triage',
    message_count: 4,
    updated_at: '1000',
  },
  {
    key: 'agent:ghost:chat:ccc',
    status: 'done',
    last_task: { status: 'failed' },
    message_count: 1,
    updated_at: '2000',
  },
]
const AGENTS = [
  { id: 'main', name: 'Main' },
  { id: 'bot', name: 'Support Bot' },
]

function wireRpc(
  opts: {
    sessions?: unknown[]
    agents?: unknown[]
    sessionsReject?: boolean
    agentsReject?: boolean
    createReject?: { code?: string; message?: string }
    agentCreateReject?: { code?: string; message?: string }
    deleteResult?: unknown
    deleteReject?: boolean
    createKey?: string
  } = {},
) {
  mockRpc.call.mockImplementation((method: string) => {
    switch (method) {
      case 'sessions.list':
        return opts.sessionsReject
          ? Promise.reject(new Error('sessions down'))
          : Promise.resolve({ sessions: opts.sessions ?? SESSIONS })
      case 'agents.list':
        return opts.agentsReject
          ? Promise.reject(new Error('agents down'))
          : Promise.resolve({ agents: opts.agents ?? AGENTS })
      case 'agents.create': {
        if (opts.agentCreateReject) {
          const e = new Error(opts.agentCreateReject.message ?? 'agent create failed') as Error & {
            code?: string
          }
          e.code = opts.agentCreateReject.code
          return Promise.reject(e)
        }
        return Promise.resolve({})
      }
      case 'sessions.create': {
        if (opts.createReject) {
          const e = new Error(opts.createReject.message ?? 'create failed') as Error & {
            code?: string
          }
          e.code = opts.createReject.code
          return Promise.reject(e)
        }
        return Promise.resolve({ key: opts.createKey ?? 'agent:main:chat:new' })
      }
      case 'sessions.delete':
        if (opts.deleteReject) return Promise.reject(new Error('delete failed'))
        return Promise.resolve(opts.deleteResult ?? { deleted: [], errors: [] })
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
        <SessionsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const callsFor = (m: string) => mockRpc.call.mock.calls.filter(([x]) => x === m).length

describe('SessionsPage', () => {
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

  it('loads sessions {limit:200} and agents.list after waitForConnection', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('sessions.list', { limit: 200 }))
    expect(mockRpc.call).toHaveBeenCalledWith('agents.list', {})
    expect(mockRpc.waitForConnection).toHaveBeenCalled()
  })

  it('renders the stat row from the sessions payload', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Total sessions')).toHaveTextContent('3'))
    // messages: 12 + 4 + 1 = 17
    expect(screen.getByLabelText('Messages')).toHaveTextContent('17')
  })

  it('renders a row per session with key link and status', async () => {
    wireRpc()
    renderPage()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'agent:main:chat:aaa' })).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: 'agent:bot:chat:bbb' })).toBeInTheDocument()
    // failed run-status badge for the ghost session
    expect(screen.getByText(/Last task failed/i)).toBeInTheDocument()
  })

  it('marks an orphaned agent once the registry loads', async () => {
    wireRpc()
    renderPage()
    // ghost agent is not in AGENTS → orphan chip after agents.list resolves
    await waitFor(() => expect(screen.getByText(/Orphaned/i)).toBeInTheDocument())
  })

  it('shows the empty state with a create CTA when there are no sessions', async () => {
    wireRpc({ sessions: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No sessions yet/i)).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /start a new session/i })).toBeInTheDocument()
  })

  it('toasts when sessions.list fails', async () => {
    wireRpc({ sessionsReject: true })
    renderPage()
    await waitFor(() => expect(toast.error).toHaveBeenCalled())
  })

  it('the key link navigates to /chat?session=<key> (encoded)', async () => {
    wireRpc()
    renderPage()
    const link = await screen.findByRole('button', { name: 'agent:main:chat:aaa' })
    fireEvent.click(link)
    expect(navigateSpy).toHaveBeenCalledWith('/chat?session=agent%3Amain%3Achat%3Aaaa')
  })

  it('copies a session key to the clipboard and toasts', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    wireRpc()
    renderPage()
    await screen.findByRole('button', { name: 'agent:main:chat:aaa' })
    fireEvent.click(screen.getByRole('button', { name: 'Copy session key agent:main:chat:aaa' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('agent:main:chat:aaa'))
    await waitFor(() => expect(toast.success).toHaveBeenCalled())
  })

  it('filters the table via the search box (matches display_name)', async () => {
    wireRpc()
    renderPage()
    await screen.findByRole('button', { name: 'agent:main:chat:aaa' })
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: 'bug' } })
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'agent:main:chat:aaa' })).not.toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: 'agent:bot:chat:bbb' })).toBeInTheDocument()
  })

  it('shows the "No matches" empty state when a search excludes everything', async () => {
    wireRpc()
    renderPage()
    await screen.findByRole('button', { name: 'agent:main:chat:aaa' })
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: 'zzzzz' } })
    await waitFor(() => expect(screen.getByText(/No matches/i)).toBeInTheDocument())
  })

  it('deleting one session confirms then calls sessions.delete {key} and reloads', async () => {
    wireRpc({ deleteResult: { deleted: ['agent:main:chat:aaa'], errors: [] } })
    renderPage()
    await screen.findByRole('button', { name: 'agent:main:chat:aaa' })
    fireEvent.click(screen.getByRole('button', { name: 'Delete session agent:main:chat:aaa' }))
    const confirm = await screen.findByRole('alertdialog')
    expect(mockRpc.call).not.toHaveBeenCalledWith('sessions.delete', expect.anything())
    fireEvent.click(within(confirm).getByRole('button', { name: /^delete$/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('sessions.delete', {
        key: 'agent:main:chat:aaa',
      }),
    )
    await waitFor(() => expect(callsFor('sessions.list')).toBeGreaterThanOrEqual(2))
  })

  it('cancelling the delete confirmation does not call sessions.delete', async () => {
    wireRpc()
    renderPage()
    await screen.findByRole('button', { name: 'agent:main:chat:aaa' })
    fireEvent.click(screen.getByRole('button', { name: 'Delete session agent:main:chat:aaa' }))
    const confirm = await screen.findByRole('alertdialog')
    fireEvent.click(within(confirm).getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(mockRpc.call).not.toHaveBeenCalledWith('sessions.delete', expect.anything())
  })

  it('bulk-selecting rows and deleting calls sessions.delete {keys:[…]}', async () => {
    wireRpc({
      deleteResult: { deleted: ['agent:main:chat:aaa', 'agent:bot:chat:bbb'], errors: [] },
    })
    renderPage()
    await screen.findByRole('button', { name: 'agent:main:chat:aaa' })
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select session agent:main:chat:aaa' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select session agent:bot:chat:bbb' }))
    fireEvent.click(screen.getByRole('button', { name: /delete selected/i }))
    const confirm = await screen.findByRole('alertdialog')
    fireEvent.click(within(confirm).getByRole('button', { name: /delete all/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('sessions.delete', expect.anything()),
    )
    const call = mockRpc.call.mock.calls.find(([m]) => m === 'sessions.delete')
    expect(call?.[1]).toHaveProperty('keys')
    expect((call?.[1] as { keys: string[] }).keys.sort()).toEqual([
      'agent:bot:chat:bbb',
      'agent:main:chat:aaa',
    ])
  })

  it('creating a session with an existing agent calls sessions.create and navigates', async () => {
    wireRpc({ createKey: 'agent:bot:chat:zzz' })
    renderPage()
    await screen.findByRole('button', { name: 'agent:main:chat:aaa' })
    fireEvent.click(screen.getByRole('button', { name: /new session/i }))
    const dialog = await screen.findByRole('dialog')
    // pick an existing agent by typing its exact id
    fireEvent.change(within(dialog).getByLabelText(/agent/i), { target: { value: 'bot' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /start chat/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('sessions.create', { agentId: 'bot' }),
    )
    // no inline agent create for an existing agent
    expect(mockRpc.call).not.toHaveBeenCalledWith('agents.create', expect.anything())
    await waitFor(() =>
      expect(navigateSpy).toHaveBeenCalledWith('/chat?session=agent%3Abot%3Achat%3Azzz'),
    )
  })

  it('creating a session with a new agent id creates the agent first, then the session', async () => {
    wireRpc({ createKey: 'agent:fresh:chat:zzz' })
    renderPage()
    await screen.findByRole('button', { name: 'agent:main:chat:aaa' })
    fireEvent.click(screen.getByRole('button', { name: /new session/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/agent/i), { target: { value: 'fresh' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /start chat/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('agents.create', { id: 'fresh', name: 'fresh' }),
    )
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('sessions.create', { agentId: 'fresh' }),
    )
  })

  it('surfaces an inline error and keeps the create dialog open when sessions.create fails', async () => {
    wireRpc({ createReject: { code: 'agent.not_found', message: 'nope' } })
    renderPage()
    await screen.findByRole('button', { name: 'agent:main:chat:aaa' })
    fireEvent.click(screen.getByRole('button', { name: /new session/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/agent/i), { target: { value: 'bot' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /start chat/i }))
    await waitFor(() => expect(within(dialog).getByText(/doesn't exist/i)).toBeInTheDocument())
    // dialog stays open
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('refreshes sessions on the Refresh button', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(callsFor('sessions.list')).toBe(1))
    fireEvent.click(screen.getByRole('button', { name: /^refresh$/i }))
    await waitFor(() => expect(callsFor('sessions.list')).toBe(2))
  })

  it('sets the document title', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(document.title).toBe('Sessions - AgentOS Control'))
  })
})
