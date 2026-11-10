import '@/test/web'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useApprovals } from '@/services/approval-monitor'
import { useConnection } from '@/stores/connection'
import { renderShell, resetShellState, shellRpc } from '@/test/web-shell'

// Settings › Channels, Agents, Approvals and Environment (overhaul-control-ui
// 7.4, 7.5, 7.7, 7.14).

beforeEach(resetShellState)
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

const OFFLINE = 'The gateway is offline. Reconnect to use this.'

function channelsRpc() {
  return shellRpc()
    .handle('channels.status', {
      channels: [{ name: 'tg', type: 'telegram', status: 'running', connected: true }],
    })
    .handle('channels.pairing.list', {
      channels: [{ name: 'tg', pending: [{ sender_id: 42, username: 'alice' }], paired: [] }],
    })
    .handle('config.snapshot', {
      revision: 'r1',
      config: { channels: { channels: [{ name: 'tg', type: 'telegram' }] } },
      catalog: {
        channels: [
          {
            type: 'telegram',
            label: 'Telegram',
            fields: [
              { name: 'name', label: 'Name', required: true },
              { name: 'bot_token', label: 'Bot token', secret: true, required: true },
            ],
          },
        ],
      },
    })
    .handle('channels.pairing.approve', {})
}

function envRpc() {
  return shellRpc()
    .handle('env.list', {
      envFilePath: '~/.agentos/.env',
      setCount: 1,
      totalCount: 1,
      shadowedCount: 0,
      vars: [
        {
          name: 'OPENROUTER_API_KEY',
          isSet: true,
          source: 'home_file',
          masked: 'sk-or-…7f2a',
          secret: true,
          description: 'OpenRouter API key',
          url: '',
          category: 'provider',
          owner: '',
          required: false,
          writable: true,
          restartRequired: false,
          missing: false,
        },
      ],
    })
    .handle('env.reveal', { value: 'sk-or-v1-plaintext' })
}

describe('Channels', () => {
  it('lists a pending Telegram pairing request with Approve and Deny, and approves it', async () => {
    const rpc = channelsRpc()
    renderShell('/settings/channels', rpc)
    const card = await screen.findByRole('article', { name: 'Channel tg' })
    await within(card).findByText('@alice')
    expect(within(card).getByRole('button', { name: 'Deny' })).toBeEnabled()
    await userEvent.setup().click(within(card).getByRole('button', { name: 'Approve' }))
    await waitFor(() =>
      expect(rpc.callsTo('channels.pairing.approve')).toEqual([{ channel: 'tg', senderId: '42' }]),
    )
  })

  it('opens the setup dialog from ?setup=1', async () => {
    renderShell('/settings/channels?setup=1', channelsRpc())
    const dialog = await screen.findByRole('dialog', { name: 'Add a channel' })
    expect(await within(dialog).findByRole('radio', { name: /Telegram/ })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    expect(within(dialog).getByLabelText(/Bot token/)).toHaveAttribute('type', 'password')
  })
})

describe('Environment', () => {
  it('asks before revealing OPENROUTER_API_KEY, then masks it again after thirty seconds', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const rpc = envRpc()
    renderShell('/settings/environment', rpc)
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await user.click(await screen.findByRole('button', { name: 'Reveal OPENROUTER_API_KEY' }))
    // One click must not put the secret on screen.
    const confirm = await screen.findByRole('alertdialog', { name: 'Reveal OPENROUTER_API_KEY?' })
    expect(rpc.callsTo('env.reveal')).toEqual([])
    await user.click(within(confirm).getByRole('button', { name: 'Reveal' }))
    expect(await screen.findByText('sk-or-v1-plaintext')).toBeInTheDocument()
    expect(rpc.callsTo('env.reveal')).toEqual([{ name: 'OPENROUTER_API_KEY' }])

    act(() => {
      vi.advanceTimersByTime(15_000)
    })
    expect(screen.getByText('sk-or-v1-plaintext')).toBeInTheDocument()
    act(() => {
      vi.advanceTimersByTime(15_000)
    })
    await waitFor(() => expect(screen.queryByText('sk-or-v1-plaintext')).not.toBeInTheDocument())
    expect(screen.getByText('sk-or-…7f2a')).toBeInTheDocument()
  })

  it('cancelling the reveal confirmation fetches nothing', async () => {
    const rpc = envRpc()
    renderShell('/settings/environment', rpc)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Reveal OPENROUTER_API_KEY' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Reveal OPENROUTER_API_KEY?' })
    await user.click(within(confirm).getByRole('button', { name: 'Cancel' }))
    expect(rpc.callsTo('env.reveal')).toEqual([])
    expect(screen.queryByText('sk-or-v1-plaintext')).not.toBeInTheDocument()
  })

  it('disables Set with the offline reason once the gateway has been gone for the grace period', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    renderShell('/settings/environment', envRpc())
    const change = await screen.findByRole('button', { name: 'Change OPENROUTER_API_KEY' })
    expect(change).toBeEnabled()

    act(() => useConnection.getState().setState('disconnected'))
    act(() => {
      vi.advanceTimersByTime(2_100)
    })
    await waitFor(() => expect(change).toBeDisabled())
    const row = change.closest('li') as HTMLElement
    expect(within(row).getByText(OFFLINE)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add variable' })).toBeDisabled()
  })
})

