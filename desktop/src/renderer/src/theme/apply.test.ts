import { describe, expect, it } from 'vitest'
import { applyTheme } from './apply'
import { PALETTES } from './palettes'

describe('applyTheme', () => {
  it('writes attributes, color-scheme and every token onto the target element', () => {
    const el = document.createElement('div')
    applyTheme('dark', 'graphite', el)
    expect(el.getAttribute('data-theme')).toBe('dark')
    expect(el.getAttribute('data-palette')).toBe('graphite')
    expect(el.style.colorScheme).toBe('dark')
    expect(el.style.getPropertyValue('--primary')).toBe(PALETTES.graphite.dark.primary)
    expect(el.style.getPropertyValue('--grain-opacity')).toBe('0')
  })

  it('overwrites a previous palette fully when switching', () => {
    const el = document.createElement('div')
    applyTheme('light', 'tactical', el)
    applyTheme('light', 'graphite', el)
    expect(el.style.getPropertyValue('--primary')).toBe(PALETTES.graphite.light.primary)
    expect(el.style.getPropertyValue('--background')).toBe(PALETTES.graphite.light.background)
  })

  it('defaults to document.documentElement', () => {
    applyTheme('light', 'tactical')
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
  })
})
