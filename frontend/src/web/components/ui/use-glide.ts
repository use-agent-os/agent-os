import { useLayoutEffect, useRef, type Ref, type RefCallback, type RefObject } from 'react'

/** What marks the active item inside a `.glide-track`. */
export const GLIDE_ACTIVE =
  '[data-state="active"],[aria-pressed="true"],[aria-current="page"],[aria-selected="true"],[data-active="true"]'

const WATCHED = ['data-state', 'aria-pressed', 'aria-current', 'aria-selected', 'data-active']

/**
 * Drives the `.glide-track` indicator (base.css): measures the active item
 * into --glide-x/y/w/h on the track so one pill glides between items. It
 * follows attribute changes and resizes on its own, so it works for children
 * that re-render without the track (Radix tabs). The first placement does not
 * animate; reduced motion is handled by the stylesheet.
 */
export function useGlide<T extends HTMLElement>(selector: string = GLIDE_ACTIVE) {
  const ref = useRef<T>(null)
  useLayoutEffect(() => {
    const track = ref.current
    if (!track) return
    let frame = 0
    const place = () => {
      const item = track.querySelector<HTMLElement>(selector)
      if (!item || item.offsetParent !== track) {
        delete track.dataset.glide
        return
      }
      const s = track.style
      s.setProperty('--glide-x', `${item.offsetLeft}px`)
      s.setProperty('--glide-y', `${item.offsetTop}px`)
      s.setProperty('--glide-w', `${item.offsetWidth}px`)
      s.setProperty('--glide-h', `${item.offsetHeight}px`)
      if (!track.dataset.glide) {
        track.dataset.glide = 'ready'
        frame = requestAnimationFrame(() => {
          frame = requestAnimationFrame(() => {
            if (track.dataset.glide) track.dataset.glide = 'animate'
          })
        })
      }
    }
    place()
    const mutations = new MutationObserver(place)
    mutations.observe(track, { subtree: true, childList: true, attributeFilter: WATCHED })
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place)
    resize?.observe(track)
    void document.fonts?.ready.then(place)
    return () => {
      mutations.disconnect()
      resize?.disconnect()
      cancelAnimationFrame(frame)
    }
  }, [selector])
  return ref
}

/** Merge a local ref with a ref passed in props (React 19 refs are props). */
export function mergeRefs<T>(...refs: (Ref<T> | undefined)[]): RefCallback<T> {
  return (node) => {
    for (const ref of refs) {
      if (typeof ref === 'function') ref(node)
      else if (ref) (ref as RefObject<T | null>).current = node
    }
  }
}
