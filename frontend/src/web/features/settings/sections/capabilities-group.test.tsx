import '@/test/web'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { renderShell, resetShellState, shellRpc } from '@/test/web-shell'
import { oauthRedirect } from './mcp'

beforeEach(resetShellState)
afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

const OFFLINE = 'The gateway is offline. Reconnect to use this.'

// ── Skills ──────────────────────────────────────────────────────────────────

const weather = {
  name: 'weather',
  description: 'Forecasts',
  status: 'needs_setup',
  eligible: false,
  missing_env: ['WEATHER_KEY'],
  acquisition: { kind: 'hub', removable: true, updatable: true, identifier: 'acme/weather' },
  availability: { offered: false, reason: 'ineligible' },
}

// Ready, but the agent is never told about it.
const digest = {
  name: 'digest',
  status: 'ready',
  eligible: true,
  availability: { offered: false, reason: 'prompt_budget', detail: 'Over the prompt budget.' },
}

function skillsRpc() {
  return shellRpc()
    .handle('skills.list', { skills: [weather, digest] })
    .handle('skills.search', {
      results: [{ name: 'notes', identifier: 'hub/notes', source: 'clawhub', installed: false }],
    })
    .handle('skills.install', { success: true, name: 'notes' })
    .handle('skills.uninstall', { success: true })
    .handle('env.set', { ok: true })
}

