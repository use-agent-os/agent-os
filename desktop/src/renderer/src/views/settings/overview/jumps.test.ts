import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useUi } from '~/stores/ui'
import { FOCUS_TTL_MS, jumpTo, registerJump, takeFocus } from './jumps'

beforeEach(() => {
  useUi.setState({ settingsSection: 'overview' })
})

describe('jumps', () => {
  it('switches the section and runs the hooks registered for it', () => {
    const memory = vi.fn()
    const other = vi.fn()
    const off = registerJump('memory', memory)
    registerJump('channels', other)
    jumpTo('memory', 'setup')
    expect(useUi.getState().settingsSection).toBe('memory')
    expect(memory).toHaveBeenCalledWith('setup')
    expect(other).not.toHaveBeenCalled()
    off()
    jumpTo('memory')
    expect(memory).toHaveBeenCalledTimes(1)
  })

  it('hands the focus to the section it was for, while the jump is fresh', () => {
    jumpTo('capabilities', 'image')
    expect(takeFocus('channels')).toBeNull()
    // Readable twice: development mounts run their effects twice.
    expect(takeFocus('capabilities')).toBe('image')
    expect(takeFocus('capabilities')).toBe('image')
    expect(takeFocus('capabilities', Date.now() + FOCUS_TTL_MS + 1)).toBeNull()
    expect(takeFocus('capabilities')).toBeNull()
  })

  it('a plain jump clears an earlier focus', () => {
    jumpTo('capabilities', 'voice')
    jumpTo('capabilities')
    expect(takeFocus('capabilities')).toBeNull()
  })
})
