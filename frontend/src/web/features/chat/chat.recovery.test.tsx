import '@/test/web'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { CHAT_PATH, KEY, chatRpc, emit, openChat, resetChatState, userRow } from '@/test/web-chat'
import { readQueue, resetQueuesForTests } from './composer/queue'
import { initialTranscript } from './model/reducer'
import type { TranscriptState, Turn } from './model/types'
import { contentVersion } from './transcript/TranscriptView'

// The chat workspace's send / stop / queue paths and restored parity
// features, driven through the real routes.

beforeEach(() => {
  resetChatState()
  resetQueuesForTests()
})
afterEach(() => {
  vi.restoreAllMocks()
})

function log() {
  return screen.getByRole('log', { name: 'Conversation' })
}

/** A history whose run is still going (the gateway's snapshot says so). */
function runningRpc(rows: Record<string, unknown>[] = [userRow('u1', 'go')]) {
  return chatRpc(rows).handle('chat.history', {
    messages: rows,
    has_more: false,
    newest_cursor: '1|x',
    in_flight_run: { text: 'working', through_stream_seq: 0, started_at: 1 },
  })
}

describe('slash text', () => {
  it('sends a path-like message instead of swallowing it', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc()
    const { composer } = await openChat(rpc)
    await user.type(composer, '/var/log/gateway.log is full, why?{Enter}')
    await waitFor(() =>
      expect(rpc.callsTo('chat.send')[0]).toMatchObject({
        message: '/var/log/gateway.log is full, why?',
      }),
    )
    expect(composer).toHaveValue('')
  })

  it('sends //text as a literal /text', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc()
    const { composer } = await openChat(rpc)
    await user.type(composer, '//compact is a command{Enter}')
    await waitFor(() =>
      expect(rpc.callsTo('chat.send')[0]).toMatchObject({ message: '/compact is a command' }),
    )
    expect(rpc.callsTo('sessions.contextCompact')).toEqual([])
  })

  it('shows /model output in the transcript', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc().handle('commands.list_for_surface', {
      commands: [{ name: '/model', description: 'Models', execution: { action: 'models.list' } }],
    })
    const { composer } = await openChat(rpc)
    await user.type(composer, '/model ')
    await user.keyboard('{Enter}')
    const note = await within(log()).findByText(/Available models \(1\)/)
    expect(note.closest('[data-role="note"]')).not.toBeNull()
    expect(rpc.callsTo('chat.send')).toEqual([])
  })
})