describe('Settings › Skills', () => {
  it('installs a community skill', async () => {
    const user = userEvent.setup()
    const rpc = skillsRpc()
    renderShell('/settings/skills', rpc)
    await user.click(await screen.findByRole('tab', { name: 'Community' }))
    await user.click(await screen.findByRole('button', { name: 'Install notes' }))
    await waitFor(() =>
      expect(rpc.callsTo('skills.install')).toEqual([
        { identifier: 'hub/notes', source: 'clawhub', force: false },
      ]),
    )
  })

  it('disables Install with the reason while the gateway is offline', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    useConnection.getState().setState('disconnected')
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    renderShell('/settings/skills', skillsRpc())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_100)
    })
    await user.click(await screen.findByRole('tab', { name: 'Community' }))
    const install = await screen.findByRole('button', { name: 'Install notes' })
    expect(install).toBeDisabled()
    expect(install).toHaveAccessibleDescription(OFFLINE)
    expect(screen.getByRole('button', { name: 'Install' })).toBeDisabled()
  })

  it('shows the gap between installed and offered, and uninstalls after confirming', async () => {
    const user = userEvent.setup()
    const rpc = skillsRpc()
    renderShell('/settings/skills', rpc)
    expect(
      await screen.findByText('1 ready skill is not offered to the agent. Open it to see why.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Skill digest' })).toHaveTextContent(
      /Ready.*Not offered — prompt too long/,
    )
    await user.click(screen.getByRole('button', { name: 'Skill weather' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Uninstall' }))
    const confirm = await screen.findByRole('alertdialog')
    expect(rpc.callsTo('skills.uninstall')).toEqual([])
    await user.click(within(confirm).getByRole('button', { name: 'Uninstall' }))
    await waitFor(() => expect(rpc.callsTo('skills.uninstall')).toEqual([{ name: 'weather' }]))
  })

  it('sets a missing env key through a masked input', async () => {
    const user = userEvent.setup()
    const rpc = skillsRpc()
    renderShell('/settings/skills', rpc)
    await user.click(await screen.findByRole('button', { name: 'Skill weather' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Set WEATHER_KEY' }))
    const input = within(dialog).getByLabelText('Value for WEATHER_KEY')
    expect(input).toHaveAttribute('type', 'password')
    await user.type(input, 's3cret')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpc.callsTo('env.set')).toEqual([{ name: 'WEATHER_KEY', value: 's3cret' }]),
    )
  })

  it('shows the partner brand marks with alt text', async () => {
    const user = userEvent.setup()
    renderShell('/settings/skills', skillsRpc())
    await user.click(await screen.findByRole('tab', { name: 'Bankr' }))
    expect(await screen.findByRole('img', { name: 'Bankr logo' })).toBeInTheDocument()
  })
})

// ── MCP ─────────────────────────────────────────────────────────────────────

const remote = {
  name: 'remote',
  transport: 'streamable_http',
  url: 'https://mcp.example.com/mcp',
  command: null,
  args: [],
  env: {},
  headers: {},
  oauth: true,
  tool_timeout_seconds: 30,
}

function mcpRpc() {
  return shellRpc()
    .handle('config.snapshot', {
      config: { mcp: { enabled: true, servers: [remote] } },
      revision: 'r1',
    })
    .handle('mcp.status', {
      enabled: true,
      servers: [{ name: 'remote', oauth: true, authenticated: false, connected: false }],
    })
    .handle('mcp.oauth.start', { authorizationUrl: 'https://auth.example.com/authorize?x=1' })
}

describe('Settings › MCP', () => {
  it('starts OAuth with the callback URL and hands the browser to the authorization server', async () => {
    const user = userEvent.setup()
    const assign = vi.spyOn(oauthRedirect, 'assign').mockImplementation(() => {})
    const rpc = mcpRpc()
    renderShell('/settings/mcp', rpc)
    await user.click(await screen.findByRole('button', { name: 'Authorize' }))
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith('https://auth.example.com/authorize?x=1'),
    )
    const [params] = rpc.callsTo('mcp.oauth.start')
    expect(params?.name).toBe('remote')
    expect(String(params?.redirectUri)).toMatch(
      new RegExp(`^${window.location.origin}(/[\\w-]+)*/mcp/oauth/callback$`),
    )
  })

  it('shows the outcome the OAuth callback passes back', async () => {
    const { router } = renderShell('/settings', mcpRpc())
    await act(() => router.navigate('/settings/mcp', { state: { mcpOAuth: { ok: true } } }))
    expect(await screen.findByText(/Authorization complete/)).toBeInTheDocument()
  })

  it('shows the error the OAuth callback passes back', async () => {
    const { router } = renderShell('/settings', mcpRpc())
    await act(() =>
      router.navigate('/settings/mcp', {
        state: { mcpOAuth: { ok: false, message: 'access_denied' } },
      }),
    )
    expect(await screen.findByText('Authorization failed: access_denied')).toBeInTheDocument()
  })

  it('stages a removal and saves it through the save bar', async () => {
    const user = userEvent.setup()
    const rpc = mcpRpc().handle('config.patch', {}).handle('mcp.oauth.clear', {})
    renderShell('/settings/mcp', rpc)
    await user.click(await screen.findByRole('button', { name: 'Remove remote' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Remove' }),
    )
    expect(rpc.callsTo('config.patch')).toEqual([])
    await user.click(await screen.findByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpc.callsTo('config.patch')).toEqual([
        { patches: { 'mcp.servers': [] }, expectedRevision: 'r1' },
      ]),
    )
    await waitFor(() => expect(rpc.callsTo('mcp.oauth.clear')).toEqual([{ name: 'remote' }]))
  })
})

// ── Memory ──────────────────────────────────────────────────────────────────

function memoryRpc() {
  const curated = (target: string) => ({
    agentId: 'main',
    target,
    entries: target === 'memory' ? ['Prefers metric units'] : [],
    usage: '',
    charCount: 20,
    charLimit: 4000,
    loadFailed: false,
  })
  return shellRpc()
    .handle('memory.index', { chunkCount: 12, vecAvailable: true })
    .handle('memory.curated.get', (p: Record<string, unknown>) => curated(String(p.target)))
    .handle('memory.curated.add', { ok: true })
    .handle('memory.curated.remove', { ok: true })
    .handle('memory.knowledge_base.list', { documents: [] })
    .handle('memory.knowledge_base.ingest', { results: [{ chunksIndexed: 3 }] })
}

describe('Settings › Memory', () => {
  it('adds a curated entry', async () => {
    const user = userEvent.setup()
    const rpc = memoryRpc()
    renderShell('/settings/memory', rpc)
    const agent = await screen.findByRole('region', { name: 'Agent memory' })
    expect(await within(agent).findByText('Prefers metric units')).toBeInTheDocument()
    await user.click(within(agent).getByRole('button', { name: 'Add' }))
    await user.type(await screen.findByLabelText('Content'), 'Lives in Lisbon')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpc.callsTo('memory.curated.add')).toEqual([
        { agentId: 'main', target: 'memory', content: 'Lives in Lisbon' },
      ]),
    )
  })

  it('removes a curated entry only after confirming', async () => {
    const user = userEvent.setup()
    const rpc = memoryRpc()
    renderShell('/settings/memory', rpc)
    const agent = await screen.findByRole('region', { name: 'Agent memory' })
    await user.click(await within(agent).findByRole('button', { name: 'Remove entry' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Remove' }),
    )
    await waitFor(() =>
      expect(rpc.callsTo('memory.curated.remove')).toEqual([
        { agentId: 'main', target: 'memory', oldText: 'Prefers metric units' },
      ]),
    )
  })

  it('ingests a URL into the knowledge base', async () => {
    const user = userEvent.setup()
    const rpc = memoryRpc()
    renderShell('/settings/memory', rpc)
    await user.click(await screen.findByRole('tab', { name: 'Knowledge base' }))
    await user.type(await screen.findByLabelText('Add a file or URL'), 'https://example.com/a.md')
    await user.click(screen.getByRole('button', { name: 'Ingest' }))
    await waitFor(() =>
      expect(rpc.callsTo('memory.knowledge_base.ingest')).toEqual([
        { agentId: 'main', path: 'https://example.com/a.md' },
      ]),
    )
  })
})
