import { describe, expect, it } from 'vitest'
import { isPlaceholderSessionName, shownSessionName } from './session-name'

describe('session placeholder names', () => {
  it('treats the gateway defaults as unnamed, case-insensitively', () => {
    for (const n of ['WebChat', 'webchat', 'Chat', 'New session', 'untitled', '', '  ', null]) {
      expect(isPlaceholderSessionName(n)).toBe(true)
    }
  })
  it('keeps real titles', () => {
    expect(isPlaceholderSessionName('Plan the Tokyo trip')).toBe(false)
    expect(shownSessionName(' Plan the Tokyo trip ')).toBe('Plan the Tokyo trip')
    expect(shownSessionName('WebChat')).toBe('')
  })
})
