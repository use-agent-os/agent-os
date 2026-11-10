import { describe, expect, it } from 'vitest'
import { STREAMS, type StreamFixture } from './__fixtures__/streams'
import { comparableTurn, foldHistory, initialTranscript, isRunning, reduce } from './reducer'
import type { HistoryPage, TranscriptState, Turn } from './types'

function foldLive(
  fx: StreamFixture,
  start: TranscriptState = initialTranscript(),
): TranscriptState {
  let state = start
  let at = 1000
  if (fx.send !== null) {
    state = reduce(state, { type: 'optimisticSend', text: fx.send, idempotencyKey: 'k1', at })
    state = reduce(state, { type: 'sendAccepted', idempotencyKey: 'k1' })
  }
  for (const e of fx.events)
    state = reduce(state, { type: 'event', name: e.name, payload: e.payload, at: (at += 500) })
  return state
}

function foldPage(page: HistoryPage): TranscriptState {
  return reduce(initialTranscript(), { type: 'historyPage', page, mode: 'replace' })
}

const byName = (name: string) => STREAMS.find((s) => s.name.startsWith(name))!

describe('history pages', () => {
  it('folds user and assistant rows into turns', () => {
    const turns = foldPage(byName('router_decision').history!).turns
    expect(turns).toHaveLength(1)
    const [turn] = turns as [Turn]
    expect(turn.userMessage?.text).toBe('chart btc')
    expect(turn.items.map((i) => i.kind)).toEqual([
      'routerDecision',
      'thinkingRef',
      'toolCall',
      'toolCall',
      'text',
      'card',
    ])
    expect(turn.usage).toEqual({
      model: 'anthropic/claude-sonnet',
      inputTokens: 1200,
      outputTokens: 340,
      costUsd: 0.0042,
    })
  })

  it('pages older entries in without duplicates', () => {
    const recent: HistoryPage = {
      messages: [
        { id: 'u2', message_id: 'u2', role: 'user', text: 'second', timestamp: 20 },
        { id: 'a2', message_id: 'a2', role: 'assistant', text: 'two', timestamp: 21 },
      ],
      has_more: true,
      oldest_cursor: '20|u2',
    }
    const older: HistoryPage = {
      messages: [
        { id: 'u1', message_id: 'u1', role: 'user', text: 'first', timestamp: 10 },
        { id: 'a1', message_id: 'a1', role: 'assistant', text: 'one', timestamp: 11 },
        { id: 'u2', message_id: 'u2', role: 'user', text: 'second', timestamp: 20 },
      ],
      has_more: false,
      oldest_cursor: '10|u1',
    }
    let state = foldPage(recent)
    expect(state.hasMore).toBe(true)
    state = reduce(state, { type: 'historyPage', page: older, mode: 'older' })
    expect(state.turns.map((t) => t.userMessage?.text)).toEqual(['first', 'second'])
    expect(state.rows).toHaveLength(4)
    expect(state.hasMore).toBe(false)
    expect(state.oldestCursor).toBe('10|u1')
  })

  it('keeps the older pages the reader loaded when a turn end refreshes the window', () => {
    const row = (n: number, role: 'user' | 'assistant') => ({
      id: `${role[0]}${n}`,
      message_id: `${role[0]}${n}`,
      role,
      text: `${role} ${n}`,
      timestamp: n * 10 + (role === 'user' ? 0 : 1),
    })
    // Loaded: an older page (turns 1-2) above the latest one (turns 3-4).
    let state = foldPage({
      messages: [row(3, 'user'), row(3, 'assistant'), row(4, 'user'), row(4, 'assistant')],
      has_more: true,
      oldest_cursor: '30|u3',
    })
    state = reduce(state, {
      type: 'historyPage',
      mode: 'older',
      page: {
        messages: [row(1, 'user'), row(1, 'assistant'), row(2, 'user'), row(2, 'assistant')],
        has_more: true,
        oldest_cursor: '10|u1',
      },
    })
    // Turn 5 ended; the latest window slid past turn 3.
    const latest: HistoryPage = {
      messages: [row(4, 'user'), row(4, 'assistant'), row(5, 'user'), row(5, 'assistant')],
      has_more: true,
      oldest_cursor: '40|u4',
    }
    const refreshed = reduce(state, { type: 'historyPage', page: latest, mode: 'refresh' })
    expect(refreshed.turns.map((t) => t.userMessage?.text)).toEqual([
      'user 1',
      'user 2',
      'user 3',
      'user 4',
      'user 5',
    ])
    expect(refreshed.hasMore).toBe(true)
    expect(refreshed.oldestCursor).toBe('10|u1')
    // A plain replace (opening the session) still takes the latest window only.
    const replaced = reduce(state, { type: 'historyPage', page: latest, mode: 'replace' })
    expect(replaced.turns.map((t) => t.userMessage?.text)).toEqual(['user 4', 'user 5'])
    expect(replaced.oldestCursor).toBe('40|u4')
    // A window that does not overlap what is loaded replaces it.
    const far = reduce(state, {
      type: 'historyPage',
      mode: 'refresh',
      page: {
        messages: [row(9, 'user'), row(9, 'assistant')],
        has_more: true,
        oldest_cursor: '90|u9',
      },
    })
    expect(far.turns.map((t) => t.userMessage?.text)).toEqual(['user 9'])
    expect(far.oldestCursor).toBe('90|u9')
  })

  it('places compaction summaries by time and folds cron rows as standalone entries', () => {
    const turns = foldHistory(
      [
        { id: 'u1', role: 'user', text: 'a', timestamp: 100 },
        { id: 'c1', role: 'assistant', text: 'report', timestamp: 300, provenance_kind: 'cron' },
      ],
      [{ id: 's', summary_text: 'sum', created_at: 200 }],
    )
    expect(turns.map((t) => t.items[0]?.kind ?? 'user')).toEqual([
      'user',
      'compaction',
      'cronResult',
    ])
  })
})