describe('the queue', () => {
  it('survives leaving the session and coming back', async () => {
    const user = userEvent.setup()
    const rpc = runningRpc()
    const first = await openChat(rpc)
    await within(log()).findByText('working')
    await user.type(first.composer, 'also check BTC')
    await user.click(screen.getByRole('button', { name: 'Queue' }))
    first.unmount()
    await openChat(rpc)
    const queue = await screen.findByRole('region', { name: 'Queued messages' })
    expect(within(queue).getByText('also check BTC')).toBeInTheDocument()
  })

  it('goes back into the composer after Stop instead of sending', async () => {
    const user = userEvent.setup()
    const rpc = runningRpc()
    const { composer } = await openChat(rpc)
    await within(log()).findByText('working')
    await user.type(composer, 'next one')
    await user.click(screen.getByRole('button', { name: 'Queue' }))
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    rpc.handle('chat.history', { messages: [userRow('u1', 'go')], in_flight_run: null })
    emit(rpc, 'done', { state: 'aborted', partial_text: 'working', stream_seq: 1 })
    await waitFor(() => expect(composer).toHaveValue('next one'))
    await new Promise((r) => setTimeout(r, 120))
    expect(rpc.callsTo('chat.send')).toEqual([])
    expect(readQueue(KEY)).toEqual([])
  })

  it('takes the newest queued message back with Alt+↑', async () => {
    const user = userEvent.setup()
    const rpc = runningRpc()
    const { composer } = await openChat(rpc)
    await within(log()).findByText('working')
    for (const text of ['one', 'two']) {
      await user.type(composer, text)
      await user.click(screen.getByRole('button', { name: 'Queue' }))
    }
    await user.click(composer)
    await user.keyboard('{Alt>}{ArrowUp}{/Alt}')
    expect(composer).toHaveValue('two')
    expect(readQueue(KEY).map((q) => q.text)).toEqual(['one'])
  })

  it('reports its length in Details', async () => {
    const user = userEvent.setup()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1600 })
    const rpc = runningRpc()
    const { composer } = await openChat(rpc)
    await within(log()).findByText('working')
    await user.type(composer, 'later')
    await user.click(screen.getByRole('button', { name: 'Queue' }))
    const details = await screen.findByRole('complementary', { name: 'Session details' })
    await waitFor(() =>
      expect(within(details).getByText('Queued messages').nextSibling).toHaveTextContent('1'),
    )
  })

  it('holds a card answer given while a turn runs', async () => {
    const user = userEvent.setup()
    const result = JSON.stringify({
      status: 'question_presented',
      questions: [{ question: 'Which day?', options: [{ label: 'Mon' }, { label: 'Tue' }] }],
    })
    const rpc = chatRpc([userRow('u1', 'book')])
    await openChat(rpc)
    emit(rpc, 'tool_use_start', { tool_use_id: 'q', tool_name: 'ask_user', stream_seq: 1 })
    emit(rpc, 'tool_result', {
      tool_use_id: 'q',
      tool_name: 'ask_user',
      result,
      is_error: false,
      stream_seq: 2,
    })
    await user.click(await within(log()).findByRole('radio', { name: 'Tue' }))
    await user.click(within(log()).getByRole('button', { name: 'Send answer' }))
    expect(rpc.callsTo('chat.send')).toEqual([])
    expect(readQueue(KEY)[0]?.text).toContain('Tue')
  })
})

