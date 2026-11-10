import '@/test/web'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { renderShell, resetShellState, shellRpc } from '@/test/web-shell'

beforeEach(resetShellState)

function newRpc(create: (p: Record<string, unknown>) => unknown) {
  return shellRpc()
    .handle('projects.list', { projects: [{ project_id: 'p-trading', name: 'Trading' }] })
    .handle('sessions.create', create)
    .handle('chat.send', { ok: true })
    .handle('chat.history', { messages: [] })
    .handle('sessions.messages.subscribe', { subscribed: true })
    .handle('sessions.messages.unsubscribe', null)
}

describe('New session page', () => {
  it('creates a session in the chosen project, sends the prompt and opens it', async () => {
    const user = userEvent.setup()
    const rpc = newRpc(() => ({ key: 'agent:main:webchat:n1' }))
    const { router } = renderShell('/new', rpc)
    const project = await screen.findByRole('combobox', { name: 'Project' })
    await waitFor(() => expect(screen.getByRole('option', { name: 'Trading' })).toBeInTheDocument())
    await user.selectOptions(screen.getByRole('combobox', { name: 'Agent' }), 'main')
    await user.selectOptions(project, 'p-trading')
    await user.type(screen.getByRole('textbox', { name: 'First message' }), 'Plan a BTC DCA')
    await user.click(screen.getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/chat/main/webchat/n1'))
    expect(rpc.callsTo('sessions.create')).toEqual([{ agentId: 'main', projectId: 'p-trading' }])
    expect(rpc.callsTo('chat.send')[0]).toMatchObject({
      sessionKey: 'agent:main:webchat:n1',
      message: 'Plan a BTC DCA',
    })
  })

  it('keeps Start disabled with its reason while defaults load', async () => {
    const rpc = newRpc(() => ({ key: 'k' })).handle('agents.list', () => new Promise(() => {}))
    renderShell('/new', rpc)
    expect(await screen.findByText('Loading agents and projects…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled()
  })

  it('stays on /new with the prompt and attachments restored when creation fails', async () => {
    const user = userEvent.setup()
    const rpc = newRpc(() => {
      throw new Error('quota exceeded')
    })
    const { router } = renderShell('/new', rpc)
    const prompt = await screen.findByRole('textbox', { name: 'First message' })
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Agent' })).not.toBeDisabled())
    await user.type(prompt, 'Hello there')
    const file = new File([new Uint8Array([137, 80, 78, 71])], 'pic.png', { type: 'image/png' })
    fireEvent.paste(prompt, { clipboardData: { files: [file], types: ['Files'] } })
    await screen.findByRole('button', { name: 'Remove pic.png' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start' })).not.toBeDisabled())
    await user.click(screen.getByRole('button', { name: 'Start' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "Couldn't start the session: quota exceeded",
    )
    expect(router.state.location.pathname).toBe('/new')
    expect(screen.getByRole('textbox', { name: 'First message' })).toHaveValue('Hello there')
    expect(screen.getByRole('button', { name: 'Remove pic.png' })).toBeInTheDocument()
  })

  it('sends a retry into the session the failed Start already created', async () => {
    const user = userEvent.setup()
    let attempts = 0
    const rpc = newRpc(() => ({ key: 'agent:main:webchat:n1' })).handle('chat.send', () => {
      if (++attempts === 1) throw new Error('provider down')
      return { ok: true }
    })
    const { router } = renderShell('/new', rpc)
    const prompt = await screen.findByRole('textbox', { name: 'First message' })
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Agent' })).toBeEnabled())
    await user.type(prompt, 'Hello')
    await user.click(screen.getByRole('button', { name: 'Start' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('provider down')
    await user.click(screen.getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/chat/main/webchat/n1'))
    expect(rpc.callsTo('sessions.create')).toHaveLength(1)
    expect(rpc.callsTo('chat.send').map((p) => p.sessionKey)).toEqual([
      'agent:main:webchat:n1',
      'agent:main:webchat:n1',
    ])
  })

  it('shows a failed agent list with Retry and keeps Start disabled with a reason', async () => {
    const user = userEvent.setup()
    let fail = true
    const rpc = newRpc(() => ({ key: 'k' })).handle('agents.list', () => {
      if (fail) throw new Error('agents unavailable')
      return { agents: [{ id: 'main', name: 'main' }] }
    })
    renderShell('/new', rpc)
    expect(await screen.findByText("Couldn't load the agents")).toBeInTheDocument()
    await user.type(screen.getByRole('textbox', { name: 'First message' }), 'Hi')
    expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled()
    expect(screen.getByText('No agent to start the session with')).toBeInTheDocument()
    fail = false
    await user.click(screen.getByRole('button', { name: /Retry/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled())
  })

  it('drops a preselected agent or project that no longer exists', async () => {
    const user = userEvent.setup()
    const rpc = newRpc(() => ({ key: 'agent:main:webchat:n2' }))
    renderShell('/new?agent=ghost&project=deleted', rpc)
    const project = await screen.findByRole('combobox', { name: 'Project' })
    await waitFor(() => expect(project).not.toBeDisabled())
    expect(project).toHaveValue('')
    await user.type(screen.getByRole('textbox', { name: 'First message' }), 'Hi')
    await user.click(screen.getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(rpc.callsTo('sessions.create')).toHaveLength(1))
    expect(rpc.callsTo('sessions.create')[0]).toEqual({ agentId: 'main' })
  })

  it('starts a chat in a project under the agent that owns it', async () => {
    const user = userEvent.setup()
    const rpc = newRpc(() => ({ key: 'agent:research:webchat:n3' })).handle('projects.list', {
      projects: [{ project_id: 'p-trading', name: 'Trading', agent_id: 'research' }],
    })
    renderShell('/new?project=p-trading', rpc)
    const agent = await screen.findByRole('combobox', { name: 'Agent' })
    await waitFor(() => expect(agent).toHaveValue('research'))
    await user.type(screen.getByRole('textbox', { name: 'First message' }), 'Hi')
    await user.click(screen.getByRole('button', { name: 'Start' }))
    await waitFor(() =>
      expect(rpc.callsTo('sessions.create')[0]).toEqual({
        agentId: 'research',
        projectId: 'p-trading',
      }),
    )
  })

  it('prefills the first message from ?prompt= and drops it from the address', async () => {
    const rpc = newRpc(() => ({ key: 'k' }))
    const { router } = renderShell('/new?prompt=use%20skill%20pdf', rpc)
    expect(await screen.findByRole('textbox', { name: 'First message' })).toHaveValue(
      'use skill pdf',
    )
    await waitFor(() => expect(router.state.location.search).toBe(''))
  })
})
