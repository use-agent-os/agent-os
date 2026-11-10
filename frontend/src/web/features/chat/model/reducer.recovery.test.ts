import { describe, expect, it } from 'vitest'
import { initialTranscript, isRunning, reduce } from './reducer'
import type { HistoryPage, HistoryRow, TranscriptState } from './types'

// Recovery paths of the transcript model: missed terminal frames, snapshots
// plus replayed frames, failures history did not keep, repeated text.

const row = (id: string, role: string, text: string, timestamp: number): HistoryRow => ({
  id,
  message_id: id,
  role,
  text,
  timestamp,
})

function page(state: TranscriptState, p: HistoryPage, issuedAt?: number): TranscriptState {
  return reduce(state, { type: 'historyPage', page: p, mode: 'replace', issuedAt })
}

function frame(
  state: TranscriptState,
  kind: string,
  payload: Record<string, unknown> = {},
  at = 10,
): TranscriptState {
  return reduce(state, { type: 'event', name: `session.event.${kind}`, payload, at })
}

function sent(state: TranscriptState, text: string, key: string, at: number): TranscriptState {
  const s = reduce(state, { type: 'optimisticSend', text, idempotencyKey: key, at })
  return reduce(s, { type: 'sendAccepted', idempotencyKey: key, at })
}

const textOf = (state: TranscriptState) =>
  state.turns
    .at(-1)!
    .items.filter((i) => i.kind === 'text')
    .map((i) => (i as { text: string }).text)
    .join('')

describe('in_flight_run: null is authoritative', () => {
  it('ends a live turn whose terminal frame was missed', () => {
    let state = sent(initialTranscript(), 'go', 'k', 100)
    state = frame(state, 'text_delta', { text: 'partial' }, 110)
    expect(isRunning(state)).toBe(true)
    state = page(state, { messages: [row('u', 'user', 'go', 100)], in_flight_run: null }, 200)
    expect(isRunning(state)).toBe(false)
    expect(state.turns.at(-1)!.status).not.toBe('live')
  })

  it('keeps a run accepted after the history request went out', () => {
    let state = sent(initialTranscript(), 'go', 'k', 300)
    state = page(state, { messages: [], in_flight_run: null }, 200)
    expect(isRunning(state)).toBe(true)
  })

  it('never ends a send still in flight', () => {
    const state = reduce(initialTranscript(), {
      type: 'optimisticSend',
      text: 'go',
      idempotencyKey: 'k',
      at: 100,
    })
    expect(isRunning(page(state, { messages: [], in_flight_run: null }, 200))).toBe(true)
  })

  it('a page from an older gateway (no in_flight_run key) proves nothing', () => {
    let state = sent(initialTranscript(), 'go', 'k', 100)
    state = page(state, { messages: [row('u', 'user', 'go', 100)] }, 200)
    expect(isRunning(state)).toBe(true)
  })

  it('the subscribe answer ends live turns and subagent groups, then asks for a reload', () => {
    let state = sent(initialTranscript(), 'go', 'k', 100)
    state = reduce(state, {
      type: 'event',
      name: 'session.event.task_group.waiting',
      payload: { group_id: 'g' },
      at: 120,
    })
    state = reduce(state, { type: 'runState', inFlight: null, issuedAt: 200 })
    expect(isRunning(state)).toBe(false)
    expect(state.activeGroups).toEqual([])
    expect(state.needsResync).toBe(true)
  })
})

