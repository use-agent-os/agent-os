// System / Light / Dark preference (overhaul-control-ui D3, design-system spec
// "Theme modes"): migration of the previous console's stored value, live OS
// switching, and the pre-paint bootstrap script.
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { initTheme, resetThemeForTests, useTheme } from './theme'

type Listener = (event: MediaQueryListEvent) => void

function stubScheme(initialDark: boolean) {
  const listeners = new Set<Listener>()
  const query = {
    matches: initialDark,
    addEventListener: vi.fn((_: string, fn: Listener) => listeners.add(fn)),
    removeEventListener: vi.fn((_: string, fn: Listener) => listeners.delete(fn)),
  }
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => query),
  )
  return {
    flip(dark: boolean) {
      query.matches = dark
      for (const fn of listeners) fn({ matches: dark } as MediaQueryListEvent)
    },
    listenerCount: () => listeners.size,
  }
}

const theme = () => document.documentElement.getAttribute('data-theme')

beforeEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
  document.documentElement.removeAttribute('data-text-size')
})

afterEach(() => {
  resetThemeForTests()
  vi.unstubAllGlobals()
})

describe('theme preference', () => {
  it.each(['dark', 'light'] as const)('migrates a stored %s unchanged', (stored) => {
    stubScheme(stored === 'light')
    localStorage.setItem('agentos-theme', stored)
    initTheme()
    expect(useTheme.getState()).toMatchObject({ preference: stored, mode: stored })
    expect(theme()).toBe(stored)
  })

  it('defaults to System when nothing or junk is stored', () => {
    stubScheme(true)
    localStorage.setItem('agentos-theme', 'purple')
    initTheme()
    expect(useTheme.getState()).toMatchObject({ preference: 'system', mode: 'dark' })
  })

  it('follows the OS live while the preference is System', () => {
    const os = stubScheme(false)
    initTheme()
    expect(theme()).toBe('light')
    os.flip(true)
    expect(theme()).toBe('dark')
    expect(useTheme.getState().mode).toBe('dark')
  })

  it('ignores OS changes once an explicit mode is chosen', () => {
    const os = stubScheme(false)
    initTheme()
    useTheme.getState().setPreference('light')
    os.flip(true)
    expect(theme()).toBe('light')
  })

  it('stores System under the existing key and resolves it at once', () => {
    stubScheme(true)
    initTheme()
    useTheme.getState().setPreference('light')
    useTheme.getState().setPreference('system')
    expect(localStorage.getItem('agentos-theme')).toBe('system')
    expect(useTheme.getState().mode).toBe('dark')
  })

  it('attaches a single OS listener across repeated init', () => {
    const os = stubScheme(false)
    initTheme()
    initTheme()
    expect(os.listenerCount()).toBe(1)
  })

  it('keeps working when storage throws', () => {
    stubScheme(true)
    const spy = vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    initTheme()
    expect(useTheme.getState().preference).toBe('system')
    spy.mockRestore()
  })
})

describe('theme-bootstrap.js', () => {
  const script = readFileSync('public/theme-bootstrap.js', 'utf8')
  const run = () => new Function(script)()

  it('paints a stored Light before the app loads', () => {
    stubScheme(true)
    localStorage.setItem('agentos-theme', 'light')
    run()
    expect(theme()).toBe('light')
  })

  it('resolves System from the OS scheme', () => {
    stubScheme(true)
    localStorage.setItem('agentos-theme', 'system')
    run()
    expect(theme()).toBe('dark')
  })

  it('applies a saved text size before first paint', () => {
    stubScheme(false)
    localStorage.setItem('agentos.ui.prefs.v1', JSON.stringify({ textSize: 'large' }))
    run()
    expect(document.documentElement.getAttribute('data-text-size')).toBe('large')
  })
})
