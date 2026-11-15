// @vitest-environment node
import { describe, expect, it, vi } from 'vitest'
import {
  clipboardDigest,
  ClipboardScrub,
  SCRUB_MAX_MS,
  SCRUB_MAX_PENDING,
  SCRUB_MIN_MS,
  scrubBeforeQuit,
} from './clipboard-scrub'

const KEY = '0x5555555555555555555555555555555555555555555555555555555555555555'

function harness(initial = KEY) {
  const board = { text: initial }
  // Electron's clipboard.readText is async.
  const clear = vi.fn(() => {
    board.text = ''
  })
  const scrub = new ClipboardScrub({
    readText: async () => board.text,
    clear,
    setTimer: (fn, ms) => setTimeout(fn, ms),
    clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  })
  return { board, clear, scrub }
}

describe('ClipboardScrub (WAL-15)', () => {
  it('blanks the clipboard after the window when it still holds the secret', async () => {
    vi.useFakeTimers()
    try {
      const { board, scrub } = harness()
      expect(scrub.schedule(clipboardDigest(KEY), 60_000)).toBe(true)
      await vi.advanceTimersByTimeAsync(59_999)
      expect(board.text).toBe(KEY)
      await vi.advanceTimersByTimeAsync(1)
      expect(board.text).toBe('')
      expect(scrub.size).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('leaves anything copied over the secret alone', async () => {
    vi.useFakeTimers()
    try {
      const { board, clear, scrub } = harness()
      scrub.schedule(clipboardDigest(KEY), 60_000)
      board.text = 'a grocery list'
      await vi.advanceTimersByTimeAsync(60_000)
      expect(clear).not.toHaveBeenCalled()
      expect(board.text).toBe('a grocery list')
    } finally {
      vi.useRealTimers()
    }
  })

  it('holds the quit to scrub a pending secret, then quits; leaves a quit alone otherwise', async () => {
    vi.useFakeTimers()
    try {
      const { board, scrub } = harness()
      const idle = { preventDefault: vi.fn() }
      const quitIdle = vi.fn()
      scrubBeforeQuit(scrub, idle, quitIdle)
      expect(idle.preventDefault).not.toHaveBeenCalled()
      expect(quitIdle).not.toHaveBeenCalled()

      scrub.schedule(clipboardDigest(KEY), 60_000)
      const event = { preventDefault: vi.fn() }
      const quit = vi.fn()
      scrubBeforeQuit(scrub, event, quit)
      expect(event.preventDefault).toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(0)
      expect(board.text).toBe('')
      expect(quit).toHaveBeenCalledTimes(1)
      expect(scrub.size).toBe(0)
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('never hangs a quit on a clipboard that does not answer', async () => {
    vi.useFakeTimers()
    try {
      const scrub = new ClipboardScrub({
        readText: () => new Promise<string>(() => {}),
        clear: vi.fn(),
        setTimer: (fn, ms) => setTimeout(fn, ms),
        clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
      })
      scrub.schedule(clipboardDigest(KEY), 60_000)
      const quit = vi.fn()
      scrubBeforeQuit(scrub, { preventDefault: vi.fn() }, quit, 500)
      await vi.advanceTimersByTimeAsync(499)
      expect(quit).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(1)
      expect(quit).toHaveBeenCalledTimes(1)
      expect(scrub.size).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('refuses anything but a SHA-256 hex digest and a finite delay, and clamps the window', async () => {
    vi.useFakeTimers()
    try {
      const { board, scrub } = harness()
      expect(scrub.schedule(KEY, 60_000)).toBe(false)
      expect(scrub.schedule(clipboardDigest(KEY).toUpperCase(), 60_000)).toBe(false)
      expect(scrub.schedule(clipboardDigest(KEY), Number.NaN)).toBe(false)
      expect(scrub.schedule(clipboardDigest(KEY), '60000')).toBe(false)
      expect(scrub.size).toBe(0)
      // Asked for "never": still blanked within the longest window.
      scrub.schedule(clipboardDigest(KEY), Number.MAX_SAFE_INTEGER)
      await vi.advanceTimersByTimeAsync(SCRUB_MAX_MS)
      expect(board.text).toBe('')
      // Asked for 0: waits the shortest window, not an instant clear.
      board.text = KEY
      scrub.schedule(clipboardDigest(KEY), 0)
      await vi.advanceTimersByTimeAsync(SCRUB_MIN_MS - 1)
      expect(board.text).toBe(KEY)
      await vi.advanceTimersByTimeAsync(1)
      expect(board.text).toBe('')
    } finally {
      vi.useRealTimers()
    }
  })

  it('a second copy of the same key restarts its window; past the cap the oldest is scrubbed now', async () => {
    vi.useFakeTimers()
    try {
      const { board, scrub } = harness()
      scrub.schedule(clipboardDigest(KEY), 60_000)
      await vi.advanceTimersByTimeAsync(30_000)
      scrub.schedule(clipboardDigest(KEY), 60_000)
      await vi.advanceTimersByTimeAsync(30_000)
      expect(board.text).toBe(KEY)
      for (let i = 0; i < SCRUB_MAX_PENDING; i++)
        scrub.schedule(clipboardDigest(`other ${i}`), 60_000)
      await vi.advanceTimersByTimeAsync(0)
      // The key's scrub was the oldest: it ran early rather than being dropped.
      expect(board.text).toBe('')
      expect(scrub.size).toBe(SCRUB_MAX_PENDING)
    } finally {
      vi.useRealTimers()
    }
  })
})