describe('in-flight snapshot and replayed frames', () => {
  const opened = () =>
    page(initialTranscript(), {
      messages: [row('u', 'user', 'say hello', 1)],
      in_flight_run: { text: 'Hello wor', through_stream_seq: 3, started_at: 1 },
    })

  it('does not double text the snapshot already holds', () => {
    let state = opened()
    state = frame(state, 'text_delta', { text: 'Hel', stream_seq: 1 })
    state = frame(state, 'text_delta', { text: 'lo ', stream_seq: 2 })
    state = frame(state, 'text_delta', { text: 'wor', stream_seq: 3 })
    expect(textOf(state)).toBe('Hello wor')
    state = frame(state, 'text_delta', { text: 'ld', stream_seq: 4 })
    expect(textOf(state)).toBe('Hello world')
  })

  it('ignores a replayed terminal frame of an earlier, finished turn', () => {
    let state = opened()
    state = frame(state, 'done', { text: 'old', stream_seq: 2, outcome: { kind: 'completed' } })
    expect(isRunning(state)).toBe(true)
  })

  it('a reset stream (gateway restart) takes low stream_seq values again', () => {
    let state = reduce(opened(), { type: 'streamReset' })
    state = frame(state, 'text_delta', { text: 'ld', stream_seq: 1 })
    expect(textOf(state)).toBe('Hello world')
  })

  it('builds tool rows, the plan and the truncation flag from the snapshot', () => {
    const state = page(initialTranscript(), {
      messages: [row('u', 'user', 'plan it', 1)],
      in_flight_run: {
        text: '…tail',
        text_truncated: true,
        through_stream_seq: 9,
        plan: 'Step 1',
        events: [
          { kind: 'tool_use_start', tool_use_id: 't1', tool_name: 'bash', stream_seq: 2 },
          { kind: 'tool_result', tool_use_id: 't1', tool_name: 'bash', stream_seq: 3 },
          { kind: 'tool_use_start', tool_use_id: 't2', tool_name: 'web_search', stream_seq: 4 },
          { kind: 'artifact', id: 'a', stream_seq: 5 },
        ],
      },
    })
    const turn = state.turns.at(-1)!
    expect(turn.textTruncated).toBe(true)
    expect(turn.items.map((i) => i.kind)).toEqual(['toolCall', 'toolCall', 'plan', 'text'])
    expect(turn.items.filter((i) => i.kind === 'toolCall').map((i) => i.state)).toEqual([
      'done',
      'running',
    ])
  })

  it('rebuilds a live turn that missed frames the snapshot covers', () => {
    let state = sent(initialTranscript(), 'go', 'k', 100)
    state = frame(state, 'text_delta', { text: 'a', stream_seq: 1 })
    state = reduce(state, {
      type: 'runState',
      inFlight: { text: 'abc', through_stream_seq: 3 },
      issuedAt: 200,
    })
    expect(textOf(state)).toBe('abc')
    state = frame(state, 'text_delta', { text: 'c', stream_seq: 3 })
    state = frame(state, 'text_delta', { text: 'd', stream_seq: 4 })
    expect(textOf(state)).toBe('abcd')
  })

  it('leaves a live turn that is ahead of the snapshot alone', () => {
    let state = sent(initialTranscript(), 'go', 'k', 100)
    state = frame(state, 'text_delta', { text: 'abcd', stream_seq: 5 })
    state = reduce(state, {
      type: 'runState',
      inFlight: { text: 'abc', through_stream_seq: 3 },
      issuedAt: 200,
    })
    expect(textOf(state)).toBe('abcd')
  })
})

describe('a failed turn survives the history reload', () => {
  it('keeps the reason when history has no error row, and stops the spinner', () => {
    let state = sent(initialTranscript(), 'go', 'k', 100)
    state = frame(state, 'tool_use_start', { tool_use_id: 't', tool_name: 'bash' })
    state = frame(state, 'error', { message: 'Timed out', code: 'timeout' })
    expect(state.turns.at(-1)).toMatchObject({ status: 'failed', failure: 'Timed out' })
    state = page(state, {
      messages: [
        row('u', 'user', 'go', 100),
        {
          ...row('a', 'assistant', '', 101),
          tool_calls: [{ type: 'tool_use', tool_use_id: 't', name: 'bash', input: {} }],
        },
      ],
      in_flight_run: null,
    })
    expect(state.turns).toHaveLength(1)
    const turn = state.turns[0]!
    expect(turn).toMatchObject({ status: 'failed', failure: 'Timed out' })
    expect(turn.items.find((i) => i.kind === 'toolCall')).toMatchObject({ state: 'failed' })
  })

  it('history’s own error row wins', () => {
    let state = sent(initialTranscript(), 'go', 'k', 100)
    state = frame(state, 'error', { message: 'live reason' })
    state = page(state, {
      messages: [row('u', 'user', 'go', 100), row('e', 'error', 'stored reason', 101)],
    })
    expect(state.turns[0]).toMatchObject({ status: 'failed', failure: 'stored reason' })
  })
})

