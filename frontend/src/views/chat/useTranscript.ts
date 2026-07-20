import { useEffect, useRef } from 'react'

/**
 * Imperative transcript controller (Task 1 skeleton).
 *
 * Later tasks grow this into the DOM-owning transcript renderer/streamer that
 * mirrors legacy chat.js's imperative `_thread` handling. For the foundation it
 * only owns a ref to the scroll container so `ChatPage` can mount the thread
 * region; no rendering, subscription, or streaming logic exists yet. The return
 * shape is intentionally open for extension (later tasks add methods/state).
 */
export function useTranscript(opts: { sessionKey: string }): {
  containerRef: React.RefObject<HTMLDivElement | null>
} {
  const containerRef = useRef<HTMLDivElement>(null)

  // The session-change seam: later tasks (re)load history and (re)subscribe to
  // the stream here when the active session changes. The skeleton only observes
  // it so the dependency — and the effect the real controller hangs off — is in
  // place from the foundation.
  useEffect(() => {
    // no-op in the foundation; wired in a later task.
  }, [opts.sessionKey])

  return { containerRef }
}
