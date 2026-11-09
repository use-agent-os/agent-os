import '@/test/web'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { FakeRpc } from '@/test/fake-rpc'
import { testQueryClient, WebTestProviders } from '@/test/render-web'
import { useChatSession } from './useChatSession'

const A = 'agent:main:webchat:a'
const B = 'agent:main:webchat:b'

function chatRpc() {
  return new FakeRpc()
    .handle('chat.history', (p: Record<string, unknown>) => ({
      messages: [
        {
          id: `${p.sessionKey}-u`,
          message_id: `${p.sessionKey}-u`,
          role: 'user',
          text: `hi ${p.sessionKey}`,
          timestamp: 1,
        },
      ],
      has_more: false,
      newest_cursor: '1|x',
    }))
    .handle('sessions.messages.subscribe', (p: Record<string, unknown>) => ({
      subscribed: true,
      key: p.key,
      current_stream_seq: 0,
      replay_complete: true,
    }))
    .handle('sessions.messages.unsubscribe', null)
    .handle('chat.send', { ok: true, status: 'accepted' })
    .handle('chat.abort', { aborted: true })
}

function setup(rpc: FakeRpc, initial: string | null) {
  const queryClient = testQueryClient()
  const wrapper = ({ children }: { children: ReactNode }) => (
    <WebTestProviders rpc={rpc} queryClient={queryClient}>
      {children}
    </WebTestProviders>
  )
  return renderHook(({ k }: { k: string | null }) => useChatSession(k), {
    initialProps: { k: initial },
    wrapper,
  })
}

describe('useChatSession', () => {
  it('opens a session: loads history and subscribes', async () => {
    const rpc = chatRpc()
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.state.turns).toHaveLength(1))
    expect(rpc.callsTo('chat.history')[0]).toMatchObject({ sessionKey: A, includeCanonical: false })
    expect(rpc.callsTo('sessions.messages.subscribe')).toEqual([{ key: A, since_stream_seq: 0 }])
  })

  it('folds frames for its session only, dropping duplicates by stream_seq', async () => {
    const rpc = chatRpc()
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => {
      rpc.emit('session.event.text_delta', { session_key: A, stream_seq: 1, text: 'one ' })
      rpc.emit('session.event.text_delta', { session_key: A, stream_seq: 1, text: 'one ' })
      rpc.emit('session.event.text_delta', { session_key: B, stream_seq: 2, text: 'other' })
      rpc.emit('session.event.text_delta', { session_key: A, stream_seq: 2, text: 'two' })
    })
    const live = result.current.state.turns.at(-1)!
    expect(live.items).toEqual([expect.objectContaining({ kind: 'text', text: 'one two' })])
  })

  it('switches sessions without leaking the old subscription', async () => {
    const rpc = chatRpc()
    const baseline = rpc.listenerCount('*')
    const { result, rerender, unmount } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    rerender({ k: B })
    await waitFor(() => expect(result.current.state.turns[0]?.userMessage?.text).toBe(`hi ${B}`))
    expect(rpc.callsTo('sessions.messages.unsubscribe')).toEqual([{ key: A }])
    expect(rpc.callsTo('sessions.messages.subscribe').map((p) => p.key)).toEqual([A, B])
    expect(rpc.listenerCount('*')).toBe(baseline + 1)
    unmount()
    expect(rpc.callsTo('sessions.messages.unsubscribe')).toEqual([{ key: A }, { key: B }])
    expect(rpc.listenerCount('*')).toBe(baseline)
    expect(rpc.listenerCount('_hello')).toBe(0)
  })

  it('sends through chat.send with an idempotency key and shows the message at once', async () => {
    const rpc = chatRpc()
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    await act(() => result.current.send({ text: 'hello' }))
    const [params] = rpc.callsTo('chat.send')
    expect(params).toMatchObject({ message: 'hello', sessionKey: A })
    expect(typeof params!.idempotency_key).toBe('string')
    expect(result.current.state.turns.at(-1)!.userMessage).toMatchObject({ text: 'hello' })
  })

  it('reloads history after a terminal frame and on a gap', async () => {
    const rpc = chatRpc()
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    const before = rpc.callsTo('chat.history').length
    act(() =>
      rpc.emit('session.event.done', {
        session_key: A,
        stream_seq: 5,
        outcome: { kind: 'completed' },
      }),
    )
    await waitFor(() => expect(rpc.callsTo('chat.history').length).toBe(before + 1))
    act(() => rpc.emit('_gap', { expected: 3, actual: 9 }))
    await waitFor(() => expect(rpc.callsTo('chat.history').length).toBe(before + 2))
  })

  it('aborts the open session', async () => {
    const rpc = chatRpc()
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    await act(() => result.current.abort())
    expect(rpc.callsTo('chat.abort')).toEqual([{ sessionKey: A, source: 'web' }])
  })
})
