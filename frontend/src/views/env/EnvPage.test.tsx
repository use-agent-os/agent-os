import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { EnvPage } from './EnvPage'
import type { EnvListResponse, EnvVarRow } from './logic'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

const mockRpc = { call: vi.fn(), waitForConnection: vi.fn().mockResolvedValue(undefined) }

vi.mock('@/app/providers', () => ({
  useRpc: () => mockRpc,
  useBootstrap: () => ({ base_path: '/control', features: {} }),
}))

const SECRET = 'sk-live-supersecret-value'

function row(partial: Partial<EnvVarRow> & { name: string }): EnvVarRow {
  return {
    isSet: false,
    source: 'unset',
    masked: null,
    secret: true,
    description: '',
    url: '',
    category: 'custom',
    owner: '',
    required: false,
    writable: true,
    restartRequired: false,
    missing: false,
    ...partial,
  }
}

const PAYLOAD: EnvListResponse = {
  envFilePath: '~/.agentos/.env',
  setCount: 1,
  totalCount: 4,
  shadowedCount: 0,
  vars: [
    row({
      name: 'OPENAI_API_KEY',
      isSet: true,
      source: 'home_file',
      masked: 'sk-l…alue',
      category: 'provider',
      owner: 'openai',
      description: 'API key for OpenAI (LLM provider).',
      restartRequired: true,
    }),
    row({
      name: 'BASE_RPC_URL',
      category: 'skill',
      owner: 'onchain',
      secret: false,
      description: 'Base L2 RPC endpoint',
      url: 'https://docs.example.invalid/',
      required: true,
      missing: true,
    }),
    row({ name: 'PATH', isSet: true, source: 'process', masked: '/usr/bin', writable: false }),
    row({ name: 'MY_OWN', category: 'custom' }),
  ],
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <EnvPage />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockRpc.call.mockImplementation((method: string) => {
    if (method === 'env.list') return Promise.resolve(PAYLOAD)
    return Promise.resolve({})
  })
})

