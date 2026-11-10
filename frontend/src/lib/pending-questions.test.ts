import { afterEach, describe, expect, it } from 'vitest'
import {
  bindPendingQuestions,
  loadPendingQuestions,
  usePendingQuestions,
  type QuestionsRpc,
} from './pending-questions'

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

describe('loadPendingQuestions', () => {
  const rpcAnswering = (answer: unknown) => ({
    calls: [] as string[],
    async call<T>(method: string): Promise<T> {
      this.calls.push(method)
      if (answer instanceof Error) throw answer
      return answer as T
    },
  })

  it('restores the questions the gateway reports, so the inbox lists them after a reload', async () => {
    const rpc = rpcAnswering({
      questions: [
        {
          key: 'agent:main:webchat:a',
          questions: PRESENTED.questions,
          toolUseId: 'tu-9',
          askedAt: 10,
        },
        { key: 'agent:main:webchat:bad', questions: [{ question: 'no options' }] },
      ],
    })
    await loadPendingQuestions(rpc, { now: () => 100 })
    expect(rpc.calls).toEqual(['sessions.questions'])
    const items = usePendingQuestions.getState().items
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({
      id: 'question:agent:main:webchat:a',
      sessionKey: 'agent:main:webchat:a',
      toolUseId: 'tu-9',
      presentedAt: 10,
    })
    expect(items[0]!.questions[0]!.question).toBe('Which branch?')
  })

  it('replaces what was known before the call and keeps a question seen live since', async () => {
    const rpc = new FakeRpc()
    let clock = 5
    const off = bindPendingQuestions(rpc, { now: () => clock })
    rpc.emit('session.event.tool_result', askResult('agent:main:webchat:answered-elsewhere'))
    clock = 50
    const pending = loadPendingQuestions(
      {
        call: async <T>() => {
          // Arrives live while the list is on its way.
          clock = 60
          rpc.emit('session.event.tool_result', askResult('agent:main:webchat:new'))
          return { questions: [] } as T
        },
      },
      { now: () => clock },
    )
    await pending
    expect(usePendingQuestions.getState().items.map((q) => q.sessionKey)).toEqual([
      'agent:main:webchat:new',
    ])
    off()
  })

  it('leaves the list alone when the gateway has no such method', async () => {
    usePendingQuestions.getState().present({
      id: 'question:k',
      sessionKey: 'k',
      questions: [],
      toolUseId: null,
      presentedAt: 1,
    })
    await loadPendingQuestions(rpcAnswering(new Error('METHOD_NOT_FOUND')))
    expect(usePendingQuestions.getState().items).toHaveLength(1)
  })
})
