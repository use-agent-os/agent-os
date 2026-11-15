import { createHash } from 'node:crypto'

/** The clear window the renderer may ask for: at least a second, at most ten minutes. */
export const SCRUB_MIN_MS = 1_000
export const SCRUB_MAX_MS = 10 * 60_000
/** Pending scrubs held at once; the oldest is dropped (scrubbed now) past this. */
export const SCRUB_MAX_PENDING = 8

const DIGEST = /^[0-9a-f]{64}$/

/** SHA-256 of the text, hex: how a secret is named without being held. */
export function clipboardDigest(text: string): string {
  return createHash('sha256').update(text, 'utf8').digest('hex')
}

export interface ScrubDeps {
  readText: () => string | Promise<string>
  clear: () => void
  setTimer: (fn: () => void, ms: number) => unknown
  clearTimer: (handle: unknown) => void
}

/**
 * Blanks a copied secret (an exported private key) off the system clipboard
 * a while after the copy, if the clipboard still holds exactly it. Lives in
 * main so the clear outlives the sheet that copied, a renderer reload and a
 * closed window, and runs at once when the app quits. Only a SHA-256 digest
 * crosses IPC: main never holds the secret itself. Anything copied over it
 * in the meantime is left alone.
 */
export class ClipboardScrub {
  private readonly pending = new Map<string, unknown>()

  constructor(private readonly deps: ScrubDeps) {}

  /** Schedule a scrub of `digest` in `afterMs`; false for anything malformed. */
  schedule(digest: unknown, afterMs: unknown): boolean {
    if (typeof digest !== 'string' || !DIGEST.test(digest)) return false
    if (typeof afterMs !== 'number' || !Number.isFinite(afterMs)) return false
    const ms = Math.min(Math.max(afterMs, SCRUB_MIN_MS), SCRUB_MAX_MS)
    const previous = this.pending.get(digest)
    if (previous !== undefined) this.deps.clearTimer(previous)
    this.pending.delete(digest)
    while (this.pending.size >= SCRUB_MAX_PENDING) {
      const [oldest] = this.pending.keys()
      if (oldest === undefined) break
      void this.fire(oldest)
    }
    this.pending.set(
      digest,
      this.deps.setTimer(() => void this.fire(digest), ms),
    )
    return true
  }

  /** Scrub every pending secret now (app quit). */
  async flush(): Promise<void> {
    await Promise.all([...this.pending.keys()].map((digest) => this.fire(digest)))
  }

  /** Forget every pending scrub without running it (the quit could not wait). */
  abandon(): void {
    for (const handle of this.pending.values()) this.deps.clearTimer(handle)
    this.pending.clear()
  }

  /** How many scrubs are waiting. */
  get size(): number {
    return this.pending.size
  }

  private async fire(digest: string): Promise<void> {
    const handle = this.pending.get(digest)
    if (handle !== undefined) this.deps.clearTimer(handle)
    this.pending.delete(digest)
    try {
      const text = await this.deps.readText()
      if (text && clipboardDigest(text) === digest) this.deps.clear()
    } catch {
      /* clipboard unreadable: nothing to do */
    }
  }
}

/** The longest a quit waits on the clipboard scrub. */
export const QUIT_SCRUB_WAIT_MS = 1_000

/**
 * `will-quit` with a scrub pending: Electron reads the clipboard
 * asynchronously, so the quit is held just long enough to read and blank
 * it (at most `QUIT_SCRUB_WAIT_MS`), then `quit` runs again with nothing
 * pending and goes through. Nothing pending: the quit is not touched.
 */
export function scrubBeforeQuit(
  scrub: ClipboardScrub,
  event: { preventDefault: () => void },
  quit: () => void,
  waitMs = QUIT_SCRUB_WAIT_MS,
): void {
  if (scrub.size === 0) return
  event.preventDefault()
  let timer: ReturnType<typeof setTimeout> | undefined
  const waited = new Promise<void>((resolve) => {
    timer = setTimeout(resolve, waitMs)
  })
  void Promise.race([scrub.flush(), waited])
    .catch(() => {})
    .finally(() => {
      clearTimeout(timer)
      scrub.abandon()
      quit()
    })
}
