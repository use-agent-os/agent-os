import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import type { EnvListResponse, EnvVarRow } from '@/views/env/logic'
import { FOCUS_TTL_MS, jumpTo, takeFocus } from '../overview/jumps'
import { EnvironmentPane } from './EnvironmentPane'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {} }),
}))

const openExternal = vi.fn(async () => {})
const showItemInFolder = vi.fn(async () => {})
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal, showItemInFolder } }),
  isDesktop: () => true,
}))

function row(patch: Partial<EnvVarRow> & { name: string }): EnvVarRow {
  return {
    isSet: false,
    source: 'unset',
    masked: null,
    secret: true,
    description: '',
    url: '',
    category: 'provider',
    owner: '',
    required: false,
    writable: true,
    restartRequired: false,
    missing: false,
    availableFrom: null,
    ...patch,
  }
}

const LISTING: EnvListResponse = {
  envFilePath: '/tmp/agentos-home/.agentos/.env',
  vars: [
    row({
      name: 'OPENAI_API_KEY',
      isSet: true,
      source: 'home_file',
      masked: 'sk-…abcd',
      description: 'OpenAI key',
      url: 'https://platform.openai.com/api-keys',
    }),
    row({ name: 'ANTHROPIC_API_KEY', availableFrom: { id: 'claude-code', label: 'Claude Code' } }),
    row({ name: 'BRAVE_API_KEY', category: 'search', missing: true, owner: 'web-search' }),
    row({
      name: 'SHELL_TOKEN',
      category: 'custom',
      isSet: true,
      source: 'process',
      masked: '••••',
    }),
    row({ name: 'PATH', category: 'custom', isSet: true, source: 'process', writable: false }),
  ],
  setCount: 3,
  totalCount: 5,
  shadowedCount: 2,
}

function renderPane(onSheetKey?: (key: string) => void) {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {/* Stands in for the Settings sheet: what reaches it would close it. */}
      <div onKeyDown={(e) => onSheetKey?.(e.key)}>
        <EnvironmentPane />
      </div>
    </QueryClientProvider>,
  )
}

async function loaded() {
  await screen.findByTestId('env-row-OPENAI_API_KEY')
}

beforeEach(() => {
  // A jump from an earlier test is still fresh for a moment: let it lapse.
  takeFocus('environment', Date.now() + FOCUS_TTL_MS + 1)
  rpcCall.mockReset()
  openExternal.mockClear()
  showItemInFolder.mockClear()
  useConnection.setState({ state: 'connected' })
  rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
    if (method === 'env.list') return LISTING
    if (method === 'env.set') return { ...row({ name: String(params.name) }), isSet: true }
    if (method === 'env.reveal') return { value: 'sk-real-value' }
    return {}
  })
})