describe('repeated user text', () => {
  const earlier = [row('u1', 'user', 'continue', 1), row('a1', 'assistant', 'one', 2)]

  it('matches a live turn only to a row after the one it was sent behind', () => {
    let state = page(initialTranscript(), { messages: earlier })
    // Turn A ends; the queued "continue" goes out before A's reload lands.
    state = sent(state, 'continue', 'kA', 100)
    state = frame(state, 'text_delta', { text: 'two' }, 110)
    state = frame(state, 'done', { text: 'two', outcome: { kind: 'completed' } }, 120)
    state = sent(state, 'continue', 'kB', 130)
    state = frame(state, 'text_delta', { text: 'three' }, 140)
    state = page(state, {
      messages: [...earlier, row('u2', 'user', 'continue', 100), row('a2', 'assistant', 'two', 3)],
    })
    expect(isRunning(state)).toBe(true)
    expect(state.turns).toHaveLength(3)
    expect(state.turns.map((t) => t.status)).toEqual(['done', 'done', 'live'])
    expect(textOf(state)).toBe('three')
  })

  it('keeps a failed send of repeated text over a reload', () => {
    let state = page(initialTranscript(), { messages: earlier })
    state = reduce(state, {
      type: 'optimisticSend',
      text: 'continue',
      idempotencyKey: 'k',
      at: 100,
    })
    state = reduce(state, {
      type: 'sendFailed',
      idempotencyKey: 'k',
      error: 'boom',
      notSent: false,
    })
    state = page(state, { messages: earlier })
    expect(state.turns).toHaveLength(2)
    expect(state.turns[1]).toMatchObject({ status: 'failed', failure: 'boom' })
  })

  it('does not hide the previous same-text turn behind a live one', () => {
    let state = page(initialTranscript(), {
      messages: [...earlier, row('u2', 'user', 'continue', 3), row('a2', 'assistant', 'two', 4)],
    })
    state = sent(state, 'continue', 'k', 100)
    state = frame(state, 'text_delta', { text: 'three' }, 110)
    expect(state.turns.map((t) => t.items.find((i) => i.kind === 'text'))).toEqual([
      expect.objectContaining({ text: 'one' }),
      expect.objectContaining({ text: 'two' }),
      expect.objectContaining({ text: 'three' }),
    ])
  })
})

describe('local notes, stop requests and the fold cache', () => {
  it('keeps a slash-command note over a reload, in place', () => {
    let state = page(initialTranscript(), { messages: [row('u1', 'user', 'hi', 1)] })
    state = reduce(state, { type: 'systemNote', text: 'Available models (2):', at: 5_000 })
    state = page(state, {
      messages: [row('u1', 'user', 'hi', 1), row('u2', 'user', 'later', 9)],
    })
    expect(state.turns.map((t) => t.userMessage?.text ?? t.items[0]?.kind)).toEqual([
      'hi',
      'note',
      'later',
    ])
  })

  it('marks and clears a stop request', () => {
    let state = sent(initialTranscript(), 'go', 'k', 1)
    state = reduce(state, { type: 'abortRequested' })
    expect(state.turns.at(-1)!.abortRequested).toBe(true)
    state = reduce(state, { type: 'abortFailed' })
    expect(state.turns.at(-1)!.abortRequested).toBeUndefined()
  })

  it('reuses history turn objects across live frames', () => {
    let state = page(initialTranscript(), {
      messages: [row('u1', 'user', 'hi', 1), row('a1', 'assistant', 'yo', 2)],
    })
    state = sent(state, 'more', 'k', 10)
    const before = state.turns[0]
    state = frame(state, 'text_delta', { text: 'x' })
    state = frame(state, 'text_delta', { text: 'y' })
    expect(state.turns[0]).toBe(before)
  })

  it('folds a batch of frames like one at a time', () => {
    const start = sent(initialTranscript(), 'go', 'k', 1)
    const one = frame(frame(start, 'text_delta', { text: 'a' }), 'text_delta', { text: 'b' })
    const batch = reduce(start, {
      type: 'events',
      events: [
        { name: 'session.event.text_delta', payload: { text: 'a' }, at: 10 },
        { name: 'session.event.text_delta', payload: { text: 'b' }, at: 10 },
      ],
    })
    expect(textOf(batch)).toBe(textOf(one))
  })
})

// The gateway's time prefix on a stored user message ("[<iso> <day> <tz>]\n").
const PREFIX = '[2026-11-10T11:23+07:00 Tue Asia/Ho_Chi_Minh]\n'

function task(
  state: TranscriptState,
  name: string,
  payload: Record<string, unknown>,
  at = 50,
): TranscriptState {
  return reduce(state, { type: 'event', name, payload, at })
}

function accepted(state: TranscriptState, text: string, runId: string, at: number) {
  const s = reduce(state, { type: 'optimisticSend', text, idempotencyKey: runId, at })
  return reduce(s, { type: 'sendAccepted', idempotencyKey: runId, at, runId })
}

const running = (runId: string) => ({
  run_id: runId,
  task_id: runId,
  text: '',
  through_stream_seq: 1,
})

