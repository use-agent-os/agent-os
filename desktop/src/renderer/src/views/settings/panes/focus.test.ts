import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearOnEscape, keepFocus, useArrivalFocus } from './focus'

function sheet(): { dialog: HTMLElement; anchor: HTMLElement; button: HTMLButtonElement } {
  document.body.innerHTML = `
    <div role="dialog" tabindex="-1" id="sheet">
      <span id="anchor"></span>
      <button id="other">Other</button>
    </div>`
  return {
    dialog: document.getElementById('sheet')!,
    anchor: document.getElementById('anchor')!,
    button: document.getElementById('other') as HTMLButtonElement,
  }
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('keepFocus', () => {
  it('puts focus on the target when the focused control went away', () => {
    const { anchor, button } = sheet()
    const gone = document.createElement('button')
    anchor.parentElement!.appendChild(gone)
    gone.focus()
    gone.remove()
    keepFocus(anchor, () => button)
    vi.runAllTimers()
    expect(document.activeElement).toBe(button)
  })

  it('falls back to the dialog that holds the anchor, so Escape still reaches it', () => {
    const { dialog, anchor } = sheet()
    ;(document.activeElement as HTMLElement | null)?.blur()
    keepFocus(anchor, () => null)
    vi.runAllTimers()
    expect(document.activeElement).toBe(dialog)
  })

  it('counts a control greyed out under focus as lost', () => {
    const { dialog, anchor, button } = sheet()
    button.focus()
    button.disabled = true
    keepFocus(anchor)
    vi.runAllTimers()
    expect(document.activeElement).toBe(dialog)
  })

  it('leaves focus alone when something still holds it', () => {
    const { anchor, button } = sheet()
    button.focus()
    keepFocus(anchor)
    vi.runAllTimers()
    expect(document.activeElement).toBe(button)
  })
})

describe('clearOnEscape', () => {
  function key(name: string) {
    return {
      key: name,
      preventDefault: vi.fn(),
      stopPropagation: vi.fn(),
    } as unknown as React.KeyboardEvent<HTMLInputElement>
  }

  it('clears what was typed and stops there', () => {
    const clear = vi.fn()
    const e = key('Escape')
    clearOnEscape(e, 'xai', clear)
    expect(clear).toHaveBeenCalled()
    expect(e.stopPropagation).toHaveBeenCalled()
  })

  it('lets Escape through when there is nothing to clear, and ignores other keys', () => {
    const clear = vi.fn()
    const empty = key('Escape')
    clearOnEscape(empty, '', clear)
    const enter = key('Enter')
    clearOnEscape(enter, 'xai', clear)
    expect(clear).not.toHaveBeenCalled()
    expect(empty.stopPropagation).not.toHaveBeenCalled()
  })
})

describe('useArrivalFocus', () => {
  it('lands a jump that left focus nowhere on the section’s rail item', () => {
    document.body.innerHTML = '<button id="stg-rail-router">Pilot Router</button>'
    ;(document.activeElement as HTMLElement | null)?.blur()
    renderHook(() => useArrivalFocus('router'))
    vi.runAllTimers()
    expect(document.activeElement).toBe(document.getElementById('stg-rail-router'))
  })

  it('leaves focus where a rail click put it', () => {
    document.body.innerHTML = '<button id="stg-rail-router">Router</button><input id="other" />'
    const other = document.getElementById('other') as HTMLInputElement
    other.focus()
    renderHook(() => useArrivalFocus('router'))
    vi.runAllTimers()
    expect(document.activeElement).toBe(other)
  })
})
