import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useUi } from '~/stores/ui'
import { FOCUS_TTL_MS, jumpTo, LAND_MS, registerJump, takeFocus } from './jumps'

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

describe('focus after a jump', () => {
  function section(id: string, html: string): HTMLElement {
    const el = document.createElement('div')
    el.className = 'stg-section'
    el.dataset.section = id
    el.innerHTML = html
    document.body.append(el)
    return el
  }

  it('lands on the destination heading instead of <body>', () => {
    vi.useFakeTimers()
    try {
      const root = section('providers', '<header><h1>Providers</h1></header><button>Add</button>')
      jumpTo('providers')
      vi.advanceTimersByTime(LAND_MS)
      expect(document.activeElement).toBe(root.querySelector('h1'))
      root.remove()
    } finally {
      vi.useRealTimers()
    }
  })

  it('lands on the field holding what the jump was for', () => {
    vi.useFakeTimers()
    try {
      const root = section(
        'environment',
        '<h1>Environment</h1><input type="search" value="XAI_API_KEY" />',
      )
      jumpTo('environment', 'XAI_API_KEY')
      vi.advanceTimersByTime(LAND_MS)
      expect(document.activeElement).toBe(root.querySelector('input'))
      root.remove()
    } finally {
      vi.useRealTimers()
    }
  })

  it('leaves focus alone when the pane already placed it', () => {
    vi.useFakeTimers()
    try {
      const root = section('capabilities', '<h1>Capabilities</h1><input id="key" />')
      jumpTo('capabilities', 'x')
      ;(root.querySelector('#key') as HTMLInputElement).focus()
      vi.advanceTimersByTime(LAND_MS)
      expect(document.activeElement?.id).toBe('key')
      root.remove()
    } finally {
      vi.useRealTimers()
    }
  })
})
