import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

// Follow-to-latest scrolling (chat spec "Follow scrolling", "History paging").
//
// While following, new content keeps the end in view. The reader pauses
// following by scrolling up: that intent is read from input events (wheel,
// touch drag, keyboard, a scrollbar drag) the moment they happen, and from a
// scroll position above the last one seen. Nothing suppresses scroll events,
// so a reader's scroll that lands between two chunks still wins. Once paused,
// nothing scrolls programmatically until the reader returns to the end, or
// presses Latest or sends. Content growth then leaves the reading position
// alone (the browser's own scroll anchoring would move it, so it is turned off
// on the scroller). Latest is shown whenever the view is away from the end
// and not following, derived from the measured distance, not from a flag.
// Older history inserted above keeps the visible content in place by
// compensating scrollTop for the height added on top.
//
// Settled turns render with `content-visibility: auto`, so a turn's real
// height is only known once it is near the viewport: the height can change
// with no new content, and a turn laid out at its placeholder height for one
// frame clamps scrollTop. While following, any size change of the content
// keeps the end in view (a ResizeObserver on the scroller's content), and two
// frames after a content change the end is checked again, because a shrink and
// a regrowth inside one frame leave the ResizeObserver silent.

export const NEAR_END_PX = 48
/** Sub-pixel rounding: closer than this to the end is at the end. */
const AT_END_PX = 1

type Box = Pick<HTMLElement, 'scrollTop' | 'scrollHeight' | 'clientHeight'>

function distanceFromEnd(el: Box) {
  return el.scrollHeight - el.scrollTop - el.clientHeight
}

export function isNearEnd(el: Box) {
  return distanceFromEnd(el) <= NEAR_END_PX
}

const UP_KEYS = new Set(['PageUp', 'Home', 'ArrowUp'])

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return true
  }
}

export interface FollowScroll {
  /** Attach to the scrolling element. */
  ref: (el: HTMLElement | null) => void
  following: boolean
  /** Not following and the view is away from the end: offer Latest. */
  showLatest: boolean
  /** Scroll to the end and follow again (Latest, send). */
  resume(): void
  /** Call when the reader reaches the top; returns a function to call after inserting older entries. */
  preserveOnPrepend(): () => void
}

/**
 * @param contentVersion changes whenever the transcript content changes
 *   (a counter or the last item's length); growth is detected from it.
 */
