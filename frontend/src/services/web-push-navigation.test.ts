import { describe, expect, it, vi } from 'vitest'
import {
  listenForPushNavigation,
  PUSH_NAVIGATE_MESSAGE,
  pushRouterPath,
} from './web-push-navigation'

describe('pushRouterPath', () => {
  const ORIGIN = 'https://gw.example'

  it('strips the console base from a deep link', () => {
    expect(pushRouterPath('/control/inbox?approval=a1', '/control', ORIGIN)).toBe(
      '/inbox?approval=a1',
    )
    expect(pushRouterPath(`${ORIGIN}/control`, '/control', ORIGIN)).toBe('/')
    expect(pushRouterPath('/inbox?approval=a1', '/', ORIGIN)).toBe('/inbox?approval=a1')
  })

  it('refuses a link that leaves the console', () => {
    expect(pushRouterPath('https://evil.example/control/x', '/control', ORIGIN)).toBeNull()
    expect(pushRouterPath('/controller/x', '/control', ORIGIN)).toBeNull()
  })
})

describe('listenForPushNavigation', () => {
  // Under vitest the console's base path is the dev server's `/control`.
  it('routes on a click relayed by the service worker', () => {
    const container = Object.assign(new EventTarget(), { startMessages: vi.fn() })
    const navigate = vi.fn()
    const stop = listenForPushNavigation(navigate, container as never)
    expect(container.startMessages).toHaveBeenCalledTimes(1)

    container.dispatchEvent(new MessageEvent('message', { data: { type: 'other', url: '/x' } }))
    container.dispatchEvent(
      new MessageEvent('message', {
        data: {
          type: PUSH_NAVIGATE_MESSAGE,
          url: `${window.location.origin}/control/inbox?approval=a1`,
        },
      }),
    )
    expect(navigate).toHaveBeenCalledTimes(1)
    expect(navigate).toHaveBeenCalledWith('/inbox?approval=a1')

    stop()
    container.dispatchEvent(
      new MessageEvent('message', { data: { type: PUSH_NAVIGATE_MESSAGE, url: '/approvals' } }),
    )
    expect(navigate).toHaveBeenCalledTimes(1)
  })

  it('is a no-op without service workers', () => {
    expect(() => listenForPushNavigation(vi.fn(), null)()).not.toThrow()
  })
})
