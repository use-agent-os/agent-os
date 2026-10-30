import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import {
  ApprovalMonitor,
  ELEVATED_MODE_KEY,
  ELEVATED_MODE_VERSION_KEY,
  approvalCommand,
  approvalDetail,
  approvalFromEvent,
  approvalMeta,
  approvalsResolveUrl,
  approvalsSettingsUrl,
  approvalsUrl,
  canAlwaysAllow,
  canBypass,
  isApprovalBypassMode,
  readBrowserElevated,
  saveApprovalMode,
  setBrowserElevated,
  useApprovals,
  type Approval,
  type ApprovalEventSource,
} from './approval-monitor'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
  },
}))

function okResponse(body: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    json: async () => body,
  } as Response
}

function resetStore() {
  useApprovals.setState({ pending: [], count: 0, mode: 'prompt' })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

type Handler = (...args: unknown[]) => void

// A stand-in for WsRpcClient: records subscriptions and lets a test push
// frames and connection-state transitions the way the real client emits them.
function fakeRpc(state: string = 'disconnected') {
  const handlers = new Map<string, Set<Handler>>()
  const rpc = {
    state,
    on(event: string, handler: Handler): () => void {
      if (!handlers.has(event)) handlers.set(event, new Set())
      handlers.get(event)!.add(handler)
      return () => {
        handlers.get(event)?.delete(handler)
      }
    },
    emit(event: string, ...args: unknown[]): void {
      handlers.get(event)?.forEach((h) => h(...args))
    },
    setState(next: string): void {
      rpc.state = next
      rpc.emit('_state', next)
    },
    listeners(): number {
      let n = 0
      handlers.forEach((set) => (n += set.size))
      return n
    },
  }
  return rpc
}

// Only the GET backfills (not resolve POSTs) — what the no-polling tests count.
function backfillCalls(fetchMock: ReturnType<typeof vi.fn>): number {
  return fetchMock.mock.calls.filter(([url]) => url === '/api/approvals').length
}

describe('approval-monitor service', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  // Track every monitor a test creates so a failed assertion (which skips the
  // test's own mon.stop()) can never leak a live focus/visibility listener or
  // an rpc subscription into the next test.
  const live: ApprovalMonitor[] = []
  function makeMonitor(): ApprovalMonitor {
    const mon = new ApprovalMonitor()
    live.push(mon)
    return mon
  }

  beforeEach(() => {
    vi.useFakeTimers()
    resetStore()
    localStorage.clear()
    sessionStorage.clear()
    fetchMock = vi.fn().mockResolvedValue(okResponse({ pending: [], mode: 'prompt' }))
    vi.stubGlobal('fetch', fetchMock)
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.warning).mockClear()
    vi.mocked(toast.error).mockClear()
  })

  afterEach(() => {
    for (const mon of live.splice(0)) mon.stop()
    vi.clearAllTimers()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  // Drain the microtask queue so awaited fetch / json promises settle before
  // the test asserts.
  async function flush() {
    for (let i = 0; i < 6; i++) await Promise.resolve()
  }

  describe('backfill on start + (re)connect', () => {
    it('backfills once from the root-absolute /api/approvals when started without a client', async () => {
      const mon = makeMonitor()
      mon.start()
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/approvals',
        expect.objectContaining({ cache: 'no-store' }),
      )
    })

    it('backfills at once when the client is already connected', async () => {
      const rpc = fakeRpc('connected')
      makeMonitor().start(rpc)
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)
    })

    it('backfills at once when the client does not report a state', async () => {
      const rpc = fakeRpc()
      const source: ApprovalEventSource = { on: (event, handler) => rpc.on(event, handler) }
      makeMonitor().start(source)
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)
    })

    it('waits for the connection, then does exactly one GET per transition to connected', async () => {
      const rpc = fakeRpc('disconnected')
      makeMonitor().start(rpc)
      await flush()
      expect(fetchMock).not.toHaveBeenCalled()

      rpc.setState('connecting')
      await flush()
      expect(fetchMock).not.toHaveBeenCalled()

      rpc.setState('connected')
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)

      // A drop and a reconnect: events were missed meanwhile, so one more.
      rpc.setState('disconnected')
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)
      rpc.setState('connected')
      await flush()
      expect(backfillCalls(fetchMock)).toBe(2)
    })

    it('never fetches on a timer: no GET fires long after start', async () => {
      const rpc = fakeRpc('connected')
      makeMonitor().start(rpc)
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)
      // Well past the old 1.5s floor and the 30s backoff ceiling.
      await vi.advanceTimersByTimeAsync(10 * 60_000)
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)
    })

    it('never fetches on a timer even with pending items or a failing endpoint', async () => {
      fetchMock.mockResolvedValue(okResponse({}, false, 503))
      makeMonitor().start()
      await flush()
      await vi.advanceTimersByTimeAsync(120_000)
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)

      fetchMock.mockResolvedValue(okResponse({ pending: [{ id: 'a1' }], mode: 'prompt' }))
      const rpc = fakeRpc('connected')
      const other = makeMonitor()
      other.start(rpc)
      await flush()
      await vi.advanceTimersByTimeAsync(120_000)
      await flush()
      expect(backfillCalls(fetchMock)).toBe(2)
    })

    it('start() is idempotent: a second call neither re-subscribes nor re-fetches', async () => {
      const rpc = fakeRpc('connected')
      const mon = makeMonitor()
      mon.start(rpc)
      mon.start(rpc)
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)
      expect(rpc.listeners()).toBe(3)
    })

    it('stop() unsubscribes from the client and ignores later frames + reconnects', async () => {
      const rpc = fakeRpc('connected')
      const mon = makeMonitor()
      mon.start(rpc)
      await flush()
      expect(rpc.listeners()).toBe(3)
      mon.stop()
      expect(rpc.listeners()).toBe(0)
      fetchMock.mockClear()
      rpc.emit('approval.requested', { id: 'late', namespace: 'exec' })
      rpc.setState('disconnected')
      rpc.setState('connected')
      await flush()
      expect(useApprovals.getState().pending).toEqual([])
      expect(fetchMock).not.toHaveBeenCalled()
    })
  })

  describe('pushed events', () => {
    it('approval.requested with the full item adds it verbatim', async () => {
      const rpc = fakeRpc('disconnected')
      makeMonitor().start(rpc)
      const full: Approval = {
        id: 'a1',
        namespace: 'exec',
        toolName: 'bash',
        sessionKey: 'agent:main:1',
        command: 'rm -rf /tmp/x',
        warning: 'destructive',
        argv: ['rm', '-rf', '/tmp/x'],
        mode: 'prompt',
        created_at: 1700000000,
        title: 'Run a shell command',
      }
      rpc.emit('approval.requested', { id: 'a1', namespace: 'exec', approval: full })
      const state = useApprovals.getState()
      expect(state.pending).toEqual([full])
      expect(state.count).toBe(1)
      expect(fetchMock).not.toHaveBeenCalled()
    })

    it('approval.requested without the full item builds one from the summary fields', () => {
      const rpc = fakeRpc()
      makeMonitor().start(rpc)
      rpc.emit('approval.requested', {
        id: 'p1',
        namespace: 'plugin',
        session_key: 's-9',
        created_at: 1700000001,
        expires_at: 1700000301,
        summary: 'Plugin wants network access',
        tool: 'web_fetch',
        command: 'GET https://example.invalid',
      })
      expect(useApprovals.getState().pending).toEqual([
        {
          id: 'p1',
          namespace: 'plugin',
          sessionKey: 's-9',
          toolName: 'web_fetch',
          command: 'GET https://example.invalid',
          warning: 'Plugin wants network access',
          created_at: 1700000001,
        },
      ])
      expect(useApprovals.getState().count).toBe(1)
    })

    it('a duplicate frame neither duplicates the item nor toasts again', () => {
      const rpc = fakeRpc()
      makeMonitor().start(rpc)
      rpc.emit('approval.requested', { id: 'a1', namespace: 'exec' })
      rpc.emit('approval.requested', { id: 'a1', namespace: 'exec' })
      expect(useApprovals.getState().pending).toEqual([{ id: 'a1', namespace: 'exec' }])
      expect(useApprovals.getState().count).toBe(1)
      expect(toast.warning).toHaveBeenCalledTimes(1)
      expect(toast.warning).toHaveBeenCalledWith(
        'Approval required',
        expect.objectContaining({ id: 'approval-required', duration: 2500 }),
      )
    })

    it('keeps arrival order; a full repeat refreshes in place, a bare repeat changes nothing', () => {
      const rpc = fakeRpc()
      makeMonitor().start(rpc)
      rpc.emit('approval.requested', {
        id: 'a1',
        namespace: 'exec',
        approval: { id: 'a1', namespace: 'exec', command: 'ls', warning: 'from queue' },
      })
      rpc.emit('approval.requested', { id: 'a2', namespace: 'acp' })
      rpc.emit('approval.requested', { id: 'a3', namespace: 'acp_elicitation' })
      // A bare summary for a known id must not clobber the richer item.
      rpc.emit('approval.requested', { id: 'a1', namespace: 'exec', summary: 'short' })
      expect(useApprovals.getState().pending.map((p) => p.id)).toEqual(['a1', 'a2', 'a3'])
      expect(useApprovals.getState().pending[0]!.warning).toBe('from queue')
      // A full item for a known id replaces it without moving it.
      rpc.emit('approval.requested', {
        id: 'a2',
        namespace: 'acp',
        approval: { id: 'a2', namespace: 'acp', toolName: 'edit' },
      })
      const pending = useApprovals.getState().pending
      expect(pending.map((p) => p.id)).toEqual(['a1', 'a2', 'a3'])
      expect(pending[1]).toEqual({ id: 'a2', namespace: 'acp', toolName: 'edit' })
      expect(toast.warning).toHaveBeenCalledTimes(3)
    })

    it('approval.resolved removes the item and updates the count', () => {
      const rpc = fakeRpc()
      makeMonitor().start(rpc)
      rpc.emit('approval.requested', { id: 'a1', namespace: 'exec' })
      rpc.emit('approval.requested', { id: 'a2', namespace: 'plugin' })
      expect(useApprovals.getState().count).toBe(2)
      rpc.emit('approval.resolved', {
        id: 'a1',
        namespace: 'exec',
        decision: 'approved',
        resolved_by: 'operator',
        resolved_at: 1700000002,
      })
      expect(useApprovals.getState().pending).toEqual([{ id: 'a2', namespace: 'plugin' }])
      expect(useApprovals.getState().count).toBe(1)
      rpc.emit('approval.resolved', { id: 'a2', namespace: 'plugin', decision: 'expired' })
      expect(useApprovals.getState().pending).toEqual([])
      expect(useApprovals.getState().count).toBe(0)
    })

    it('ignores a resolved frame for an unknown id and frames without an id', () => {
      const rpc = fakeRpc()
      makeMonitor().start(rpc)
      rpc.emit('approval.requested', { id: 'a1', namespace: 'exec' })
      rpc.emit('approval.resolved', { id: 'nope', namespace: 'exec', decision: 'denied' })
      rpc.emit('approval.requested', { namespace: 'exec' })
      rpc.emit('approval.requested', null)
      rpc.emit('approval.resolved', { namespace: 'exec', decision: 'denied' })
      expect(useApprovals.getState().pending).toEqual([{ id: 'a1', namespace: 'exec' }])
      expect(toast.warning).toHaveBeenCalledTimes(1)
    })
  })

  describe('backfill vs. events', () => {
    it('a backfill replaces pending wholesale', async () => {
      const rpc = fakeRpc()
      makeMonitor().start(rpc)
      rpc.emit('approval.requested', { id: 'stale', namespace: 'exec' })
      fetchMock.mockResolvedValue(
        okResponse({ pending: [{ id: 'fresh', namespace: 'exec' }], mode: 'prompt' }),
      )
      rpc.setState('connected')
      await flush()
      expect(useApprovals.getState().pending).toEqual([{ id: 'fresh', namespace: 'exec' }])
    })

    it('an item requested while a backfill is in flight survives the older snapshot', async () => {
      const gate = deferred<Response>()
      fetchMock.mockReturnValueOnce(gate.promise)
      const rpc = fakeRpc()
      makeMonitor().start(rpc)
      rpc.setState('connected')
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)

      rpc.emit('approval.requested', { id: 'new', namespace: 'acp' })
      expect(useApprovals.getState().pending.map((p) => p.id)).toEqual(['new'])

      // The snapshot was taken before 'new' was queued.
      gate.resolve(okResponse({ pending: [{ id: 'old', namespace: 'exec' }], mode: 'prompt' }))
      await flush()
      expect(useApprovals.getState().pending.map((p) => p.id)).toEqual(['old', 'new'])
      expect(useApprovals.getState().count).toBe(2)
    })

    it('an item resolved while a backfill is in flight is not brought back by it', async () => {
      const gate = deferred<Response>()
      fetchMock.mockReturnValueOnce(gate.promise)
      const rpc = fakeRpc()
      makeMonitor().start(rpc)
      rpc.emit('approval.requested', { id: 'a1', namespace: 'exec' })
      rpc.setState('connected')
      await flush()

      rpc.emit('approval.requested', { id: 'a2', namespace: 'exec' })
      rpc.emit('approval.resolved', { id: 'a1', namespace: 'exec', decision: 'denied' })
      rpc.emit('approval.resolved', { id: 'a2', namespace: 'exec', decision: 'approved' })

      gate.resolve(
        okResponse({
          pending: [
            { id: 'a1', namespace: 'exec' },
            { id: 'a2', namespace: 'exec' },
            { id: 'a3', namespace: 'plugin' },
          ],
          mode: 'prompt',
        }),
      )
      await flush()
      expect(useApprovals.getState().pending.map((p) => p.id)).toEqual(['a3'])
    })

    it('the full item from a snapshot wins over the summary an event left behind', async () => {
      const gate = deferred<Response>()
      fetchMock.mockReturnValueOnce(gate.promise)
      const rpc = fakeRpc()
      makeMonitor().start(rpc)
      rpc.setState('connected')
      await flush()
      rpc.emit('approval.requested', { id: 'a1', namespace: 'exec', summary: 'short' })
      gate.resolve(
        okResponse({
          pending: [{ id: 'a1', namespace: 'exec', warning: 'full text', command: 'ls' }],
          mode: 'prompt',
        }),
      )
      await flush()
      expect(useApprovals.getState().pending).toEqual([
        { id: 'a1', namespace: 'exec', warning: 'full text', command: 'ls' },
      ])
    })

    it('a backfill asked for while one is in flight runs once more after it', async () => {
      const gate = deferred<Response>()
      fetchMock
        .mockReturnValueOnce(gate.promise)
        .mockResolvedValue(okResponse({ pending: [{ id: 'b' }], mode: 'auto-deny' }))
      const mon = makeMonitor()
      mon.start()
      await flush()
      let settled = false
      const asked = mon.pollNow().then(() => {
        settled = true
      })
      const askedAgain = mon.pollNow()
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)
      expect(settled).toBe(false)

      gate.resolve(okResponse({ pending: [{ id: 'a' }], mode: 'prompt' }))
      await asked
      await askedAgain
      // Two callers during one flight coalesce into a single extra fetch.
      expect(backfillCalls(fetchMock)).toBe(2)
      expect(useApprovals.getState().pending).toEqual([{ id: 'b' }])
      expect(useApprovals.getState().mode).toBe('auto-deny')
    })

    it('a backfill that lands after stop() does not repopulate the cleared store', async () => {
      const gate = deferred<Response>()
      fetchMock.mockReturnValueOnce(gate.promise)
      const mon = makeMonitor()
      mon.start()
      await flush()
      mon.stop()
      gate.resolve(okResponse({ pending: [{ id: 'ghost' }], mode: 'prompt' }))
      await flush()
      expect(useApprovals.getState().pending).toEqual([])
      expect(useApprovals.getState().count).toBe(0)
    })
  })

  describe('store updates', () => {
    it('publishes pending + count + mode to useApprovals on a successful backfill', async () => {
      const pending: Approval[] = [
        { id: 'a1', namespace: 'exec', command: 'rm -rf /' },
        { id: 'a2', namespace: 'plugin' },
      ]
      fetchMock.mockResolvedValue(okResponse({ pending, mode: 'auto-approve' }))
      const mon = makeMonitor()
      mon.start()
      await flush()
      const state = useApprovals.getState()
      expect(state.count).toBe(2)
      expect(state.pending).toEqual(pending)
      expect(state.mode).toBe('auto-approve')
    })

    it('zeroes the badge but PRESERVES pending on a non-ok response (open prompt survives)', async () => {
      // approval_monitor.js:71-74 — a failed poll only calls _setBadge(0); it
      // never touches the open modal. So the badge count drops to 0 while the
      // pending item backing an open prompt stays put.
      useApprovals.getState().setFromPoll([{ id: 'x' }], 'prompt')
      fetchMock.mockResolvedValue(okResponse({}, false, 503))
      const mon = makeMonitor()
      mon.start()
      await flush()
      expect(useApprovals.getState().count).toBe(0)
      expect(useApprovals.getState().pending).toEqual([{ id: 'x' }])
    })

    it('zeroes the badge but PRESERVES pending when fetch throws', async () => {
      // approval_monitor.js:92-97 — the catch branch mirrors the non-ok branch.
      useApprovals.getState().setFromPoll([{ id: 'x' }], 'prompt')
      fetchMock.mockRejectedValue(new Error('offline'))
      const mon = makeMonitor()
      mon.start()
      await flush()
      expect(useApprovals.getState().count).toBe(0)
      expect(useApprovals.getState().pending).toEqual([{ id: 'x' }])
    })

    it('stop() clears the store', () => {
      useApprovals.getState().setFromPoll([{ id: 'x' }], 'prompt')
      const mon = makeMonitor()
      mon.start()
      mon.stop()
      expect(useApprovals.getState().count).toBe(0)
      expect(useApprovals.getState().pending).toEqual([])
    })

    it('zeroBadge() zeroes the count but leaves pending intact', () => {
      useApprovals.getState().setFromPoll([{ id: 'x' }, { id: 'y' }], 'prompt')
      useApprovals.getState().zeroBadge()
      expect(useApprovals.getState().count).toBe(0)
      expect(useApprovals.getState().pending).toEqual([{ id: 'x' }, { id: 'y' }])
    })

    it('upsert() appends new ids and replaces known ones in place; remove() drops by id', () => {
      const store = useApprovals.getState()
      store.upsert({ id: 'x', command: 'one' })
      store.upsert({ id: 'y' })
      store.upsert({ id: 'x', command: 'two' })
      expect(useApprovals.getState().pending).toEqual([{ id: 'x', command: 'two' }, { id: 'y' }])
      expect(useApprovals.getState().count).toBe(2)
      store.remove('x')
      expect(useApprovals.getState().pending).toEqual([{ id: 'y' }])
      expect(useApprovals.getState().count).toBe(1)
      const before = useApprovals.getState()
      store.remove('missing')
      expect(useApprovals.getState()).toBe(before)
    })
  })

  describe('toast on new pending', () => {
    it('toasts when a backfill brings a new id, and not again when it only confirms it', async () => {
      fetchMock.mockResolvedValue(
        okResponse({ pending: [{ id: 'a1', namespace: 'exec' }], mode: 'prompt' }),
      )
      const mon = makeMonitor()
      mon.start()
      await flush()
      expect(toast.warning).toHaveBeenCalledWith(
        'Approval required',
        expect.objectContaining({ id: 'approval-required', duration: 2500 }),
      )
      expect(toast.warning).toHaveBeenCalledTimes(1)
      await mon.pollNow()
      expect(toast.warning).toHaveBeenCalledTimes(1)
      // A new id joins → toast again.
      fetchMock.mockResolvedValue(
        okResponse({ pending: [{ id: 'a1' }, { id: 'a2' }], mode: 'prompt' }),
      )
      await mon.pollNow()
      expect(toast.warning).toHaveBeenCalledTimes(2)
      // Shrinking the queue is not news.
      fetchMock.mockResolvedValue(okResponse({ pending: [{ id: 'a2' }], mode: 'prompt' }))
      await mon.pollNow()
      expect(toast.warning).toHaveBeenCalledTimes(2)
    })

    it('a backfill that only confirms an item an event already brought stays quiet', async () => {
      const rpc = fakeRpc()
      makeMonitor().start(rpc)
      rpc.emit('approval.requested', { id: 'a1', namespace: 'exec' })
      expect(toast.warning).toHaveBeenCalledTimes(1)
      fetchMock.mockResolvedValue(
        okResponse({ pending: [{ id: 'a1', namespace: 'exec' }], mode: 'prompt' }),
      )
      rpc.setState('connected')
      await flush()
      expect(toast.warning).toHaveBeenCalledTimes(1)
    })
  })

  describe('focus + visibility backfill', () => {
    function setVisibility(state: 'visible' | 'hidden') {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        get: () => state,
      })
    }

    it('backfills exactly once on window focus, with no timer afterwards', async () => {
      const mon = makeMonitor()
      mon.start(fakeRpc('disconnected'))
      await flush()
      expect(fetchMock).not.toHaveBeenCalled()
      window.dispatchEvent(new Event('focus'))
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)
      await vi.advanceTimersByTimeAsync(60_000)
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)
    })

    it('backfills when the document becomes visible', async () => {
      const mon = makeMonitor()
      mon.start()
      await flush()
      fetchMock.mockClear()
      setVisibility('visible')
      document.dispatchEvent(new Event('visibilitychange'))
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(1)
    })

    it('does not backfill when the document becomes hidden', async () => {
      const mon = makeMonitor()
      mon.start()
      await flush()
      fetchMock.mockClear()
      setVisibility('hidden')
      document.dispatchEvent(new Event('visibilitychange'))
      await flush()
      expect(fetchMock).not.toHaveBeenCalled()
      setVisibility('visible')
    })

    it('detaches focus/visibility listeners on stop()', async () => {
      const mon = makeMonitor()
      mon.start()
      await flush()
      mon.stop()
      fetchMock.mockClear()
      window.dispatchEvent(new Event('focus'))
      await flush()
      expect(fetchMock).not.toHaveBeenCalled()
    })
  })

  describe('pollNow', () => {
    it('backfills on demand', async () => {
      const mon = makeMonitor()
      // No start() — pollNow works standalone (approvals view calls it).
      await mon.pollNow()
      expect(fetchMock).toHaveBeenCalledTimes(1)
    })
  })

  describe('resolve', () => {
    const item: Approval = { id: 'a1', namespace: 'exec', command: 'ls' }

    it('POSTs approve-once and backfills', async () => {
      fetchMock
        .mockResolvedValueOnce(okResponse({ ok: true }))
        .mockResolvedValue(okResponse({ pending: [], mode: 'prompt' }))
      const mon = makeMonitor()
      await mon.resolve(item, 'once')
      const [url, opts] = fetchMock.mock.calls[0]!
      expect(url).toBe('/api/approvals/resolve')
      expect(opts.method).toBe('POST')
      const body = JSON.parse(opts.body as string)
      expect(body).toMatchObject({
        id: 'a1',
        namespace: 'exec',
        approved: true,
        allowAlways: false,
        rememberIntent: false,
      })
      expect(body.elevatedMode).toBeUndefined()
      expect(toast.success).toHaveBeenCalledWith('Approval granted', expect.anything())
      // Backfill after resolve.
      expect(fetchMock).toHaveBeenCalledTimes(2)
      expect(fetchMock.mock.calls[1]![0]).toBe('/api/approvals')
    })

    it('never sends "always" or an elevation for a once-only spend approval', async () => {
      fetchMock
        .mockResolvedValueOnce(okResponse({ ok: true }))
        .mockResolvedValue(okResponse({ pending: [], mode: 'prompt' }))
      const spend: Approval = { id: 'f1', namespace: 'flow', command: 'flow_run Poster' }
      expect(canBypass(spend)).toBe(false)
      expect(canAlwaysAllow(spend)).toBe(false)
      expect(canBypass(item)).toBe(true)
      await makeMonitor().resolve(spend, 'bypass')
      const body = JSON.parse(fetchMock.mock.calls[0]![1].body as string)
      expect(body).toMatchObject({
        id: 'f1',
        namespace: 'flow',
        approved: true,
        allowAlways: false,
        rememberIntent: false,
      })
      expect(body.elevatedMode).toBeUndefined()
    })

    it('drops the item from the store as soon as the POST succeeds, before the backfill lands', async () => {
      useApprovals.getState().setFromPoll([item, { id: 'a2', namespace: 'exec' }], 'prompt')
      const gate = deferred<Response>()
      fetchMock.mockResolvedValueOnce(okResponse({ ok: true })).mockReturnValueOnce(gate.promise)
      const mon = makeMonitor()
      const done = mon.resolve(item, 'once')
      await flush()
      expect(backfillCalls(fetchMock)).toBe(1)
      expect(useApprovals.getState().pending.map((p) => p.id)).toEqual(['a2'])
      expect(useApprovals.getState().count).toBe(1)
      gate.resolve(okResponse({ pending: [{ id: 'a2', namespace: 'exec' }], mode: 'prompt' }))
      await done
      expect(useApprovals.getState().pending.map((p) => p.id)).toEqual(['a2'])
    })

    it('keeps the item when the POST fails', async () => {
      useApprovals.getState().setFromPoll([item], 'prompt')
      fetchMock.mockResolvedValue(okResponse({}, false, 500))
      const mon = makeMonitor()
      await expect(mon.resolve(item, 'deny')).rejects.toThrow(/HTTP 500/)
      expect(useApprovals.getState().pending).toEqual([item])
    })

    it('maps "always" to allowAlways + rememberIntent', async () => {
      fetchMock.mockResolvedValue(okResponse({ ok: true }))
      const mon = makeMonitor()
      await mon.resolve(item, 'always')
      const body = JSON.parse(fetchMock.mock.calls[0]![1].body as string)
      expect(body.allowAlways).toBe(true)
      expect(body.rememberIntent).toBe(true)
      expect(body.approved).toBe(true)
    })

    it('maps "bypass" to elevatedMode + persists it under storage version 2', async () => {
      fetchMock.mockResolvedValue(okResponse({ ok: true }))
      const mon = makeMonitor()
      await mon.resolve(item, 'bypass')
      const body = JSON.parse(fetchMock.mock.calls[0]![1].body as string)
      expect(body.approved).toBe(true)
      expect(body.elevatedMode).toBe('bypass')
      expect(localStorage.getItem(ELEVATED_MODE_KEY)).toBe('bypass')
      expect(localStorage.getItem(ELEVATED_MODE_VERSION_KEY)).toBe('2')
      // approval_monitor.js:208-209 — bypass sets approved=true → legacy 'info'
      // tone, which the port maps to toast.success (not warning).
      expect(toast.success).toHaveBeenCalledWith('Approval bypass enabled', expect.anything())
      expect(toast.warning).not.toHaveBeenCalledWith('Approval bypass enabled', expect.anything())
    })

    it('maps "deny" to approved:false and warns', async () => {
      fetchMock.mockResolvedValue(okResponse({ ok: true }))
      const mon = makeMonitor()
      await mon.resolve(item, 'deny')
      const body = JSON.parse(fetchMock.mock.calls[0]![1].body as string)
      expect(body.approved).toBe(false)
      expect(toast.warning).toHaveBeenCalledWith('Approval denied', expect.anything())
    })

    it('defaults namespace to exec when the item omits it', async () => {
      fetchMock.mockResolvedValue(okResponse({ ok: true }))
      const mon = makeMonitor()
      await mon.resolve({ id: 'x' }, 'once')
      const body = JSON.parse(fetchMock.mock.calls[0]![1].body as string)
      expect(body.namespace).toBe('exec')
    })

    it('rejects and error-toasts on a non-ok resolve, without persisting elevated mode', async () => {
      fetchMock.mockResolvedValue(okResponse({}, false, 500))
      const mon = makeMonitor()
      await expect(mon.resolve(item, 'bypass')).rejects.toThrow(/HTTP 500/)
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringContaining('Approval failed'),
        expect.anything(),
      )
      expect(localStorage.getItem(ELEVATED_MODE_KEY)).toBeNull()
    })
  })

  describe('auth header', () => {
    it('attaches a Bearer token from sessionStorage when present', async () => {
      sessionStorage.setItem('agentos.wsToken', 'tok-123')
      const mon = makeMonitor()
      await mon.pollNow()
      const opts = fetchMock.mock.calls[0]![1]
      expect(opts.headers.Authorization).toBe('Bearer tok-123')
    })

    it('omits the Authorization header when no token is stored', async () => {
      const mon = makeMonitor()
      await mon.pollNow()
      const opts = fetchMock.mock.calls[0]![1]
      expect(opts.headers.Authorization).toBeUndefined()
    })
  })
})

