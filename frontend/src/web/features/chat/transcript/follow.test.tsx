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
    /** Change the height the way layout does: scrollTop is clamped, no event. */
    setHeight(h: number) {
      scrollHeight = h
      scrollTop = Math.max(0, Math.min(scrollTop, scrollHeight - clientHeight))
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
    expect(api.showLatest).toBe(true)
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
    expect(api.showLatest).toBe(false)
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

  it('offers Latest whenever a paused reader is away from the end', async () => {
    const s = fakeScroller()
    let api!: FollowScroll
    render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    // No new content: being away from the end is enough.
    act(() => s.userScroll(300))
    expect(api.following).toBe(false)
    expect(api.showLatest).toBe(true)
    act(() => s.userScroll(s.bottom))
    expect(api.following).toBe(true)
    expect(api.showLatest).toBe(false)
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
    expect(api.showLatest).toBe(true)
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
    // The smooth scroll is still on its way when the content grows.
    s.grow(400)
    act(() => s.userScroll(700))
    expect(api.following).toBe(true)
    // Moving up from there is the reader pausing again.
    act(() => s.userScroll(650))
    expect(api.following).toBe(false)
  })

  it('keeps a following reader at the end when the finished turn lays out short for a frame', async () => {
    // A settled turn remounts with content-visibility: its first layout uses
    // the placeholder height, scrollTop is clamped, and the turn regrows to
    // the very height the ResizeObserver last saw, so it reports nothing.
    const ro = captureResizeObserver()
    const s = fakeScroller()
    s.el.appendChild(document.createElement('div'))
    let api!: FollowScroll
    const { rerender } = render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    s.grow(2000)
    rerender(<Harness el={s.el} version={1} onApi={(a) => (api = a)} />)
    act(() => ro.resized())
    expect(s.top).toBe(s.bottom)
    s.setHeight(1400)
    rerender(<Harness el={s.el} version={2} onApi={(a) => (api = a)} />)
    act(() => s.el.dispatchEvent(new Event('scroll')))
    s.setHeight(3000)
    await frame()
    await frame()
    expect(api.following).toBe(true)
    expect(s.top).toBe(s.bottom)
    expect(api.showLatest).toBe(false)
  })

  it('lets a wheel up right after a programmatic scroll win over the stream', async () => {
    const s = fakeScroller()
    let api!: FollowScroll
    const { rerender } = render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    s.grow(100)
    rerender(<Harness el={s.el} version={1} onApi={(a) => (api = a)} />)
    expect(s.top).toBe(s.bottom)
    // Same frame as that scroll to the end: the wheel lands, the first
    // scroll step is still close to the end.
    act(() => {
      s.el.dispatchEvent(new WheelEvent('wheel', { deltaY: -1500 }))
      s.userScroll(s.top - 30)
    })
    const reading = s.top
    s.grow(100)
    rerender(<Harness el={s.el} version={2} onApi={(a) => (api = a)} />)
    await frame()
    await frame()
    expect(api.following).toBe(false)
    expect(s.top).toBe(reading)
    expect(api.showLatest).toBe(true)
  })

  it('pauses on a scroll up that lands in the frame after a programmatic scroll', async () => {
    const s = fakeScroller()
    let api!: FollowScroll
    const { rerender } = render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    s.grow(500)
    rerender(<Harness el={s.el} version={1} onApi={(a) => (api = a)} />)
    act(() => s.userScroll(s.top - 400))
    const reading = s.top
    s.grow(100)
    rerender(<Harness el={s.el} version={2} onApi={(a) => (api = a)} />)
    expect(api.following).toBe(false)
    expect(s.top).toBe(reading)
  })

  it.each([
    ['PageUp', () => new KeyboardEvent('keydown', { key: 'PageUp' })],
    ['Home', () => new KeyboardEvent('keydown', { key: 'Home' })],
    ['ArrowUp', () => new KeyboardEvent('keydown', { key: 'ArrowUp' })],
  ])('pauses on %s in the log', async (_name, event) => {
    const s = fakeScroller()
    let api!: FollowScroll
    render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    act(() => {
      s.el.dispatchEvent(event())
    })
    expect(api.following).toBe(false)
  })

  it('pauses on a touch drag that scrolls up, not on one that scrolls down', async () => {
    const s = fakeScroller()
    let api!: FollowScroll
    render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    const touch = (type: string, y: number) => {
      const e = new Event(type) as Event & { touches: Array<{ clientY: number }> }
      Object.defineProperty(e, 'touches', { value: [{ clientY: y }] })
      s.el.dispatchEvent(e)
    }
    act(() => {
      touch('touchstart', 300)
      touch('touchmove', 250)
    })
    expect(api.following).toBe(true)
    act(() => touch('touchmove', 320))
    expect(api.following).toBe(false)
  })

  it('pauses on a scrollbar drag up even while still near the end', async () => {
    const s = fakeScroller()
    let api!: FollowScroll
    render(<Harness el={s.el} version={0} onApi={(a) => (api = a)} />)
    await frame()
    act(() => {
      s.el.dispatchEvent(new Event('pointerdown'))
      s.userScroll(s.bottom - 10)
    })
    expect(api.following).toBe(false)
    act(() => {
      window.dispatchEvent(new Event('pointerup'))
    })
  })
})
