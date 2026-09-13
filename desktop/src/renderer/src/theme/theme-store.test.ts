import { beforeEach, describe, expect, it, vi } from 'vitest'
import { resetDesktopApiForTests } from '~/lib/desktop-api'
import { PALETTES } from './palettes'
import { initTheme, useTheme } from './theme-store'

type MqlListener = (e: MediaQueryListEvent) => void

/** Install a controllable matchMedia so tests can flip the OS appearance. */
function mockMatchMedia(initialDark: boolean) {
  const listeners = new Set<MqlListener>()
  let dark = initialDark
  const mql = {
    get matches() {
      return dark
    },
    media: '(prefers-color-scheme: dark)',
    addEventListener: (_: string, fn: MqlListener) => listeners.add(fn),
    removeEventListener: (_: string, fn: MqlListener) => listeners.delete(fn),
  }
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue(mql))
  return {
    flip(next: boolean) {
      dark = next
      for (const fn of listeners) fn({ matches: next } as MediaQueryListEvent)
    },
  }
}

beforeEach(() => {
  localStorage.clear()
  resetDesktopApiForTests()
  useTheme.setState({
    preference: 'system',
    palette: 'tactical',
    systemDark: false,
    resolved: 'light',
    ready: false,
  })
  document.documentElement.removeAttribute('data-theme')
})

describe('initTheme', () => {
  it('paints the OS mode when preference is system', async () => {
    mockMatchMedia(true)
    await initTheme()
    expect(useTheme.getState()).toMatchObject({ ready: true, resolved: 'dark', systemDark: true })
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(document.documentElement.style.getPropertyValue('--primary')).toBe(
      PALETTES.tactical.dark.primary,
    )
  })

  it('restores a persisted explicit preference and palette', async () => {
    mockMatchMedia(true)
    localStorage.setItem(
      'agentos-desktop.settings',
      JSON.stringify({ theme: { preference: 'light', palette: 'graphite' } }),
    )
    await initTheme()
    expect(useTheme.getState()).toMatchObject({
      preference: 'light',
      palette: 'graphite',
      resolved: 'light',
    })
    expect(document.documentElement.getAttribute('data-palette')).toBe('graphite')
  })

  it('follows OS flips only while preference is system', async () => {
    const os = mockMatchMedia(false)
    const dispose = await initTheme()
    os.flip(true)
    expect(useTheme.getState().resolved).toBe('dark')

    await useTheme.getState().setPreference('light')
    os.flip(false)
    os.flip(true)
    expect(useTheme.getState().resolved).toBe('light')
    dispose()
  })
})

describe('useTheme actions', () => {
  it('setPreference paints immediately and persists', async () => {
    mockMatchMedia(false)
    await initTheme()
    await useTheme.getState().setPreference('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    const saved = JSON.parse(localStorage.getItem('agentos-desktop.settings') ?? '{}')
    expect(saved.theme.preference).toBe('dark')
  })

  it('setPalette repaints tokens without touching the mode', async () => {
    mockMatchMedia(false)
    await initTheme()
    await useTheme.getState().setPalette('graphite')
    expect(useTheme.getState().resolved).toBe('light')
    expect(document.documentElement.style.getPropertyValue('--primary')).toBe(
      PALETTES.graphite.light.primary,
    )
  })

  it('cycle walks system -> light -> dark -> system', async () => {
    mockMatchMedia(false)
    await initTheme()
    const seen: string[] = []
    for (let i = 0; i < 3; i++) {
      await useTheme.getState().cycle()
      seen.push(useTheme.getState().preference)
    }
    expect(seen).toEqual(['light', 'dark', 'system'])
  })
})