describe('live events', () => {
  it.each(STREAMS.map((s) => [s.name, s] as const))('%s', (_name, fx) => {
    const state = foldLive(fx)
    expect(state.turns.length).toBeGreaterThan(0 + (fx.name.startsWith('task_group') ? -1 : 0))
    expect(state.notices.length).toBe(fx.name.startsWith('warning') ? 1 : 0)
  })

  it('streams text into one growing text item', () => {
    const turn = foldLive(byName('text_delta')).turns[0]!
    expect(turn.items.filter((i) => i.kind === 'text')).toEqual([
      expect.objectContaining({ kind: 'text', text: 'Hi there!' }),
    ])
    expect(turn.status).toBe('done')
  })

  it('settles tool calls by id with state and duration', () => {
    const turn = foldLive(byName('router_decision')).turns[0]!
    const tools = turn.items.filter((i) => i.kind === 'toolCall')
    expect(tools.map((t) => [t.name, t.state])).toEqual([
      ['web_search', 'done'],
      ['bash', 'done'],
    ])
    expect(tools.every((t) => t.endedAt! > t.startedAt!)).toBe(true)
  })

  it('keeps warnings out of the turn as notices', () => {
    const state = foldLive(byName('warning'))
    expect(state.notices.map((n) => n.text)).toEqual(['The provider is slow'])
    expect(state.turns[0]!.items.map((i) => i.kind)).toEqual(['text'])
  })

  it('records steering and subagent completions in order', () => {
    expect(foldLive(byName('steering')).turns[0]!.items.map((i) => i.kind)).toEqual([
      'text',
      'steering',
      'text',
    ])
    expect(foldLive(byName('subagent_completion')).turns[0]!.items.map((i) => i.kind)).toEqual([
      'text',
      'subagentCompletion',
    ])
  })

  it('turns an ask_user result into an ask card', () => {
    const kinds = foldLive(byName('tool_result: ask_user')).turns[0]!.items.map((i) => i.kind)
    expect(kinds).toEqual(['toolCall', 'ask'])
  })

  it('tracks subagent task groups as running work', () => {
    const fx = byName('task_group')
    let state = initialTranscript()
    state = reduce(state, { type: 'event', ...fx.events[0]!, at: 1 })
    expect(isRunning(state)).toBe(true)
    state = reduce(state, { type: 'event', ...fx.events[1]!, at: 2 })
    expect(isRunning(state)).toBe(false)
  })

  it('ignores frames from an older epoch and resyncs on an epoch change', () => {
    let state = reduce(initialTranscript(), {
      type: 'event',
      name: 'session.epoch_changed',
      payload: { epoch: 2 },
      at: 1,
    })
    expect(state.needsResync).toBe(true)
    const before = state
    state = reduce(state, {
      type: 'event',
      name: 'session.event.text_delta',
      payload: { text: 'x', epoch: 1 },
      at: 2,
    })
    expect(state).toBe(before)
  })

  it('marks a not-sent message and an abort request', () => {
    let state = reduce(initialTranscript(), {
      type: 'optimisticSend',
      text: 'hi',
      idempotencyKey: 'k',
      at: 1,
    })
    expect(state.turns[0]!.userMessage?.delivery).toBe('sending')
    state = reduce(state, { type: 'abortRequested' })
    expect(state.turns[0]!.abortRequested).toBe(true)
    state = reduce(state, {
      type: 'sendFailed',
      idempotencyKey: 'k',
      error: 'socket closed',
      notSent: true,
    })
    expect(state.turns[0]!.userMessage?.delivery).toBe('not_sent')
    expect(state.turns[0]!.status).toBe('failed')
    state = reduce(state, { type: 'queue', count: 2 })
    expect(state.queued).toBe(2)
  })
})