describe('approval-monitor pure helpers', () => {
  afterEach(() => localStorage.clear())

  it('approvalsUrl / approvalsResolveUrl / approvalsSettingsUrl are root-absolute (not base-path rewritten)', () => {
    expect(approvalsUrl()).toBe('/api/approvals')
    expect(approvalsResolveUrl()).toBe('/api/approvals/resolve')
    expect(approvalsSettingsUrl()).toBe('/api/approvals/settings')
  })

  describe('saveApprovalMode', () => {
    afterEach(() => {
      sessionStorage.clear()
      vi.unstubAllGlobals()
    })
    it('POSTs { mode } to the settings endpoint with the session Bearer token', async () => {
      sessionStorage.setItem('agentos.wsToken', 'tok-9')
      const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 } as Response)
      vi.stubGlobal('fetch', fetchMock)
      await saveApprovalMode('auto-approve')
      expect(fetchMock).toHaveBeenCalledWith('/api/approvals/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer tok-9' },
        body: JSON.stringify({ mode: 'auto-approve' }),
      })
    })
    it('throws on a non-ok response so the caller can revert', async () => {
      const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 500 } as Response)
      vi.stubGlobal('fetch', fetchMock)
      await expect(saveApprovalMode('auto-deny')).rejects.toThrow('HTTP 500')
    })
  })

  describe('approvalCommand', () => {
    it('prefers item.command', () => {
      expect(approvalCommand({ id: '1', command: 'ls -la' })).toBe('ls -la')
    })
    it('falls back to argv joined by spaces', () => {
      expect(approvalCommand({ id: '1', argv: ['git', 'push'] })).toBe('git push')
    })
    it('falls back to args.command', () => {
      expect(approvalCommand({ id: '1', args: { command: 'whoami' } })).toBe('whoami')
    })
    it('returns empty string when nothing matches', () => {
      expect(approvalCommand({ id: '1' })).toBe('')
    })
  })

  describe('approvalDetail', () => {
    it('prefers the warning text', () => {
      expect(approvalDetail({ id: '1', warning: 'danger!' })).toBe('danger!')
    })
    it('pretty-prints args as JSON', () => {
      expect(approvalDetail({ id: '1', args: { a: 1 } })).toBe('{\n  "a": 1\n}')
    })
    it('truncates long JSON at 900 chars', () => {
      const big = { s: 'x'.repeat(2000) }
      const out = approvalDetail({ id: '1', args: big })
      expect(out.length).toBe(903) // 900 + '...'
      expect(out.endsWith('...')).toBe(true)
    })
    it('returns empty string when there is nothing to show', () => {
      expect(approvalDetail({ id: '1' })).toBe('')
    })
  })

  describe('approvalMeta', () => {
    it('joins present fields with a middle dot', () => {
      expect(approvalMeta({ id: '1', namespace: 'exec', sessionKey: 's1' }, 'prompt')).toBe(
        'Namespace: exec · Mode: prompt · Session: s1',
      )
    })
    it('omits absent fields', () => {
      expect(approvalMeta({ id: '1', namespace: 'exec' }, '')).toBe('Namespace: exec')
    })
  })

  describe('canAlwaysAllow', () => {
    it('is true only for exec items with a command', () => {
      expect(canAlwaysAllow({ id: '1', namespace: 'exec', command: 'ls' })).toBe(true)
      expect(canAlwaysAllow({ id: '1', namespace: 'exec' })).toBe(false)
      expect(canAlwaysAllow({ id: '1', namespace: 'plugin', command: 'ls' })).toBe(false)
    })
  })

  describe('approvalFromEvent', () => {
    it('prefers the attached queue item, filling namespace from the frame when absent', () => {
      expect(
        approvalFromEvent({ id: 'a1', namespace: 'exec', approval: { id: 'a1', command: 'ls' } }),
      ).toEqual({ id: 'a1', namespace: 'exec', command: 'ls' })
    })
    it('maps the summary fields when no queue item is attached', () => {
      expect(
        approvalFromEvent({
          id: 'a1',
          namespace: 'acp',
          session_key: 's1',
          tool: 'edit',
          command: 'patch',
          summary: 'Edit a file',
          created_at: 5,
          expires_at: 9,
        }),
      ).toEqual({
        id: 'a1',
        namespace: 'acp',
        sessionKey: 's1',
        toolName: 'edit',
        command: 'patch',
        warning: 'Edit a file',
        created_at: 5,
      })
    })
    it('keeps the audience of a child prompt, attached or from the summary fields', () => {
      const audience = ['agent:main:subagent:s1', 'agent:main:webchat:abc']
      expect(
        approvalFromEvent({
          id: 'a1',
          namespace: 'exec',
          audience_session_keys: audience,
          approval: { id: 'a1', audienceSessionKeys: audience },
        })?.audienceSessionKeys,
      ).toEqual(audience)
      expect(
        approvalFromEvent({
          id: 'a1',
          namespace: 'exec',
          session_key: audience[0],
          audience_session_keys: [...audience, 7, ''],
        }),
      ).toEqual({
        id: 'a1',
        namespace: 'exec',
        sessionKey: audience[0],
        audienceSessionKeys: audience,
      })
    })
    it('falls back to the summary fields when the attached item has no id', () => {
      expect(approvalFromEvent({ id: 'a1', namespace: 'exec', approval: {} })).toEqual({
        id: 'a1',
        namespace: 'exec',
      })
    })
    it('returns null without a usable id', () => {
      expect(approvalFromEvent(null)).toBeNull()
      expect(approvalFromEvent('a1')).toBeNull()
      expect(approvalFromEvent({ namespace: 'exec' })).toBeNull()
      expect(approvalFromEvent({ id: '' })).toBeNull()
    })
  })

  // chat.js:2225-2227 (_isApprovalBypassMode) / approvals share the elevated-mode
  // model — bypass + full both skip approval prompts; on/'' do not.
  describe('isApprovalBypassMode', () => {
    it('is true only for bypass and full', () => {
      expect(isApprovalBypassMode('bypass')).toBe(true)
      expect(isApprovalBypassMode('full')).toBe(true)
      expect(isApprovalBypassMode('on')).toBe(false)
      expect(isApprovalBypassMode('')).toBe(false)
    })
  })

  describe('setBrowserElevated', () => {
    afterEach(() => {
      useApprovals.setState({ elevatedMode: '' })
    })
    it('persists valid modes under storage version 2 and returns the normalized value', () => {
      expect(setBrowserElevated('bypass')).toBe('bypass')
      expect(localStorage.getItem(ELEVATED_MODE_KEY)).toBe('bypass')
      expect(localStorage.getItem(ELEVATED_MODE_VERSION_KEY)).toBe('2')
    })
    it('clears the keys for an invalid mode', () => {
      localStorage.setItem(ELEVATED_MODE_KEY, 'bypass')
      localStorage.setItem(ELEVATED_MODE_VERSION_KEY, '2')
      expect(setBrowserElevated('nonsense')).toBe('')
      expect(localStorage.getItem(ELEVATED_MODE_KEY)).toBeNull()
      expect(localStorage.getItem(ELEVATED_MODE_VERSION_KEY)).toBeNull()
    })
    it('mirrors the normalized mode into the reactive store slice (backs the readout)', () => {
      setBrowserElevated('bypass')
      expect(useApprovals.getState().elevatedMode).toBe('bypass')
      setBrowserElevated('nonsense')
      expect(useApprovals.getState().elevatedMode).toBe('')
    })
  })

  describe('readBrowserElevated', () => {
    afterEach(() => localStorage.clear())
    it('downgrades a legacy full under an old storage version to bypass', () => {
      localStorage.setItem(ELEVATED_MODE_KEY, 'full')
      localStorage.setItem(ELEVATED_MODE_VERSION_KEY, '1')
      expect(readBrowserElevated()).toBe('bypass')
    })
    it('keeps full under the current storage version and returns "" when unset', () => {
      localStorage.setItem(ELEVATED_MODE_KEY, 'full')
      localStorage.setItem(ELEVATED_MODE_VERSION_KEY, '2')
      expect(readBrowserElevated()).toBe('full')
      localStorage.clear()
      expect(readBrowserElevated()).toBe('')
    })
  })
})