describe('EnvironmentPane', () => {
  it('asks for the gateway when it is not running, and never lists', () => {
    useConnection.setState({ state: 'disconnected' })
    renderPane()
    expect(screen.getByRole('heading', { name: 'Environment' })).toBeInTheDocument()
    expect(screen.getByText('Start the gateway to configure this.')).toBeInTheDocument()
    expect(rpcCall).not.toHaveBeenCalled()
  })

  it('summarises the listing, groups by category and folds the quiet tail', async () => {
    renderPane()
    await loaded()
    expect(rpcCall).toHaveBeenCalledWith('env.list', {})
    expect(screen.getByText('3 / 5')).toBeInTheDocument()
    expect(screen.getByText('…/.agentos/.env')).toBeInTheDocument()
    expect(
      screen.getByText(/Shadowed variables come from the process environment/),
    ).toBeInTheDocument()
    const providers = screen.getByRole('region', { name: 'LLM providers' })
    // ANTHROPIC_API_KEY is unset and not missing: folded until asked for.
    expect(within(providers).queryByTestId('env-row-ANTHROPIC_API_KEY')).toBeNull()
    fireEvent.click(within(providers).getByRole('button', { name: 'Show 1 unset' }))
    expect(within(providers).getByTestId('env-row-ANTHROPIC_API_KEY')).toBeInTheDocument()
    expect(within(providers).getByText('1/2 set')).toBeInTheDocument()
    // A read-only row offers no write controls.
    const path = screen.getByTestId('env-row-PATH')
    expect(within(path).queryByRole('button', { name: /Edit|Remove/ })).toBeNull()
  })

  it('turns a summary figure into the filter it counts', async () => {
    renderPane()
    await loaded()
    const summary = screen.getByRole('group', { name: 'Environment summary' })
    fireEvent.click(within(summary).getByRole('button', { name: /Missing/ }))
    expect(screen.getByRole('radio', { name: 'Missing' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByTestId('env-row-OPENAI_API_KEY')).toBeNull()
    expect(screen.getByTestId('env-row-BRAVE_API_KEY')).toBeInTheDocument()
    // A group holding only optional unset variables needs nothing: not listed.
    expect(screen.queryByRole('region', { name: 'LLM providers' })).toBeNull()
    fireEvent.click(within(summary).getByRole('button', { name: /Set/ }))
    expect(screen.getByRole('radio', { name: 'Set' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('env-row-OPENAI_API_KEY')).toBeInTheDocument()
    expect(screen.queryByTestId('env-row-BRAVE_API_KEY')).toBeNull()
    // Shadowed has no filter of its own: a figure, not a button.
    expect(within(summary).queryByRole('button', { name: /Shadowed/ })).toBeNull()
  })

  it('filters and searches', async () => {
    renderPane()
    await loaded()
    fireEvent.click(screen.getByRole('radio', { name: 'Missing' }))
    expect(screen.queryByTestId('env-row-OPENAI_API_KEY')).toBeNull()
    expect(screen.getByTestId('env-row-BRAVE_API_KEY')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'All' }))
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search variables' }), {
      target: { value: 'shell' },
    })
    expect(screen.getByTestId('env-row-SHELL_TOKEN')).toBeInTheDocument()
    expect(screen.queryByTestId('env-row-OPENAI_API_KEY')).toBeNull()
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search variables' }), {
      target: { value: 'zzz' },
    })
    expect(screen.getByText('No variables match this filter.')).toBeInTheDocument()
  })

  it('edits a value through env.set and re-lists', async () => {
    renderPane()
    await loaded()
    const openai = screen.getByTestId('env-row-OPENAI_API_KEY')
    fireEvent.click(within(openai).getByRole('button', { name: 'Edit OPENAI_API_KEY' }))
    const input = within(openai).getByLabelText('Value for OPENAI_API_KEY')
    expect(input).toHaveAttribute('type', 'password')
    fireEvent.change(input, { target: { value: 'sk-new' } })
    fireEvent.click(within(openai).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('env.set', { name: 'OPENAI_API_KEY', value: 'sk-new' }),
    )
    await waitFor(() => expect(within(openai).queryByLabelText(/Value for/)).toBeNull())
    expect(rpcCall.mock.calls.filter(([m]) => m === 'env.list').length).toBeGreaterThan(1)
  })

  it('imports from a source that already holds the credential', async () => {
    renderPane()
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Show 1 unset' }))
    fireEvent.click(screen.getByRole('button', { name: 'Use Claude Code' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('env.import', {
        name: 'ANTHROPIC_API_KEY',
        sourceId: 'claude-code',
      }),
    )
  })

  it('reveals only after confirming, and removes only after confirming', async () => {
    renderPane()
    await loaded()
    const openai = screen.getByTestId('env-row-OPENAI_API_KEY')
    fireEvent.click(within(openai).getByRole('button', { name: 'Reveal OPENAI_API_KEY' }))
    expect(rpcCall).not.toHaveBeenCalledWith('env.reveal', expect.anything())
    const reveal = screen.getByRole('alertdialog', { name: 'Show OPENAI_API_KEY?' })
    fireEvent.click(within(reveal).getByRole('button', { name: 'Show value' }))
    await waitFor(() => expect(within(openai).getByText('sk-real-value')).toBeInTheDocument())

    fireEvent.click(within(openai).getByRole('button', { name: 'Remove OPENAI_API_KEY' }))
    const remove = screen.getByRole('alertdialog', { name: 'Remove OPENAI_API_KEY?' })
    fireEvent.click(within(remove).getByRole('button', { name: 'Cancel' }))
    expect(rpcCall).not.toHaveBeenCalledWith('env.unset', expect.anything())
    fireEvent.click(within(openai).getByRole('button', { name: 'Remove OPENAI_API_KEY' }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('env.unset', { name: 'OPENAI_API_KEY' }),
    )
  })

  it('adds a variable, rejecting a bad name locally and keeping a server refusal on screen', async () => {
    renderPane()
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Add variable' }))
    const dialog = screen.getByRole('dialog', { name: 'Add a variable' })
    const name = within(dialog).getByLabelText('Name')
    fireEvent.change(name, { target: { value: '1BAD' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(within(dialog).getByRole('alert')).toHaveTextContent(/letters, digits/)
    expect(rpcCall).not.toHaveBeenCalledWith('env.set', expect.anything())

    rpcCall.mockImplementationOnce(async () => {
      throw new Error('refused by policy')
    })
    fireEvent.change(name, { target: { value: 'MY_TOKEN' } })
    fireEvent.change(within(dialog).getByLabelText('Value'), { target: { value: 'v' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(within(dialog).getByRole('alert')).toHaveTextContent('refused by policy'),
    )

    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add a variable' })).toBeNull())
    expect(rpcCall).toHaveBeenLastCalledWith('env.list', {})
    expect(rpcCall).toHaveBeenCalledWith('env.set', { name: 'MY_TOKEN', value: 'v' })
  })

  it('opens the key link and the .env folder through the desktop bridge', async () => {
    renderPane()
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Where to get this' }))
    expect(openExternal).toHaveBeenCalledWith('https://platform.openai.com/api-keys')
    fireEvent.click(screen.getByRole('button', { name: 'Show in Finder' }))
    expect(showItemInFolder).toHaveBeenCalledWith('/tmp/agentos-home/.agentos/.env')
  })

  it('shows the load error with a retry', async () => {
    rpcCall.mockRejectedValue(new Error('boom'))
    renderPane()
    expect(await screen.findByText('Environment unavailable: boom')).toBeInTheDocument()
    rpcCall.mockResolvedValue(LISTING)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await loaded()
  })

  it('shows every match of a search, including the unset ones the list folds away', async () => {
    renderPane()
    await loaded()
    const providers = screen.getByRole('region', { name: 'LLM providers' })
    expect(within(providers).queryByTestId('env-row-ANTHROPIC_API_KEY')).toBeNull()
    fireEvent.change(screen.getByLabelText('Search variables'), {
      target: { value: 'anthropic' },
    })
    expect(screen.getByTestId('env-row-ANTHROPIC_API_KEY')).toBeInTheDocument()
    // Nothing is folded while searching, so there is nothing to unfold.
    expect(screen.queryByRole('button', { name: /Show \d+ unset/ })).toBeNull()
  })

  it('clears the search on the first Escape and lets the second one close the sheet', async () => {
    const sheetKey = vi.fn()
    renderPane(sheetKey)
    await loaded()
    const search = screen.getByLabelText('Search variables')
    fireEvent.change(search, { target: { value: 'XAI' } })
    fireEvent.keyDown(search, { key: 'Escape' })
    expect(search).toHaveValue('')
    expect(sheetKey).not.toHaveBeenCalled()
    fireEvent.keyDown(search, { key: 'Escape' })
    expect(sheetKey).toHaveBeenCalledWith('Escape')
  })

  it('closes an open value editor on Escape without closing the sheet', async () => {
    const sheetKey = vi.fn()
    renderPane(sheetKey)
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Edit OPENAI_API_KEY' }))
    const value = screen.getByLabelText('Value for OPENAI_API_KEY')
    fireEvent.keyDown(value, { key: 'Escape' })
    expect(screen.queryByLabelText('Value for OPENAI_API_KEY')).toBeNull()
    expect(sheetKey).not.toHaveBeenCalled()
  })

  it('puts a jump’s variable in the search, on arrival and while already open', async () => {
    jumpTo('environment', 'BRAVE_API_KEY')
    renderPane()
    expect(await screen.findByTestId('env-row-BRAVE_API_KEY')).toBeInTheDocument()
    expect(screen.getByLabelText('Search variables')).toHaveValue('BRAVE_API_KEY')
    expect(screen.queryByTestId('env-row-OPENAI_API_KEY')).toBeNull()

    act(() => jumpTo('environment', 'ANTHROPIC_API_KEY'))
    expect(screen.getByLabelText('Search variables')).toHaveValue('ANTHROPIC_API_KEY')
    expect(screen.getByTestId('env-row-ANTHROPIC_API_KEY')).toBeInTheDocument()
  })

  it('gives focus back to Add variable when its dialog closes', async () => {
    renderPane()
    await loaded()
    const add = screen.getByRole('button', { name: 'Add variable' })
    add.focus()
    fireEvent.click(add)
    const dialog = screen.getByRole('dialog', { name: 'Add a variable' })
    expect(within(dialog).getByLabelText('Name')).toHaveFocus()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(add).toHaveFocus()
  })

  it('takes the keyboard to the search a jump filled', async () => {
    jumpTo('environment', 'BRAVE_API_KEY')
    renderPane()
    await screen.findByTestId('env-row-BRAVE_API_KEY')
    await waitFor(() => expect(screen.getByLabelText('Search variables')).toHaveFocus())
  })
})
