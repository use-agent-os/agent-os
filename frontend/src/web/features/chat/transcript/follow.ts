import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

// Follow-to-latest scrolling (chat spec "Follow scrolling", "History paging").
//
// While the reader is within NEAR_END_PX of the bottom, new content keeps the
// end in view. Scrolling up pauses following: content growth then leaves the
// reading position alone (the browser's own scroll anchoring would move it,
// so it is turned off on the scroller) and a Latest control appears once new
// content exists. Latest, scrolling back to the end, or sending resumes.
// Older history inserted above keeps the visible content in place by
// compensating scrollTop for the height added on top.

export const NEAR_END_PX = 48

export function isNearEnd(el: Pick<HTMLElement, 'scrollTop' | 'scrollHeight' | 'clientHeight'>) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_END_PX
}

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
  /** True when following is paused and content arrived since. */
  hasNew: boolean
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
  const [hasNew, setHasNew] = useState(false)
  const programmatic = useRef(false)
  // Older history going in above is not new content: no Latest for it.
  const prepending = useRef(false)

  const scrollToEnd = useCallback((smooth: boolean) => {
    const el = elRef.current
    if (!el) return
    programmatic.current = true
    const top = el.scrollHeight
    if (typeof el.scrollTo === 'function') {
      el.scrollTo({ top, behavior: smooth && !prefersReducedMotion() ? 'smooth' : 'auto' })
    } else {
      el.scrollTop = top
    }
    requestAnimationFrame(() => {
      programmatic.current = false
    })
  }, [])

  const setFollow = useCallback((next: boolean) => {
    followingRef.current = next
    setFollowing(next)
    if (next) setHasNew(false)
  }, [])

  const onScroll = useCallback(() => {
    const el = elRef.current
    if (!el || programmatic.current) return
    const near = isNearEnd(el)
    if (near !== followingRef.current) setFollow(near)
  }, [setFollow])

  const ref = useCallback(
    (el: HTMLElement | null) => {
      if (elRef.current) elRef.current.removeEventListener('scroll', onScroll)
      elRef.current = el
      if (el) {
        el.style.overflowAnchor = 'none'
        el.addEventListener('scroll', onScroll, { passive: true })
      }
    },
    [onScroll],
  )

  // New content: follow, or flag Latest.
  useLayoutEffect(() => {
    if (followingRef.current) scrollToEnd(false)
    else if (!prepending.current) setHasNew(true)
  }, [contentVersion, scrollToEnd])

  useEffect(() => () => elRef.current?.removeEventListener('scroll', onScroll), [onScroll])

  const resume = useCallback(() => {
    setFollow(true)
    scrollToEnd(true)
  }, [scrollToEnd, setFollow])

  const preserveOnPrepend = useCallback(() => {
    const el = elRef.current
    if (!el) return () => {}
    prepending.current = true
    const before = el.scrollHeight
    const top = el.scrollTop
    return () => {
      prepending.current = false
      const added = el.scrollHeight - before
      programmatic.current = true
      el.scrollTop = top + added
      requestAnimationFrame(() => {
        programmatic.current = false
      })
    }
  }, [])

  return { ref, following, hasNew, resume, preserveOnPrepend }
}
