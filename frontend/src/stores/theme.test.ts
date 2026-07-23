import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { initTheme, useTheme } from './theme'

beforeEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
})

// Stub matchMedia so `(prefers-color-scheme: dark)` resolves deterministically.
function stubPrefersDark(matches: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockReturnValue({
      matches,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  )
}

describe('theme store', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('initTheme applies stored preference', () => {
    localStorage.setItem('agentos-theme', 'dark')
    initTheme()
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(useTheme.getState().mode).toBe('dark')
  })

  // Parity: js/theme.js:8-16,35 — with no stored value the mode resolves from
  // `prefers-color-scheme`. Mock matchMedia both ways to prove the branch.
  it('initTheme resolves the system default (dark) when nothing is stored', () => {
    stubPrefersDark(true)
    initTheme()
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(useTheme.getState().mode).toBe('dark')
  })

  it('initTheme resolves the system default (light) when nothing is stored', () => {
    stubPrefersDark(false)
    initTheme()
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(useTheme.getState().mode).toBe('light')
  })

  // A stored preference must win over the system default (guards against a
  // regression where systemDefault() shadows the stored value).
  it('initTheme prefers a stored value over the system default', () => {
    stubPrefersDark(true)
    localStorage.setItem('agentos-theme', 'light')
    initTheme()
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(useTheme.getState().mode).toBe('light')
  })

  it('set persists and applies', () => {
    initTheme()
    useTheme.getState().set('dark')
    expect(localStorage.getItem('agentos-theme')).toBe('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
  })

  it('toggle flips the mode', () => {
    localStorage.setItem('agentos-theme', 'light')
    initTheme()
    useTheme.getState().toggle()
    expect(useTheme.getState().mode).toBe('dark')
  })

  it('rejects invalid modes', () => {
    initTheme()
    const before = useTheme.getState().mode
    // @ts-expect-error runtime guard mirrors legacy theme.js
    useTheme.getState().set('purple')
    expect(useTheme.getState().mode).toBe(before)
  })
})
