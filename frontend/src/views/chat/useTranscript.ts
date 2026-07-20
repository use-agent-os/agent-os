import { useEffect, useRef, useState } from 'react'
import { createStreamController, type StreamController } from './transcript/stream'

/**
 * Imperative transcript controller.
 *
 * This hook owns the DOM-mutating streaming renderer that mirrors legacy
 * chat.js's imperative `_thread` handling (design §2.1 — the owner-approved
 * imperative boundary). It creates a single `StreamController` bound to the
 * scroll-container ref and keeps it alive across renders; the controller reads
 * the live session key through a ref-backed holder so a session change does not
 * re-create it (later tasks call `parkCurrentSessionStreamState` /
 * `restoreLiveStreamStateForSession` across the session seam instead of tearing
 * the controller down).
 *
 * The return shape stays open for extension: later tasks expose the event-wiring
 * surface (RPC subscription → controller methods) on top of `controller`.
 */
export function useTranscript(opts: { sessionKey: string }): {
  containerRef: React.RefObject<HTMLDivElement | null>
  controller: StreamController
} {
  const containerRef = useRef<HTMLDivElement>(null)

  // Live session key holder (legacy `_sessionKey`), read by the controller.
  // A ref so the controller — created once — always sees the current value
  // without being re-created when the session changes. Written only in an
  // effect (never during render).
  const sessionKeyRef = useRef(opts.sessionKey)

  // The controller is created exactly once (lazy state initializer, so the
  // factory runs a single time and the value is stable across renders). Its
  // DOM-owning fields live on the instance for its whole lifetime (matching
  // legacy module-globals). Both `containerRef.current` and `sessionKeyRef`
  // are read LAZILY inside the controller's methods (never during creation),
  // so passing the refs into the factory here is safe — the
  // eslint-disable below documents that verified fact, since the strict
  // react-hooks/refs heuristic cannot see that the factory defers the reads.
  // eslint-disable-next-line react-hooks/refs -- factory stores the refs and reads .current only later, inside methods invoked outside render (never at creation)
  const [controller] = useState<StreamController>(() =>
    createStreamController(containerRef, {
      getSessionKey: () => sessionKeyRef.current,
    }),
  )

  // Keep the session-key holder current. Done in an effect (not during render)
  // so it never writes a ref value the strict rules forbid touching in render.
  useEffect(() => {
    sessionKeyRef.current = opts.sessionKey
  }, [opts.sessionKey])

  // The session-change seam: later tasks park the outgoing session's live
  // stream state and (re)load history + restore/subscribe for the incoming
  // session here. The foundation only observes the dependency so the effect the
  // real controller wiring hangs off is in place.
  useEffect(() => {
    // no-op for now; event wiring lands in a later task.
  }, [opts.sessionKey])

  // On unmount, tear down any live stream timers/rAF so a backgrounded stream
  // does not leak a timeout or animation-frame callback past the view.
  useEffect(() => {
    return () => {
      controller.clearViewLocalStreamState('unmount')
    }
  }, [controller])

  return { containerRef, controller }
}
