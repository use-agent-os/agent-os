import { afterEach, describe, expect, it } from 'vitest'
import { bindPendingQuestions, usePendingQuestions, type QuestionsRpc } from './pending-questions'

type Handler = (...args: unknown[]) => void

class FakeRpc implements QuestionsRpc {
  handlers = new Map<string, Set<Handler>>()
  on(event: string, handler: Handler): () => void {
    if (!this.handlers.has(event)) this.handlers.set(event, new Set())
    this.handlers.get(event)!.add(handler)
    return () => this.handlers.get(event)?.delete(handler)
  }
  emit(event: string, payload: unknown, meta: unknown = {}): void {
    this.handlers.get(event)?.forEach((h) => h(payload, meta))
  }
}

const PRESENTED = {
  status: 'question_presented',
  questions: [
    {
      question: 'Which branch?',
      header: 'Branch',
      options: [{ label: 'main' }, { label: 'dev' }],
      multi_select: false,
    },
  ],
}

function askResult(sessionKey: string, result: unknown = PRESENTED) {
  return {
    tool_name: 'ask_user',
    tool_use_id: 'tu-1',
    result: JSON.stringify(result),
    session_key: sessionKey,
  }
}

afterEach(() => usePendingQuestions.getState().reset())

describe('bindPendingQuestions', () => {
  it('lists a presented question for its session', () => {
    const rpc = new FakeRpc()
    const off = bindPendingQuestions(rpc, { now: () => 42 })
    rpc.emit('session.event.tool_result', askResult('agent:main:webchat:a'))
    const items = usePendingQuestions.getState().items
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({
      id: 'question:agent:main:webchat:a',
      sessionKey: 'agent:main:webchat:a',
      toolUseId: 'tu-1',
      presentedAt: 42,
    })
    expect(items[0]!.questions[0]!.options.map((o) => o.label)).toEqual(['main', 'dev'])
    off()
  })

  it('keeps one entry per session (a re-ask replaces it)', () => {
    const rpc = new FakeRpc()
    const off = bindPendingQuestions(rpc)
    rpc.emit('session.event.tool_result', askResult('s1'))
    rpc.emit('session.event.tool_result', askResult('s1'))
    rpc.emit('session.event.tool_result', askResult('s2'))
    expect(usePendingQuestions.getState().items.map((q) => q.sessionKey)).toEqual(['s1', 's2'])
    off()
  })

  it('clears a session when its next turn is queued (the answer)', () => {
    const rpc = new FakeRpc()
    const off = bindPendingQuestions(rpc)
    rpc.emit('session.event.tool_result', askResult('s1'))
    rpc.emit('session.event.tool_result', askResult('s2'))
    rpc.emit('task.queued', { task_id: 't', session_key: 's1' })
    expect(usePendingQuestions.getState().items.map((q) => q.sessionKey)).toEqual(['s2'])
    off()
  })

  it('ignores other tools, errors, replays and frames without a session', () => {
    const rpc = new FakeRpc()
    const off = bindPendingQuestions(rpc)
    rpc.emit('session.event.tool_result', { ...askResult('s1'), tool_name: 'bash' })
    rpc.emit('session.event.tool_result', { ...askResult('s1'), is_error: true })
    rpc.emit('session.event.tool_result', askResult('s1'), { replayed: true })
    rpc.emit('session.event.tool_result', { ...askResult(''), session_key: undefined })
    expect(usePendingQuestions.getState().items).toEqual([])
    off()
  })

  it('stops listening once disposed', () => {
    const rpc = new FakeRpc()
    bindPendingQuestions(rpc)()
    rpc.emit('session.event.tool_result', askResult('s1'))
    expect(usePendingQuestions.getState().items).toEqual([])
  })

  it('drops questions of sessions missing from a list read after them', () => {
    const rpc = new FakeRpc()
    let now = 1_000
    const off = bindPendingQuestions(rpc, { now: () => now })
    rpc.emit('session.event.tool_result', askResult('deleted'))
    rpc.emit('session.event.tool_result', askResult('kept'))
    now = 5_000
    rpc.emit('session.event.tool_result', askResult('newer'))
    const before = usePendingQuestions.getState()
    // A list read at t=2000 lists `kept` only; `newer` arrived after it.
    before.retainSessions(new Set(['kept']), 2_000)
    expect(usePendingQuestions.getState().items.map((q) => q.sessionKey)).toEqual(['kept', 'newer'])
    // Nothing to drop: the state object is left alone.
    const same = usePendingQuestions.getState()
    same.retainSessions(new Set(['kept']), 2_000)
    expect(usePendingQuestions.getState()).toBe(same)
    off()
  })
})
