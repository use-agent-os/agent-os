import '@/test/web'
import { act, render } from '@testing-library/react'
import { useEffect } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useFollowScroll, type FollowScroll } from './follow'

/** A scroller whose geometry the test controls (jsdom has no layout). */
function fakeScroller() {
  const el = document.createElement('div')
  let scrollHeight = 1000
  const clientHeight = 400
  let scrollTop = 0
  Object.defineProperties(el, {
    scrollHeight: { get: () => scrollHeight },
    clientHeight: { get: () => clientHeight },
    scrollTop: {
      get: () => scrollTop,
      set: (v: number) => {
        scrollTop = Math.max(0, Math.min(v, scrollHeight - clientHeight))
      },
    },
  })
  el.scrollTo = ((opts: ScrollToOptions) => {
    el.scrollTop = opts.top ?? 0
  }) as typeof el.scrollTo
  return {
    el,
    grow(by: number) {
      scrollHeight += by
    },
    userScroll(to: number) {
      el.scrollTop = to
      el.dispatchEvent(new Event('scroll'))
    },
    get top() {
      return scrollTop
    },
    get bottom() {
      return scrollHeight - clientHeight
    },
  }
}

function Harness({
  el,
  version,
  onApi,
}: {
  el: HTMLElement
  version: number
  onApi: (api: FollowScroll) => void
}) {
  const api = useFollowScroll(version)
  useEffect(() => {
    api.ref(el)
  }, [api, el])
  onApi(api)
  return null
}

/** Captures the hook's ResizeObserver so a test can report a size change. */
function captureResizeObserver() {
  const observers: Array<{ callback: () => void; targets: Element[] }> = []
  class Captured {
    targets: Element[] = []
    constructor(public callback: () => void) {
      observers.push(this)
    }
    observe(target: Element) {
      this.targets.push(target)
    }
    unobserve() {}
    disconnect() {
      this.targets = []
    }
  }
  vi.stubGlobal('ResizeObserver', Captured)
  return {
    /** The content was laid out again (a turn rendered at its real height). */
    resized() {
      for (const o of observers) if (o.targets.length) o.callback()
    },
    observed: () => observers.flatMap((o) => o.targets),
  }
}

async function frame() {
  await act(() => new Promise((r) => requestAnimationFrame(() => r(null))))
}

describe('useFollowScroll', () => {
  it('follows new content while the reader is at the end', async () => {
    const s = fakeScroller()
    let api!: FollowScroll
    const { rerender } = render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    s.grow(300)
    rerender(<Harness el={s.el} version={1} onApi={(a) => (api = a)} />)
    expect(s.top).toBe(s.bottom)
    expect(api.following).toBe(true)
  })

  it('keeps the reading position while paused, and shows Latest', async () => {
    const s = fakeScroller()
    let api!: FollowScroll
    const { rerender } = render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    act(() => s.userScroll(100))
    expect(api.following).toBe(false)
    s.grow(500)
    rerender(<Harness el={s.el} version={1} onApi={(a) => (api = a)} />)
    expect(s.top).toBe(100)
    expect(api.hasNew).toBe(true)
  })

  it('resumes on send (or Latest) and follows again', async () => {
    const s = fakeScroller()
    let api!: FollowScroll
    const { rerender } = render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    act(() => s.userScroll(50))
    act(() => api.resume())
    expect(s.top).toBe(s.bottom)
    await frame()
    s.grow(200)
    rerender(<Harness el={s.el} version={2} onApi={(a) => (api = a)} />)
    expect(s.top).toBe(s.bottom)
    expect(api.hasNew).toBe(false)
  })

  it('keeps the message being read in place when older entries load above', async () => {
    const s = fakeScroller()
    let api!: FollowScroll
    render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    act(() => s.userScroll(0))
    const done = api.preserveOnPrepend()
    s.grow(800)
    done()
    expect(s.top).toBe(800)
  })

  it('does not offer Latest for older entries loaded above', async () => {
    const s = fakeScroller()
    let api!: FollowScroll
    const { rerender } = render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    act(() => s.userScroll(0))
    const done = api.preserveOnPrepend()
    s.grow(800)
    rerender(<Harness el={s.el} version={1} onApi={(a) => (api = a)} />)
    act(() => done())
    expect(api.hasNew).toBe(false)
    // Content that does arrive afterwards still does.
    s.grow(100)
    rerender(<Harness el={s.el} version={2} onApi={(a) => (api = a)} />)
    expect(api.hasNew).toBe(true)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('keeps the end in view when the content changes size while following', async () => {
    // Settled turns render at their real height only near the viewport, so
    // jumping to the end changes the height again with no new content.
    const ro = captureResizeObserver()
    const s = fakeScroller()
    const content = document.createElement('div')
    s.el.appendChild(content)
    render(<Harness el={s.el} version={0} onApi={() => {}} />)
    await frame()
    expect(ro.observed()).toEqual([content])
    expect(s.top).toBe(s.bottom)
    s.grow(250)
    act(() => ro.resized())
    expect(s.top).toBe(s.bottom)
  })

  it('leaves a paused reader where they are when the content changes size', async () => {
    const ro = captureResizeObserver()
    const s = fakeScroller()
    s.el.appendChild(document.createElement('div'))
    let api!: FollowScroll
    render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    act(() => s.userScroll(120))
    expect(api.following).toBe(false)
    s.grow(250)
    act(() => ro.resized())
    expect(s.top).toBe(120)
    expect(api.hasNew).toBe(false)
  })

  it('keeps following through a scroll down that has not reached the end yet', async () => {
    // A smooth scroll to Latest passes through positions short of the end.
    const s = fakeScroller()
    let api!: FollowScroll
    render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    act(() => s.userScroll(100))
    expect(api.following).toBe(false)
    act(() => api.resume())
    s.grow(400)
    await frame()
    act(() => s.userScroll(700))
    expect(api.following).toBe(true)
    // Moving up from there is the reader pausing again.
    act(() => s.userScroll(650))
    expect(api.following).toBe(false)
  })
})
