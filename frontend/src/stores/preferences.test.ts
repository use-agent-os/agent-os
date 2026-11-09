import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  DEFAULT_PREFERENCES,
  PREFERENCES_KEY,
  initPreferences,
  parsePreferences,
  usePreferences,
} from './preferences'

beforeEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-text-size')
  usePreferences.setState({ ...DEFAULT_PREFERENCES })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('preferences store', () => {
  it('starts from the defaults', () => {
    initPreferences()
    const s = usePreferences.getState()
    expect({
      sendShortcut: s.sendShortcut,
      textSize: s.textSize,
      showThinking: s.showThinking,
      visualEffects: s.visualEffects,
    }).toEqual(DEFAULT_PREFERENCES)
    expect(document.documentElement.hasAttribute('data-text-size')).toBe(false)
  })

  it('persists under agentos.ui.prefs.v1 and reloads', () => {
    usePreferences.getState().setPreference('sendShortcut', 'mod-enter')
    usePreferences.getState().setPreference('showThinking', false)
    expect(JSON.parse(localStorage.getItem(PREFERENCES_KEY)!)).toMatchObject({
      sendShortcut: 'mod-enter',
      showThinking: false,
    })
    usePreferences.setState({ ...DEFAULT_PREFERENCES })
    initPreferences()
    expect(usePreferences.getState().sendShortcut).toBe('mod-enter')
    expect(usePreferences.getState().showThinking).toBe(false)
  })

  it('drives data-text-size from the text size', () => {
    usePreferences.getState().setPreference('textSize', 'large')
    expect(document.documentElement.getAttribute('data-text-size')).toBe('large')
    usePreferences.getState().setPreference('textSize', 'default')
    expect(document.documentElement.hasAttribute('data-text-size')).toBe(false)
  })

  it('rejects invalid values', () => {
    // @ts-expect-error runtime guard
    usePreferences.getState().setPreference('textSize', 'huge')
    expect(usePreferences.getState().textSize).toBe('default')
  })

  it('keeps valid fields and defaults junk ones', () => {
    expect(parsePreferences('{"textSize":"small","sendShortcut":42}')).toEqual({
      ...DEFAULT_PREFERENCES,
      textSize: 'small',
    })
    expect(parsePreferences('not json')).toEqual(DEFAULT_PREFERENCES)
    expect(parsePreferences('[1,2]')).toEqual(DEFAULT_PREFERENCES)
  })

  it('works when storage is unavailable', () => {
    vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError')
    })
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    expect(() => initPreferences()).not.toThrow()
    expect(() => usePreferences.getState().setPreference('visualEffects', false)).not.toThrow()
    expect(usePreferences.getState().visualEffects).toBe(false)
  })
})
