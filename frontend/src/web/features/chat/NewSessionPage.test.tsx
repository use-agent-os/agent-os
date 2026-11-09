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
})
