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
      // Sum offsets up to the track, so items nested in groups (cmdk) or
      // other positioned wrappers still place; give up only when the chain
      // never reaches the track (hidden item, track not positioned).
      let x = 0
      let y = 0
      let node: Element | null = item
      while (node instanceof HTMLElement && node !== track) {
        x += node.offsetLeft
        y += node.offsetTop
        node = node.offsetParent
        // An intermediate parent's border sits between its edge and ours.
        if (node instanceof HTMLElement && node !== track) {
          x += node.clientLeft
          y += node.clientTop
        }
      }
      if (!item || node !== track) {
        delete track.dataset.glide
        return
      }
      const s = track.style
      s.setProperty('--glide-x', `${x}px`)
      s.setProperty('--glide-y', `${y}px`)
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