describe('a terminal frame is final', () => {
  const answered = [row('u', 'user', `${PREFIX}go`, 100), row('a', 'assistant', 'DONE', 101)]

  it('done, then a reload whose snapshot still names the run: stays idle', () => {
    let state = accepted(initialTranscript(), 'go', 'r1', 100)
    state = frame(state, 'text_delta', { text: 'DONE' }, 110)
    state = frame(state, 'done', { run_id: 'r1', task_id: 'r1', text: 'DONE', state: 'done' }, 120)
    expect(isRunning(state)).toBe(false)
    // The ledger had not settled r1 when chat.history answered.
    state = page(state, { messages: answered, in_flight_run: running('r1') }, 121)
    expect(isRunning(state)).toBe(false)
    expect(state.turns).toHaveLength(1)
    expect(state.turns[0]).toMatchObject({ status: 'done', userMessage: { text: 'go' } })
    // The subscribe answer can carry the same stale snapshot.
    state = reduce(state, { type: 'runState', inFlight: running('r1'), issuedAt: 122 })
    expect(isRunning(state)).toBe(false)
  })

  it('a stale snapshot first, then done: ends once and stays idle', () => {
    let state = accepted(initialTranscript(), 'go', 'r1', 100)
    state = page(
      state,
      { messages: [row('u', 'user', `${PREFIX}go`, 100)], in_flight_run: running('r1') },
      105,
    )
    expect(isRunning(state)).toBe(true)
    state = frame(state, 'done', { run_id: 'r1', text: 'DONE', state: 'done' }, 120)
    state = page(state, { messages: answered, in_flight_run: running('r1') }, 121)
    expect(isRunning(state)).toBe(false)
    expect(state.turns).toHaveLength(1)
  })

  it('done arriving before the first page (opened mid-run) is not undone by its snapshot', () => {
    let state = frame(initialTranscript(), 'done', { run_id: 'r1', text: 'DONE' }, 120)
    state = page(state, { messages: answered, in_flight_run: running('r1') }, 100)
    expect(isRunning(state)).toBe(false)
    expect(state.turns.map((t) => t.userMessage?.text)).toEqual(['go'])
  })

  it('task.succeeded ends the live turn of its run, in either order with done', () => {
    let state = accepted(initialTranscript(), 'go', 'r1', 100)
    state = frame(state, 'text_delta', { text: 'DONE' }, 110)
    state = task(state, 'task.succeeded', { task_id: 'r1' }, 115)
    expect(isRunning(state)).toBe(false)
    expect(state.turns.at(-1)).toMatchObject({ status: 'done' })
    state = frame(state, 'done', { run_id: 'r1', text: 'DONE' }, 120)
    expect(state.turns).toHaveLength(1)
    state = page(state, { messages: answered, in_flight_run: running('r1') }, 121)
    expect(isRunning(state)).toBe(false)
  })

  it('task.succeeded of another run leaves the next turn running', () => {
    let state = accepted(initialTranscript(), 'first', 'r1', 100)
    state = frame(state, 'done', { run_id: 'r1', text: 'one' }, 110)
    // The queue sends the next message before r1's lifecycle frame lands.
    state = reduce(state, { type: 'optimisticSend', text: 'next', idempotencyKey: 'k2', at: 111 })
    state = task(state, 'task.succeeded', { task_id: 'r1' }, 112)
    state = task(state, 'task.failed', { task_id: 'r1', error: 'late' }, 113)
    expect(isRunning(state)).toBe(true)
    state = reduce(state, { type: 'sendAccepted', idempotencyKey: 'k2', at: 114, runId: 'r2' })
    state = task(state, 'task.succeeded', { task_id: 'r1' }, 115)
    expect(isRunning(state)).toBe(true)
    state = task(state, 'task.succeeded', { task_id: 'r2' }, 120)
    expect(isRunning(state)).toBe(false)
  })

  it('a turn adopted from a snapshot learns its run and ends on task.succeeded', () => {
    let state = page(
      initialTranscript(),
      { messages: [row('u', 'user', `${PREFIX}go`, 100)], in_flight_run: running('r1') },
      105,
    )
    expect(isRunning(state)).toBe(true)
    expect(state.turns.map((t) => t.userMessage?.text)).toEqual(['go'])
    state = task(state, 'task.succeeded', { task_id: 'r1' }, 120)
    expect(isRunning(state)).toBe(false)
  })

  it('restart recovery: a resumed run ends, the reload names it, one user bubble remains', () => {
    // Sent before the gateway went down; its terminal frame was lost.
    let state = page(initialTranscript(), { messages: [row('u0', 'user', `${PREFIX}hi`, 1)] })
    state = accepted(state, 'count', 'r1', 100)
    state = frame(state, 'text_delta', { text: '1 tide' }, 110)
    // Back up: the stream starts over and the subscribe answer reports the
    // recovered run.
    state = reduce(state, { type: 'streamReset' })
    const recovered = [
      row('u0', 'user', `${PREFIX}hi`, 1),
      row('u1', 'user', `${PREFIX}count`, 100),
    ]
    state = page(state, { messages: recovered, in_flight_run: running('r1') }, 200)
    expect(isRunning(state)).toBe(true)
    state = frame(state, 'done', { run_id: 'r1', text: '120 tide', stream_seq: 4 }, 300)
    state = page(
      state,
      {
        messages: [...recovered, row('a1', 'assistant', '120 tide', 300)],
        in_flight_run: running('r1'),
      },
      301,
    )
    expect(isRunning(state)).toBe(false)
    expect(state.turns.map((t) => t.userMessage?.text)).toEqual(['hi', 'count'])
    // The next message is sent, not queued behind a phantom run.
    state = accepted(state, 'PINEAPPLE', 'r2', 400)
    expect(state.turns.map((t) => t.status)).toEqual(['done', 'done', 'live'])
  })
})

