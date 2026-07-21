import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import {
  ApprovalMonitor,
  ELEVATED_MODE_KEY,
  ELEVATED_MODE_VERSION_KEY,
  approvalCommand,
  approvalDetail,
  approvalMeta,
  approvalsResolveUrl,
  approvalsSettingsUrl,
  approvalsUrl,
  canAlwaysAllow,
  isApprovalBypassMode,
  readBrowserElevated,
  saveApprovalMode,
  setBrowserElevated,
  useApprovals,
  type Approval,
} from './approval-monitor'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
  },
}))

// A fetch that returns { pending, mode } once, then keeps returning the same
// value on every subsequent poll (so the self-rescheduling loop stays fed).
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

describe('approval-monitor service', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  // Track every monitor a test creates so a failed assertion (which skips the
  // test's own mon.stop()) can never leak a live focus/visibility listener or a
  // scheduled poll into the next test.
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

  // Drain the microtask queue between fake-timer advances so awaited fetch
  // promises settle before we assert / advance again.
  async function flush() {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  }

  describe('polling + backoff', () => {
    it('polls the root-absolute /api/approvals immediately on start (delay 0)', async () => {
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/approvals',
        expect.objectContaining({ cache: 'no-store' }),
      )
      mon.stop()
    })

    it('grows the backoff 1500→3000→6000… while the queue stays empty', async () => {
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(1) // initial poll (empty → backoff now 3000)

      // Not yet due at 1500ms (backoff already grew past POLL_MS).
      await vi.advanceTimersByTimeAsync(1500)
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(1)

      // Due at 3000ms.
      await vi.advanceTimersByTimeAsync(1500)
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(2) // empty again → backoff 6000

      // 3rd poll is now due at 6000ms, not 3000ms (backoff doubled again).
      await vi.advanceTimersByTimeAsync(5999)
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(2) // not yet
      await vi.advanceTimersByTimeAsync(1)
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(3) // fired at 6000ms
      mon.stop()
    })

    it('clamps the backoff at 30000ms (does not grow without bound)', async () => {
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      // Uncapped exponential growth from 1500ms would blow past 30s within a few
      // polls; a clamped monitor keeps firing on a ≤30s cadence forever. Advance
      // a long horizon (10 minutes) and assert the poll count matches the 30s
      // clamp — an unclamped monitor would fire far fewer times over the same
      // span (its delay would already exceed the whole window).
      fetchMock.mockClear()
      const horizonMs = 10 * 60_000
      const step = 30_000
      for (let elapsed = 0; elapsed < horizonMs; elapsed += step) {
        await vi.advanceTimersByTimeAsync(step)
        await flush()
      }
      // At the 30s clamp the loop yields ~one poll per 30s window (≈20 over
      // 10min). This is the discriminating assertion: an UNCLAMPED monitor's
      // delay would race past the whole 10-minute horizon within a few polls,
      // producing only a handful of calls — nowhere near one-per-window.
      const windows = horizonMs / step
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(windows - 1)
      // Upper guard: the clamp floors the delay at 30s, so we can never exceed
      // one poll per window plus a small boundary allowance from async timer
      // realignment — but always far below the ~400 an uncapped 1500ms floor
      // would give.
      expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(windows + 3)
      mon.stop()
    })

    it('resets the backoff to 1500ms once the queue has pending items', async () => {
      fetchMock.mockResolvedValue(
        okResponse({ pending: [{ id: 'a1', namespace: 'exec' }], mode: 'prompt' }),
      )
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(1)
      // Pending → backoff reset to POLL_MS, so the next poll is due at 1500ms.
      await vi.advanceTimersByTimeAsync(1500)
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(2)
      mon.stop()
    })

    it('stop() clears the scheduled timer so no further polls fire', async () => {
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(1)
      mon.stop()
      await vi.advanceTimersByTimeAsync(60000)
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(1)
    })
  })

  describe('store updates', () => {
    it('publishes pending + count to useApprovals on a successful poll', async () => {
      const pending: Approval[] = [
        { id: 'a1', namespace: 'exec', command: 'rm -rf /' },
        { id: 'a2', namespace: 'plugin' },
      ]
      fetchMock.mockResolvedValue(okResponse({ pending, mode: 'auto-approve' }))
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      const state = useApprovals.getState()
      expect(state.count).toBe(2)
      expect(state.pending).toEqual(pending)
      expect(state.mode).toBe('auto-approve')
      mon.stop()
    })

    it('zeroes the badge but PRESERVES pending on a non-ok response (open prompt survives)', async () => {
      // approval_monitor.js:71-74 — a failed poll only calls _setBadge(0); it
      // never touches the open modal. So the badge count drops to 0 while the
      // pending item backing an open prompt stays put.
      useApprovals.getState().setFromPoll([{ id: 'x' }], 'prompt')
      fetchMock.mockResolvedValue(okResponse({}, false, 503))
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      expect(useApprovals.getState().count).toBe(0)
      expect(useApprovals.getState().pending).toEqual([{ id: 'x' }])
      mon.stop()
    })

    it('zeroes the badge but PRESERVES pending when fetch throws', async () => {
      // approval_monitor.js:92-97 — the catch branch mirrors the non-ok branch.
      useApprovals.getState().setFromPoll([{ id: 'x' }], 'prompt')
      fetchMock.mockRejectedValue(new Error('offline'))
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      expect(useApprovals.getState().count).toBe(0)
      expect(useApprovals.getState().pending).toEqual([{ id: 'x' }])
      mon.stop()
    })

    it('stop() clears the store', async () => {
      useApprovals.getState().setFromPoll([{ id: 'x' }], 'prompt')
      const mon = makeMonitor()
      mon.start()
      mon.stop()
      expect(useApprovals.getState().count).toBe(0)
    })

    it('zeroBadge() zeroes the count but leaves pending intact', () => {
      useApprovals.getState().setFromPoll([{ id: 'x' }, { id: 'y' }], 'prompt')
      useApprovals.getState().zeroBadge()
      expect(useApprovals.getState().count).toBe(0)
      expect(useApprovals.getState().pending).toEqual([{ id: 'x' }, { id: 'y' }])
    })
  })

  describe('toast on new pending', () => {
    it('toasts once when a new pending count appears, and not again for the same count', async () => {
      fetchMock.mockResolvedValue(
        okResponse({ pending: [{ id: 'a1', namespace: 'exec' }], mode: 'prompt' }),
      )
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      expect(toast.warning).toHaveBeenCalledWith(
        'Approval required',
        expect.objectContaining({ duration: 2500 }),
      )
      expect(toast.warning).toHaveBeenCalledTimes(1)
      // Same count on the next poll → no second toast.
      await vi.advanceTimersByTimeAsync(1500)
      await flush()
      expect(toast.warning).toHaveBeenCalledTimes(1)
      mon.stop()
    })

    it('re-toasts when the count changes after dropping to zero', async () => {
      fetchMock
        .mockResolvedValueOnce(okResponse({ pending: [{ id: 'a1' }], mode: 'prompt' }))
        .mockResolvedValueOnce(okResponse({ pending: [], mode: 'prompt' }))
        .mockResolvedValue(okResponse({ pending: [{ id: 'a2' }], mode: 'prompt' }))
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0) // count 1 → toast
      await flush()
      await vi.advanceTimersByTimeAsync(1500) // count 0 → reset lastToastCount
      await flush()
      await vi.advanceTimersByTimeAsync(30000) // count 1 again → toast
      await flush()
      expect(toast.warning).toHaveBeenCalledTimes(2)
      mon.stop()
    })
  })

  describe('focus + visibility re-poll', () => {
    // These tests assert the *re-poll* triggered by the event alone. We clear
    // the fetch spy after start()'s initial poll settles so the assertion is
    // independent of any poll scheduled by the self-rescheduling loop.
    function setVisibility(state: 'visible' | 'hidden') {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        get: () => state,
      })
    }

    it('re-polls immediately and resets backoff on window focus', async () => {
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      fetchMock.mockClear()
      window.dispatchEvent(new Event('focus'))
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(1)
      mon.stop()
    })

    it('re-polls when the document becomes visible', async () => {
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      fetchMock.mockClear()
      setVisibility('visible')
      document.dispatchEvent(new Event('visibilitychange'))
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(1)
      mon.stop()
    })

    it('does not re-poll when the document becomes hidden', async () => {
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      fetchMock.mockClear()
      setVisibility('hidden')
      document.dispatchEvent(new Event('visibilitychange'))
      await flush()
      expect(fetchMock).not.toHaveBeenCalled()
      setVisibility('visible')
      mon.stop()
    })

    it('detaches focus/visibility listeners on stop()', async () => {
      const mon = makeMonitor()
      mon.start()
      await vi.advanceTimersByTimeAsync(0)
      await flush()
      mon.stop()
      fetchMock.mockClear()
      window.dispatchEvent(new Event('focus'))
      await flush()
      expect(fetchMock).not.toHaveBeenCalled()
    })
  })

  describe('pollNow', () => {
    it('polls on demand without waiting for the scheduled tick', async () => {
      const mon = makeMonitor()
      // No start() — pollNow works standalone (approvals view calls it).
      await mon.pollNow()
      await flush()
      expect(fetchMock).toHaveBeenCalledTimes(1)
    })
  })

  describe('resolve', () => {
    const item: Approval = { id: 'a1', namespace: 'exec', command: 'ls' }

    it('POSTs approve-once and re-polls', async () => {
      fetchMock
        .mockResolvedValueOnce(okResponse({ ok: true }))
        .mockResolvedValue(okResponse({ pending: [], mode: 'prompt' }))
      const mon = makeMonitor()
      await mon.resolve(item, 'once')
      await flush()
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
      // Re-poll after resolve.
      expect(fetchMock).toHaveBeenCalledTimes(2)
    })

    it('maps "always" to allowAlways + rememberIntent', async () => {
      fetchMock.mockResolvedValue(okResponse({ ok: true }))
      const mon = makeMonitor()
      await mon.resolve(item, 'always')
      await flush()
      const body = JSON.parse(fetchMock.mock.calls[0]![1].body as string)
      expect(body.allowAlways).toBe(true)
      expect(body.rememberIntent).toBe(true)
      expect(body.approved).toBe(true)
    })

    it('maps "bypass" to elevatedMode + persists it under storage version 2', async () => {
      fetchMock.mockResolvedValue(okResponse({ ok: true }))
      const mon = makeMonitor()
      await mon.resolve(item, 'bypass')
      await flush()
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
      await flush()
      const body = JSON.parse(fetchMock.mock.calls[0]![1].body as string)
      expect(body.approved).toBe(false)
      expect(toast.warning).toHaveBeenCalledWith('Approval denied', expect.anything())
    })

    it('defaults namespace to exec when the item omits it', async () => {
      fetchMock.mockResolvedValue(okResponse({ ok: true }))
      const mon = makeMonitor()
      await mon.resolve({ id: 'x' }, 'once')
      await flush()
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
      await flush()
      const opts = fetchMock.mock.calls[0]![1]
      expect(opts.headers.Authorization).toBe('Bearer tok-123')
    })

    it('omits the Authorization header when no token is stored', async () => {
      const mon = makeMonitor()
      await mon.pollNow()
      await flush()
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
