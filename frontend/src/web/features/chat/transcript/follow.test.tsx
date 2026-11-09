import '@/test/web'
import { act, render } from '@testing-library/react'
import { useEffect } from 'react'
import { describe, expect, it } from 'vitest'
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
})
