import '@/test/web'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { FakeRpc } from '@/test/fake-rpc'
import { testQueryClient, WebTestProviders } from '@/test/render-web'
import { isRunning } from './model/reducer'
import { STREAM_IDLE_TIMEOUT_MS, useChatSession } from './useChatSession'

afterEach(() => {
  vi.useRealTimers()
})

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
    // A first open takes the snapshot instead of a replay.
    expect(rpc.callsTo('sessions.messages.subscribe')).toEqual([{ key: A }])
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
    await waitFor(() =>
      expect(result.current.state.turns.at(-1)!.items).toEqual([
        expect.objectContaining({ kind: 'text', text: 'one two' }),
      ]),
    )
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

  it('drops replayed frames the in-flight snapshot already holds', async () => {
    const rpc = chatRpc().handle('sessions.messages.subscribe', {
      subscribed: true,
      current_stream_seq: 2,
      replay_complete: true,
      in_flight_run: { text: 'Hel', through_stream_seq: 2, started_at: 1 },
    })
    const { result } = setup(rpc, A)
    await waitFor(() => expect(isRunning(result.current.state)).toBe(true))
    act(() => {
      rpc.emit('session.event.text_delta', { session_key: A, stream_seq: 1, text: 'H' })
      rpc.emit('session.event.text_delta', { session_key: A, stream_seq: 2, text: 'el' })
      rpc.emit('session.event.text_delta', { session_key: A, stream_seq: 3, text: 'lo' })
    })
    await waitFor(() =>
      expect(result.current.state.turns.at(-1)!.items).toEqual([
        expect.objectContaining({ kind: 'text', text: 'Hello' }),
      ]),
    )
  })

  it('a reconnect replays from the last frame seen', async () => {
    const rpc = chatRpc()
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => rpc.emit('session.event.text_delta', { session_key: A, stream_seq: 7, text: 'x' }))
    act(() => rpc.emit('_hello'))
    await waitFor(() =>
      expect(rpc.callsTo('sessions.messages.subscribe').at(-1)).toEqual({
        key: A,
        since_stream_seq: 7,
      }),
    )
  })

  it('ends a turn the subscribe answer says is no longer running', async () => {
    const rpc = chatRpc()
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => rpc.emit('session.event.text_delta', { session_key: A, stream_seq: 1, text: 'x' }))
    await waitFor(() => expect(isRunning(result.current.state)).toBe(true))
    rpc.handle('sessions.messages.subscribe', {
      subscribed: true,
      current_stream_seq: 9,
      replay_complete: true,
      in_flight_run: null,
    })
    act(() => rpc.emit('_hello'))
    await waitFor(() => expect(isRunning(result.current.state)).toBe(false))
  })

  it('holds text frames for the next animation frame; other frames flush them in order', async () => {
    const rpc = chatRpc()
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => {
      for (let i = 1; i <= 50; i++)
        rpc.emit('session.event.text_delta', { session_key: A, stream_seq: i, text: 'x' })
    })
    // Not folded per frame: the burst waits for one paint.
    expect(result.current.state.turns.some((t) => t.status === 'live')).toBe(false)
    act(() =>
      rpc.emit('session.event.done', {
        session_key: A,
        stream_seq: 51,
        outcome: { kind: 'completed' },
      }),
    )
    const last = result.current.state.turns.at(-1)!
    expect(last.status).toBe('done')
    expect(last.items[0]).toMatchObject({ kind: 'text', text: 'x'.repeat(50) })
  })

  it('resyncs with a full window when a delta reload fails, without a stray rejection', async () => {
    const rpc = chatRpc()
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    let calls = 0
    rpc.handle('chat.history', (p: Record<string, unknown>) => {
      calls++
      if (p.delta_cursor) throw new Error('delta failed')
      return { messages: [], has_more: false, newest_cursor: '2|y' }
    })
    act(() => rpc.emit('_hello'))
    await waitFor(() => expect(calls).toBe(2))
    expect(rpc.callsTo('chat.history').at(-1)).not.toHaveProperty('delta_cursor')
  })

  it('done, then a reload that still names the run in flight: idle at once (F1)', async () => {
    const PREFIX = '[2026-11-10T11:31+07:00 Tue Asia/Bangkok]\n'
    let answered = false
    const rpc = chatRpc()
      .handle('chat.send', { ok: true, status: 'accepted', task_id: 'run-1' })
      .handle('chat.history', () => ({
        messages: answered
          ? [
              { id: 'u', message_id: 'u', role: 'user', text: `${PREFIX}run it`, timestamp: 1 },
              { id: 'a', message_id: 'a', role: 'assistant', text: 'DONE', timestamp: 2 },
            ]
          : [],
        has_more: false,
        newest_cursor: answered ? '2|a' : null,
        // The ledger lags the terminal frame: the run is still listed.
        in_flight_run: { run_id: 'run-1', task_id: 'run-1', text: '', through_stream_seq: 3 },
      }))
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    await act(() => result.current.send({ text: 'run it' }))
    act(() => {
      rpc.emit('session.event.text_delta', { session_key: A, stream_seq: 4, text: 'DONE' })
    })
    answered = true
    await act(async () => {
      rpc.emit('session.event.done', {
        session_key: A,
        stream_seq: 5,
        run_id: 'run-1',
        task_id: 'run-1',
        state: 'done',
        text: 'DONE',
      })
      await Promise.resolve()
    })
    await waitFor(() =>
      expect(result.current.state.rows.some((r) => r.role === 'assistant')).toBe(true),
    )
    expect(isRunning(result.current.state)).toBe(false)
    expect(result.current.state.turns.map((t) => t.userMessage?.text)).toEqual(['run it'])
    act(() => rpc.emit('task.succeeded', { session_key: A, stream_seq: 6, task_id: 'run-1' }))
    expect(isRunning(result.current.state)).toBe(false)
  })

  it('task.succeeded ends the running turn of its run when done was missed', async () => {
    const rpc = chatRpc().handle('chat.send', { ok: true, status: 'accepted', task_id: 'run-7' })
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    await act(() => result.current.send({ text: 'go' }))
    act(() => rpc.emit('session.event.text_delta', { session_key: A, stream_seq: 1, text: 'x' }))
    await waitFor(() => expect(isRunning(result.current.state)).toBe(true))
    act(() => rpc.emit('task.succeeded', { session_key: A, stream_seq: 2, task_id: 'run-7' }))
    expect(isRunning(result.current.state)).toBe(false)
  })

  it('a second Stop while the first is pending stops at once; a third is not sent', async () => {
    const rpc = chatRpc()
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => rpc.emit('session.event.tool_use_start', { session_key: A, tool_use_id: 't' }))
    const releases: Array<(v: unknown) => void> = []
    rpc.handle('chat.abort', () => new Promise((r) => releases.push(r)))
    let first!: Promise<void>
    let second!: Promise<void>
    act(() => {
      first = result.current.abort()
    })
    expect(result.current.state.turns.at(-1)).toMatchObject({ abortRequested: true })
    expect(result.current.state.turns.at(-1)!.forceStop).toBeUndefined()
    act(() => {
      second = result.current.abort()
    })
    expect(result.current.state.turns.at(-1)).toMatchObject({ forceStop: true })
    await act(() => result.current.abort())
    expect(rpc.callsTo('chat.abort')).toEqual([
      { sessionKey: A, source: 'web' },
      { sessionKey: A, source: 'web' },
    ])
    // The gateway joins the two stops: both answer together, then the run ends.
    const answer = { sessionKey: A, aborted: ['run-1'], children_aborted: [], key: A }
    await act(async () => {
      releases.forEach((r) => r(answer))
      await Promise.all([first, second])
    })
    act(() => rpc.emit('session.event.done', { session_key: A, state: 'aborted', run_id: 'run-1' }))
    expect(result.current.state.turns.at(-1)!.status).toBe('aborted')
  })

  it('a refused second Stop offers Stop now again and keeps the first pending', async () => {
    const rpc = chatRpc()
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => rpc.emit('session.event.tool_use_start', { session_key: A, tool_use_id: 't' }))
    let calls = 0
    rpc.handle('chat.abort', () => {
      calls += 1
      if (calls === 1) return new Promise(() => {})
      throw new Error('gateway busy')
    })
    act(() => {
      void result.current.abort()
    })
    await expect(act(() => result.current.abort())).rejects.toThrow('gateway busy')
    expect(result.current.state.turns.at(-1)).toMatchObject({ abortRequested: true })
    expect(result.current.state.turns.at(-1)!.forceStop).toBeUndefined()
  })

  it('rejects a refused abort and clears the stop request', async () => {
    const rpc = chatRpc().handle('chat.abort', () => {
      throw new Error('gateway busy')
    })
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => rpc.emit('session.event.tool_use_start', { session_key: A, tool_use_id: 't' }))
    await expect(act(() => result.current.abort())).rejects.toThrow('gateway busy')
    expect(result.current.state.turns.at(-1)!.abortRequested).toBeUndefined()
  })

  it('checks a silent running turn against the gateway', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const rpc = chatRpc()
    const { result } = setup(rpc, A)
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => rpc.emit('session.event.tool_use_start', { session_key: A, tool_use_id: 't' }))
    const before = rpc.callsTo('chat.history').length
    await act(async () => {
      vi.advanceTimersByTime(STREAM_IDLE_TIMEOUT_MS + 10)
    })
    await waitFor(() => expect(rpc.callsTo('chat.history').length).toBe(before + 1))
  })
})