describe('live equals history', () => {
  const withHistory = STREAMS.filter((s) => s.history)

  it.each(withHistory.map((s) => [s.name, s] as const))('%s', (_name, fx) => {
    const live = foldLive(fx).turns.map(comparableTurn)
    const restored = foldPage(fx.history!).turns.map(comparableTurn)
    expect(live).toEqual(restored)
  })

  it('a reload after a turn with two tools and a chart shows the same turn', () => {
    const fx = byName('router_decision')
    const live = foldLive(fx)
    const reloaded = reduce(live, { type: 'historyPage', page: fx.history!, mode: 'replace' })
    expect(reloaded.turns).toHaveLength(1)
    expect(reloaded.turns.map(comparableTurn)).toEqual(live.turns.map(comparableTurn))
    const tools = reloaded.turns[0]!.items.filter((i) => i.kind === 'toolCall')
    expect(tools.map((t) => t.state)).toEqual(['done', 'done'])
    expect(reloaded.turns[0]!.items.some((i) => i.kind === 'card' && i.category === 'chart')).toBe(
      true,
    )
  })

  it('an aborted turn keeps its partial reply and aborted state when reopened', () => {
    const fx = byName('done (aborted)')
    const reopened = foldPage(fx.history!).turns[0]!
    expect(reopened.status).toBe('aborted')
    expect(reopened.items.find((i) => i.kind === 'text')).toMatchObject({
      text: 'Starting the job',
    })
    expect(reopened.items.find((i) => i.kind === 'toolCall')).toMatchObject({ state: 'failed' })
  })
})

describe('reconciliation', () => {
  it('shows a turn that completed during a disconnect exactly once, finished', () => {
    const fx = byName('router_decision')
    // Streamed up to the second tool, then the socket dropped.
    let state = reduce(initialTranscript(), {
      type: 'optimisticSend',
      text: 'chart btc',
      idempotencyKey: 'k',
      at: 1,
    })
    for (const e of fx.events.slice(0, 5)) state = reduce(state, { type: 'event', ...e, at: 2 })
    expect(state.turns.at(-1)!.status).toBe('live')
    // Reconnect: the history window now holds the finished turn.
    state = reduce(state, { type: 'historyPage', page: fx.history!, mode: 'replace' })
    expect(state.turns).toHaveLength(1)
    expect(state.turns[0]!.status).toBe('done')
    expect(state.turns[0]!.items.filter((i) => i.kind === 'text')).toHaveLength(1)
  })

  it('keeps a still-running turn live over history’s partial copy', () => {
    let state = reduce(initialTranscript(), {
      type: 'optimisticSend',
      text: 'go',
      idempotencyKey: 'k',
      at: 1,
    })
    state = reduce(state, {
      type: 'event',
      name: 'session.event.text_delta',
      payload: { text: 'work' },
      at: 2,
    })
    state = reduce(state, {
      type: 'historyPage',
      mode: 'replace',
      page: {
        messages: [{ id: 'u', role: 'user', text: 'go', timestamp: 1 }],
        in_flight_run: { text: 'work' },
      },
    })
    expect(state.turns).toHaveLength(1)
    expect(state.turns[0]!.status).toBe('live')
  })

  it('adopts an in-flight run when the session is opened mid-turn', () => {
    const state = foldPage({
      messages: [{ id: 'u', role: 'user', text: 'go', timestamp: 1 }],
      in_flight_run: { text: 'halfway', started_at: 1 },
    })
    expect(state.turns).toHaveLength(1)
    expect(state.turns[0]).toMatchObject({ status: 'live' })
    expect(state.turns[0]!.items).toEqual([
      expect.objectContaining({ kind: 'text', text: 'halfway' }),
    ])
  })

  it('asks for a full reload when a delta cannot stand in for one', () => {
    const state = reduce(foldPage({ messages: [] }), {
      type: 'historyPage',
      mode: 'delta',
      page: { kind: 'delta', has_more: true, messages: [] },
    })
    expect(state.needsResync).toBe(true)
  })

  it('merges a usable delta by entry id', () => {
    let state = foldPage({
      messages: [{ id: 'u1', message_id: 'u1', role: 'user', text: 'a', timestamp: 1 }],
    })
    state = reduce(state, {
      type: 'historyPage',
      mode: 'delta',
      page: {
        kind: 'delta',
        has_more: false,
        messages: [
          { id: 'u1', message_id: 'u1', role: 'user', text: 'a', timestamp: 1 },
          { id: 'a1', message_id: 'a1', role: 'assistant', text: 'b', timestamp: 2 },
        ],
      },
    })
    expect(state.rows).toHaveLength(2)
    expect(state.turns).toHaveLength(1)
  })
})