describe('Offline', () => {
  it('disables pairing Approve with the offline reason', async () => {
    useConnection.getState().setState('disconnected')
    renderShell('/settings/channels', channelsRpc())
    const card = await screen.findByRole('article', { name: 'Channel tg' })
    expect(await within(card).findByRole('button', { name: 'Approve' })).toBeDisabled()
    expect(within(card).getByText(OFFLINE)).toBeInTheDocument()
  })
})

describe('Agents', () => {
  it('locks built-ins and deletes a custom agent after confirmation', async () => {
    const rpc = shellRpc()
      .handle('agents.list', {
        agents: [
          { id: 'main', name: 'main', type: 'builtin' },
          { id: 'research', name: 'Research', type: 'custom' },
        ],
      })
      .handle('agents.delete', {})
    renderShell('/settings/agents', rpc)
    const user = userEvent.setup()
    const builtin = await screen.findByRole('article', { name: 'Agent main' })
    expect(within(builtin).getByRole('button', { name: 'Customize' })).toBeEnabled()
    expect(within(builtin).queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()

    const custom = screen.getByRole('article', { name: 'Agent research' })
    await user.click(within(custom).getByRole('button', { name: 'Delete' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Delete research?' })
    await user.click(within(confirm).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(rpc.callsTo('agents.delete')).toEqual([{ id: 'research' }]))
  })
})

describe('Approvals', () => {
  it('shows the default mode and a pending request with its full arguments', async () => {
    const pending = {
      id: 'a1',
      toolName: 'bash',
      sessionKey: 'agent:main:web:1',
      args: { command: 'rm -rf build', cwd: '/srv/app' },
    }
    // The approval monitor backfills from GET /api/approvals on entry.
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      async () => new Response(JSON.stringify({ pending: [pending], mode: 'prompt' })),
    )
    useApprovals.getState().upsert(pending)
    const rpc = shellRpc().handle('config.snapshot', {
      revision: 'r1',
      config: { permissions: { default_mode: 'on' } },
    })
    renderShell('/settings/approvals', rpc)
    const mode = await screen.findByLabelText('Default permission mode')
    await waitFor(() => expect(mode).toHaveValue('on'))
    expect(mode).toBeEnabled()
    expect(screen.getByRole('radio', { name: /Ask every time/ })).toBeChecked()
    await userEvent.setup().click(screen.getByText('Full arguments'))
    expect(screen.getByText(/"cwd": "\/srv\/app"/)).toBeVisible()
  })

  it('re-reads the pending queue when Refresh is pressed', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn(
      async () => new Response(JSON.stringify({ pending: [], mode: 'prompt' }), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    renderShell('/settings/approvals')
    const refresh = await screen.findByRole('button', { name: 'Refresh' })
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const before = fetchMock.mock.calls.length
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          pending: [{ id: 'r1', namespace: 'exec', toolName: 'bash', command: 'make deploy' }],
          mode: 'prompt',
        }),
        { status: 200 },
      ),
    )
    await user.click(refresh)
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before))
    expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain('/api/approvals')
    expect(await screen.findByRole('heading', { name: '1 pending request' })).toBeInTheDocument()
    vi.unstubAllGlobals()
  })
})