export function useFollowScroll(contentVersion: unknown): FollowScroll {
  const elRef = useRef<HTMLElement | null>(null)
  const followingRef = useRef(true)
  const [following, setFollowing] = useState(true)
  const [away, setAway] = useState(false)
  // The last scrollTop seen or set, to tell a move up (the reader) from one down.
  const lastTop = useRef(0)
  // The pointer is held on the scroller itself: a scrollbar drag.
  const dragging = useRef(false)
  const touchY = useRef<number | null>(null)

  const measure = useCallback(() => {
    const el = elRef.current
    if (el) setAway(distanceFromEnd(el) > AT_END_PX)
  }, [])

  const scrollToEnd = useCallback(
    (smooth: boolean) => {
      const el = elRef.current
      if (!el) return
      const top = el.scrollHeight
      if (typeof el.scrollTo === 'function') {
        el.scrollTo({ top, behavior: smooth && !prefersReducedMotion() ? 'smooth' : 'auto' })
      } else {
        el.scrollTop = top
      }
      lastTop.current = el.scrollTop
      measure()
    },
    [measure],
  )

  const setFollow = useCallback((next: boolean) => {
    followingRef.current = next
    setFollowing(next)
  }, [])

  /** The reader asked to look further up: stop following now. */
  const readerUp = useCallback(() => {
    const el = elRef.current
    if (!el || el.scrollTop <= 0 || !followingRef.current) return
    setFollow(false)
  }, [setFollow])

  // A move up pauses following; reaching the end by moving down resumes it.
  // A smooth scroll to the end (Latest) only moves down, so it never pauses.
  const onScroll = useCallback(() => {
    const el = elRef.current
    if (!el) return
    const top = el.scrollTop
    const movedUp = top < lastTop.current - AT_END_PX
    lastTop.current = top
    if (followingRef.current) {
      // Near the end without input this is scrollTop clamped by content
      // that got shorter, not the reader.
      if (movedUp && (dragging.current || !isNearEnd(el))) setFollow(false)
    } else if (!movedUp && isNearEnd(el)) {
      setFollow(true)
    }
    measure()
  }, [measure, setFollow])

  const onWheel = useCallback(
    (e: WheelEvent) => {
      if (e.deltaY < 0 && !e.ctrlKey) readerUp()
    },
    [readerUp],
  )
  const onKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (UP_KEYS.has(e.key) || (e.key === ' ' && e.shiftKey)) readerUp()
    },
    [readerUp],
  )
  const onTouchStart = useCallback((e: TouchEvent) => {
    touchY.current = e.touches[0]?.clientY ?? null
  }, [])
  const onTouchMove = useCallback(
    (e: TouchEvent) => {
      const y = e.touches[0]?.clientY
      if (y === undefined) return
      // The finger moving down drags the content down: scrolling up.
      if (touchY.current !== null && y > touchY.current) readerUp()
      touchY.current = y
    },
    [readerUp],
  )
  const onPointerDown = useCallback((e: PointerEvent) => {
    if (e.target === elRef.current) dragging.current = true
  }, [])
  useEffect(() => {
    const release = () => {
      dragging.current = false
    }
    window.addEventListener('pointerup', release)
    window.addEventListener('pointercancel', release)
    return () => {
      window.removeEventListener('pointerup', release)
      window.removeEventListener('pointercancel', release)
    }
  }, [])

  // Size changes of the content (turns rendering at their real height) keep
  // the end in view while following; they are not new content. So do size
  // changes of the scroller itself: the composer below it grows with text,
  // attachments and the queue, and a shorter view at the same scrollTop
  // would leave the newest content under the composer.
  const resizeObserver = useRef<ResizeObserver | null>(null)
  const observeContent = useCallback(
    (el: HTMLElement | null) => {
      resizeObserver.current?.disconnect()
      resizeObserver.current = null
      if (!el || typeof ResizeObserver === 'undefined') return
      const observer = new ResizeObserver(() => {
        if (followingRef.current && distanceFromEnd(el) > AT_END_PX) scrollToEnd(false)
        else measure()
      })
      const content = el.firstElementChild
      if (content) observer.observe(content)
      observer.observe(el)
      resizeObserver.current = observer
    },
    [measure, scrollToEnd],
  )

  // Every handler is stable, so the list (and what gets detached) is too.
  const listeners = useMemo(
    (): Array<[string, EventListener]> => [
      ['scroll', onScroll],
      ['wheel', onWheel as EventListener],
      ['keydown', onKeyDown as EventListener],
      ['touchstart', onTouchStart as EventListener],
      ['touchmove', onTouchMove as EventListener],
      ['pointerdown', onPointerDown as EventListener],
    ],
    [onScroll, onWheel, onKeyDown, onTouchStart, onTouchMove, onPointerDown],
  )
  const detach = useCallback(() => {
    const el = elRef.current
    if (el) for (const [type, fn] of listeners) el.removeEventListener(type, fn)
  }, [listeners])

  const ref = useCallback(
    (el: HTMLElement | null) => {
      if (el === elRef.current) return
      detach()
      elRef.current = el
      observeContent(el)
      if (el) {
        el.style.overflowAnchor = 'none'
        for (const [type, fn] of listeners) {
          el.addEventListener(type, fn, { passive: true })
        }
        lastTop.current = el.scrollTop
        if (followingRef.current) scrollToEnd(false)
      }
    },
    [detach, listeners, observeContent, scrollToEnd],
  )

  // New content: follow (and check again once it has rendered), or leave the
  // reader where they are.
  useLayoutEffect(() => {
    if (!followingRef.current) {
      measure()
      return
    }
    scrollToEnd(false)
    let inner = 0
    const outer = requestAnimationFrame(() => {
      inner = requestAnimationFrame(() => {
        const el = elRef.current
        if (el && followingRef.current && distanceFromEnd(el) > AT_END_PX) scrollToEnd(false)
      })
    })
    return () => {
      cancelAnimationFrame(outer)
      cancelAnimationFrame(inner)
    }
  }, [contentVersion, measure, scrollToEnd])

  useEffect(
    () => () => {
      detach()
      resizeObserver.current?.disconnect()
    },
    [detach],
  )

  const resume = useCallback(() => {
    setFollow(true)
    scrollToEnd(true)
  }, [scrollToEnd, setFollow])

  const preserveOnPrepend = useCallback(() => {
    const el = elRef.current
    if (!el) return () => {}
    const before = el.scrollHeight
    const top = el.scrollTop
    return () => {
      const added = el.scrollHeight - before
      el.scrollTop = top + added
      lastTop.current = el.scrollTop
      measure()
    }
  }, [measure])

  return { ref, following, showLatest: !following && away, resume, preserveOnPrepend }
}
