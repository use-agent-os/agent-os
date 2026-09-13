import { describe, expect, it } from 'vitest'
import { normalizeThemeSettings, resolveTheme } from './theme'

describe('resolveTheme', () => {
  it('follows the OS under system', () => {
    expect(resolveTheme('system', true)).toBe('dark')
    expect(resolveTheme('system', false)).toBe('light')
  })
  it('ignores the OS for explicit preferences', () => {
    expect(resolveTheme('light', true)).toBe('light')
    expect(resolveTheme('dark', false)).toBe('dark')
  })
})

describe('normalizeThemeSettings', () => {
  it('accepts valid values', () => {
    expect(normalizeThemeSettings({ preference: 'dark', palette: 'graphite' })).toEqual({
      preference: 'dark',
      palette: 'graphite',
    })
  })
  it('replaces junk with defaults', () => {
    expect(normalizeThemeSettings({ preference: 'neon', palette: 42 })).toEqual({
      preference: 'system',
      palette: 'tactical',
    })
    expect(normalizeThemeSettings(null)).toEqual({ preference: 'system', palette: 'tactical' })
  })
})