describe('user rows carry the gateway time prefix', () => {
  it('shows history user text without the prefix', () => {
    const state = page(initialTranscript(), {
      messages: [row('u', 'user', `${PREFIX}hello`, 1), row('a', 'assistant', 'hi', 2)],
    })
    expect(state.turns[0]!.userMessage!.text).toBe('hello')
  })

  it('matches the typed message to its prefixed row: one bubble', () => {
    let state = accepted(initialTranscript(), 'Hello, this turn should fail.', 'r1', 100)
    state = frame(state, 'error', { run_id: 'r1', message: 'The task failed.' }, 110)
    state = page(state, {
      messages: [
        row('u', 'user', `${PREFIX}Hello, this turn should fail.`, 100),
        row('e', 'system', 'Error: Request error: All connection attempts failed', 101),
      ],
      in_flight_run: null,
    })
    expect(state.turns).toHaveLength(1)
    expect(state.turns[0]).toMatchObject({
      status: 'failed',
      userMessage: { text: 'Hello, this turn should fail.' },
    })
  })

  it('a failed turn keeps its reason after a reload (fresh view)', () => {
    const state = page(initialTranscript(), {
      messages: [
        row('u', 'user', `${PREFIX}Hello, this turn should fail.`, 100),
        row('e', 'system', 'Error: Request error: All connection attempts failed', 101),
      ],
      in_flight_run: null,
    })
    expect(state.turns).toHaveLength(1)
    expect(state.turns[0]).toMatchObject({
      status: 'failed',
      failure: 'Request error: All connection attempts failed',
    })
  })

  it('a live failure is kept for a prefixed row history holds no error for', () => {
    let state = accepted(initialTranscript(), 'go', 'r1', 100)
    state = frame(state, 'error', { message: 'Timed out', code: 'timeout' }, 110)
    state = page(state, { messages: [row('u', 'user', `${PREFIX}go`, 100)], in_flight_run: null })
    expect(state.turns).toHaveLength(1)
    expect(state.turns[0]).toMatchObject({ status: 'failed', failure: 'Timed out' })
  })

  it('other system rows do not fail a turn', () => {
    const state = page(initialTranscript(), {
      messages: [
        row('u', 'user', 'go', 1),
        row('a', 'assistant', 'ok', 2),
        row('s', 'system', 'Errors are listed in the log.', 3),
      ],
    })
    expect(state.turns[0]).toMatchObject({ status: 'done' })
  })
})

describe('a second stop while stopping', () => {
  it('marks a forced stop only behind a pending one, and clears it alone on failure', () => {
    let state = sent(initialTranscript(), 'go', 'k', 1)
    expect(reduce(state, { type: 'abortRequested', force: true })).toBe(state)
    state = reduce(state, { type: 'abortRequested' })
    state = reduce(state, { type: 'abortRequested', force: true })
    expect(state.turns.at(-1)).toMatchObject({ abortRequested: true, forceStop: true })
    const failedForce = reduce(state, { type: 'abortFailed', force: true })
    expect(failedForce.turns.at(-1)).toMatchObject({ abortRequested: true })
    expect(failedForce.turns.at(-1)!.forceStop).toBeUndefined()
    const failedBoth = reduce(state, { type: 'abortFailed' })
    expect(failedBoth.turns.at(-1)!.abortRequested).toBeUndefined()
    expect(failedBoth.turns.at(-1)!.forceStop).toBeUndefined()
  })
})