describe('stop', () => {
  it('stays reachable beside Queue and shows Stopping… once asked', async () => {
    const user = userEvent.setup()
    const rpc = runningRpc().handle('chat.abort', () => new Promise(() => {}))
    const { composer } = await openChat(rpc)
    await within(log()).findByText('working')
    await user.type(composer, 'draft')
    expect(screen.getByRole('button', { name: 'Queue' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    const stopping = await screen.findByRole('button', { name: 'Stopping…' })
    expect(stopping).toBeDisabled()
    expect(within(log()).getByText('Stopping…')).toBeInTheDocument()
    expect(rpc.callsTo('chat.abort')).toHaveLength(1)
  })

  it('says so when the gateway refuses to stop', async () => {
    const user = userEvent.setup()
    const error = vi.spyOn(toast, 'error')
    const rpc = runningRpc().handle('chat.abort', () => {
      throw new Error('gateway busy')
    })
    await openChat(rpc)
    await within(log()).findByText('working')
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(error).toHaveBeenCalledWith("Couldn't stop the turn: gateway busy"))
    expect(screen.getByRole('button', { name: 'Stop' })).toBeEnabled()
  })

  it('leaves an Escape that cancels an IME composition alone', async () => {
    const rpc = runningRpc()
    const { composer } = await openChat(rpc)
    await within(log()).findByText('working')
    fireEvent.keyDown(composer, { key: 'Escape', isComposing: true })
    fireEvent.keyDown(composer, { key: 'Escape', keyCode: 229 })
    expect(rpc.callsTo('chat.abort')).toEqual([])
    fireEvent.keyDown(composer, { key: 'Escape' })
    expect(rpc.callsTo('chat.abort')).toHaveLength(1)
  })
})

describe('restored parity', () => {
  it('shows Working… between the send and the first output', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc()
    const { composer } = await openChat(rpc)
    await user.type(composer, 'hello{Enter}')
    expect(await within(log()).findByText('Working…')).toBeInTheDocument()
    emit(rpc, 'text_delta', { text: 'Hi there' })
    await within(log()).findByText('Hi there')
    expect(within(log()).queryByText('Working…')).not.toBeInTheDocument()
  })

  it('offers the suggested next prompt after a turn and takes it with Tab', async () => {
    const user = userEvent.setup()
    const rpc = runningRpc().handle('chat.suggestNext', { text: 'Now chart ETH' })
    const { composer } = await openChat(rpc)
    await within(log()).findByText('working')
    rpc.handle('chat.history', { messages: [userRow('u1', 'go')], in_flight_run: null })
    emit(rpc, 'done', { text: 'working', outcome: { kind: 'completed' }, stream_seq: 1 })
    await waitFor(() => expect(composer).toHaveAttribute('placeholder', 'Now chart ETH'))
    await user.click(composer)
    await user.keyboard('{Tab}')
    expect(composer).toHaveValue('Now chart ETH')
  })

  it('resets the session from the header menu', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc([userRow('u1', 'a')]).handle('sessions.reset', { ok: true })
    await openChat(rpc)
    await user.click(screen.getByRole('button', { name: 'Session actions' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Reset session' }))
    await waitFor(() => expect(rpc.callsTo('sessions.reset')).toEqual([{ key: KEY }]))
  })

  it('prefills the composer from ?prompt= and drops it from the address', async () => {
    const { router, composer } = await openChat(
      chatRpc(),
      `${CHAT_PATH}?prompt=${encodeURIComponent('use skill pdf\n')}`,
    )
    await waitFor(() => expect(composer).toHaveValue('use skill pdf\n'))
    expect(router.state.location.search).toBe('')
    expect(router.state.location.pathname).toBe(CHAT_PATH)
  })

  it('keeps ?prompt= through /chat', async () => {
    const { composer } = await openChat(chatRpc(), '/chat?prompt=use%20skill%20pdf')
    await waitFor(() => expect(composer).toHaveValue('use skill pdf'))
  })

  it('shows messages waiting on the gateway', async () => {
    const rpc = runningRpc().handle('chat.history', {
      messages: [userRow('u1', 'go')],
      in_flight_run: { text: 'working', started_at: 1 },
      pending_inputs: [
        { task_id: 't2', text: 'and then this', steer: false },
        { task_id: 't3', text: 'already listed', message_id: 'm' },
      ],
    })
    await openChat(rpc)
    const region = await screen.findByRole('region', { name: 'Waiting on the gateway' })
    expect(within(region).getByText('Queued for the next turn: and then this')).toBeInTheDocument()
    expect(within(region).queryByText(/already listed/)).not.toBeInTheDocument()
  })

  it('loads older history when the first page is too short to scroll', async () => {
    const rpc = chatRpc().handle('chat.history', (p: Record<string, unknown>) =>
      p.before
        ? { messages: [userRow('u0', 'oldest', 1)], has_more: false, oldest_cursor: '0|u0' }
        : {
            messages: [userRow('u1', 'newest', 2)],
            has_more: true,
            oldest_cursor: '1|u1',
            newest_cursor: '1|u1',
          },
    )
    await openChat(rpc)
    expect(await within(log()).findByText('oldest')).toBeInTheDocument()
    expect(screen.queryByText('Loading earlier messages…')).not.toBeInTheDocument()
  })
})

describe('auto-follow', () => {
  const turn = (items: Turn['items']): TranscriptState => {
    const state = initialTranscript()
    const live: Turn = {
      id: 't',
      userMessage: null,
      items,
      status: 'live',
      startedAt: 0,
      endedAt: null,
    }
    return { ...state, turns: [live] }
  }

  it('changes its version as text streams in before a trailing chart', () => {
    const chart = {
      kind: 'card' as const,
      id: 'c',
      category: 'chart',
      artifact: { id: 'a', name: 'chart' },
    }
    const before = contentVersion(turn([{ kind: 'text', id: 'x', text: 'Hel' }, chart]))
    const after = contentVersion(turn([{ kind: 'text', id: 'x', text: 'Hello' }, chart]))
    expect(after).not.toBe(before)
  })
})