describe('EnvPage', () => {
  it('groups variables and names who needs each one', async () => {
    renderPage()
    expect(await screen.findByText('LLM providers')).toBeTruthy()
    expect(screen.getByText('Skills')).toBeTruthy()
    expect(screen.getByText(/Needed by onchain/)).toBeTruthy()
  })

  it('shows masked values, never the real one', async () => {
    const { container } = renderPage()
    await screen.findByText('OPENAI_API_KEY')
    expect(container.textContent).toContain('sk-l…alue')
    expect(container.textContent).not.toContain(SECRET)
  })

  it('locks variables the server refuses to write and offers no edit control', async () => {
    renderPage()
    await screen.findByText('PATH')
    // An operator who cannot see why the row is inert will file a bug; the
    // lock plus its title is the explanation.
    expect(screen.getByLabelText('Not writable through AgentOS')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Set PATH/ })).toBeNull()
  })

  it('warns when a value is shadowed by the process environment', async () => {
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'env.list') return Promise.resolve({ ...PAYLOAD, shadowedCount: 1 })
      return Promise.resolve({})
    })
    renderPage()
    expect(
      await screen.findByText(
        /Editing them here will not take effect until the export is removed/i,
      ),
    ).toBeTruthy()
  })

  it('links to where a credential can be obtained', async () => {
    renderPage()
    await screen.findByText('BASE_RPC_URL')
    const link = screen.getByRole('link', { name: /where to get this/i })
    expect(link.getAttribute('href')).toBe('https://docs.example.invalid/')
  })

  it('saves a value through env.set', async () => {
    renderPage()
    await screen.findByText('BASE_RPC_URL')

    fireEvent.click(screen.getByRole('button', { name: 'Set BASE_RPC_URL' }))
    fireEvent.change(screen.getByLabelText('Value for BASE_RPC_URL'), {
      target: { value: 'https://rpc.example.invalid' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Save$/ }))

    await waitFor(() => {
      expect(mockRpc.call).toHaveBeenCalledWith('env.set', {
        name: 'BASE_RPC_URL',
        value: 'https://rpc.example.invalid',
      })
    })
  })

  it('filters to the variables that still need attention', async () => {
    renderPage()
    await screen.findByText('OPENAI_API_KEY')
    fireEvent.click(screen.getByRole('button', { name: 'Missing' }))
    await waitFor(() => expect(screen.queryByText('OPENAI_API_KEY')).toBeNull())
    expect(screen.getByText('BASE_RPC_URL')).toBeTruthy()
  })

  it('rejects an invalid new name before calling the server', async () => {
    renderPage()
    await screen.findByText('OPENAI_API_KEY')
    fireEvent.click(screen.getByRole('button', { name: /Add variable/ }))
    fireEvent.change(screen.getByLabelText('New variable name'), { target: { value: '1BAD' } })
    fireEvent.click(screen.getByRole('button', { name: /^Save$/ }))

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(mockRpc.call).not.toHaveBeenCalledWith('env.set', expect.anything())
  })

  it('asks in-app before revealing, and cancelling calls nothing', async () => {
    // An in-app dialog rather than window.confirm: a native one cannot be
    // themed, blocks the page, and is unreachable to the same tests and
    // assistive tech as the rest of the surface.
    renderPage()
    await screen.findByText('OPENAI_API_KEY')

    fireEvent.click(screen.getByRole('button', { name: /Reveal OPENAI_API_KEY/ }))
    expect(await screen.findByRole('alertdialog')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /^Cancel$/ }))

    expect(mockRpc.call).not.toHaveBeenCalledWith('env.reveal', expect.anything())
  })

  it('reveals only after the operator agrees', async () => {
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'env.list') return Promise.resolve(PAYLOAD)
      if (method === 'env.reveal') return Promise.resolve({ value: SECRET })
      return Promise.resolve({})
    })
    renderPage()
    await screen.findByText('OPENAI_API_KEY')

    fireEvent.click(screen.getByRole('button', { name: /Reveal OPENAI_API_KEY/ }))
    fireEvent.click(await screen.findByRole('button', { name: /Show value/ }))
    expect(await screen.findByText(SECRET)).toBeTruthy()
  })

  it('asks in-app before removing a variable', async () => {
    renderPage()
    await screen.findByText('OPENAI_API_KEY')

    fireEvent.click(screen.getByRole('button', { name: /Remove OPENAI_API_KEY/ }))
    fireEvent.click(await screen.findByRole('button', { name: /^Remove$/ }))

    await waitFor(() => {
      expect(mockRpc.call).toHaveBeenCalledWith('env.unset', { name: 'OPENAI_API_KEY' })
    })
  })

  it('surfaces a load failure with a retry instead of a blank page', async () => {
    mockRpc.call.mockRejectedValue(new Error('gateway is down'))
    renderPage()
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('gateway is down')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Retry/ })).toBeTruthy()
  })

  it('folds the rows that need nothing, and shows them on demand', async () => {
    // 22 unset provider rows would otherwise bury the two that need attention.
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'env.list')
        return Promise.resolve({
          ...PAYLOAD,
          vars: [
            row({ name: 'IDLE_ONE', category: 'search' }),
            row({ name: 'IDLE_TWO', category: 'search' }),
            row({ name: 'NEEDED_NOW', category: 'search', required: true, missing: true }),
          ],
        })
      return Promise.resolve({})
    })
    renderPage()

    // What needs attention is visible immediately...
    expect(await screen.findByText('NEEDED_NOW')).toBeTruthy()
    // ...the quiet tail is folded, but its size is stated.
    expect(screen.queryByText('IDLE_ONE')).toBeNull()

    const more = screen.getByRole('button', { name: /Show 2 unset/ })
    fireEvent.click(more)
    expect(screen.getByText('IDLE_ONE')).toBeTruthy()
  })

  it('still states the full group counts while the tail is folded', async () => {
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'env.list')
        return Promise.resolve({
          ...PAYLOAD,
          vars: [
            row({ name: 'SET_ONE', category: 'search', isSet: true }),
            row({ name: 'IDLE_ONE', category: 'search' }),
          ],
        })
      return Promise.resolve({})
    })
    renderPage()
    await screen.findByText('SET_ONE')
    expect(screen.getByText('1/2 set')).toBeTruthy()
  })

  it('states counts outside the title block', async () => {
    renderPage()
    await screen.findByText('OPENAI_API_KEY')
    // The heading says what the page is; the strip says where it stands.
    // Scoped to the <dl> because "Set" is also a filter and a row action.
    const terms = screen.getAllByRole('term').map((el) => el.textContent)
    expect(terms).toEqual(['Set', 'Missing', 'Shadowed'])
  })

  it('does not repeat the variable name in every button label', async () => {
    renderPage()
    await screen.findByText('BASE_RPC_URL')
    // Visible label is short; the accessible name still carries the variable.
    const action = screen.getByRole('button', { name: 'Set BASE_RPC_URL' })
    expect(action.textContent).toBe('Set')
  })

  it('opens Add variable as a dialog', async () => {
    renderPage()
    await screen.findByText('OPENAI_API_KEY')
    expect(screen.queryByRole('dialog')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Add variable/ }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toContain('Add a variable')
  })

  it('keeps the dialog open and shows what the server said when a write is refused', async () => {
    // Closing on failure would discard the name and value the operator typed.
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'env.list') return Promise.resolve(PAYLOAD)
      return Promise.reject(new Error('cannot be written through AgentOS'))
    })
    renderPage()
    await screen.findByText('OPENAI_API_KEY')

    fireEvent.click(screen.getByRole('button', { name: /Add variable/ }))
    fireEvent.change(await screen.findByLabelText('New variable name'), {
      target: { value: 'LD_PRELOAD' },
    })
    fireEvent.change(screen.getByLabelText('New variable value'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: /^Save$/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/cannot be written through AgentOS/)
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByLabelText('New variable name')).toHaveValue('LD_PRELOAD')
  })

  it('closes on cancel and reopens blank', async () => {
    renderPage()
    await screen.findByText('OPENAI_API_KEY')

    fireEvent.click(screen.getByRole('button', { name: /Add variable/ }))
    fireEvent.change(await screen.findByLabelText('New variable name'), {
      target: { value: 'LEFTOVER' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Cancel$/ }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    fireEvent.click(screen.getByRole('button', { name: /Add variable/ }))
    expect(await screen.findByLabelText('New variable name')).toHaveValue('')
  })

  it('keeps the page mounted while loading instead of swapping it out', async () => {
    let release: (value: EnvListResponse) => void = () => {}
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'env.list') return new Promise((resolve) => (release = resolve))
      return Promise.resolve({})
    })
    renderPage()

    // Header and toolbar are present before any data arrives, so nothing
    // flashes apart and back together when it does.
    expect(screen.getByRole('heading', { name: /Environment/ })).toBeTruthy()
    expect(screen.getByLabelText('Search variables')).toBeTruthy()
    expect(screen.getByText('Loading variables…')).toBeTruthy()

    release(PAYLOAD)
    expect(await screen.findByText('OPENAI_API_KEY')).toBeTruthy()
    // Same heading node throughout — the view was never replaced.
    expect(screen.getByRole('heading', { name: /Environment/ })).toBeTruthy()
  })

  it('offers to import a credential that already exists elsewhere', async () => {
    // The point of the offer: stop telling someone to go find a token they
    // have already authenticated with somewhere.
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'env.list')
        return Promise.resolve({
          ...PAYLOAD,
          vars: [
            row({
              name: 'GITHUB_TOKEN',
              category: 'skill',
              owner: 'repo-triage',
              required: true,
              missing: true,
              availableFrom: { id: 'gh_cli', label: 'GitHub CLI' },
            }),
          ],
        })
      return Promise.resolve({})
    })
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /Use GitHub CLI/ }))
    await waitFor(() => {
      expect(mockRpc.call).toHaveBeenCalledWith('env.import', {
        name: 'GITHUB_TOKEN',
        sourceId: 'gh_cli',
      })
    })
  })

  it('does not offer an import for a variable that is already set', async () => {
    renderPage()
    await screen.findByText('OPENAI_API_KEY')
    expect(screen.queryByRole('button', { name: /^Use / })).toBeNull()
  })
})
