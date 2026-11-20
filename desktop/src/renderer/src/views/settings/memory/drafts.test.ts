import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { useDraft, useDraftStore } from './drafts'

beforeEach(() => useDraftStore.setState({ drafts: {} }))

describe('useDraft', () => {
  it('follows a newer saved value while nothing is being edited', () => {
    const { result, rerender } = renderHook(({ saved }) => useDraft('card', saved), {
      initialProps: { saved: { n: 1 } },
    })
    rerender({ saved: { n: 2 } })
    expect(result.current.draft).toEqual({ n: 2 })
    expect(result.current.edited).toBe(false)
  })

  it('keeps an edit when the saved value changes underneath it', () => {
    const { result, rerender } = renderHook(({ saved }) => useDraft('card', saved), {
      initialProps: { saved: { n: 1, m: 1 } },
    })
    act(() => result.current.setDraft((d) => ({ ...d, n: 5 })))
    rerender({ saved: { n: 1, m: 2 } })
    expect(result.current.draft).toEqual({ n: 5, m: 1 })
    expect(result.current.edited).toBe(true)
  })

  it('parks an edit so a remount (another tab or section) gets it back', () => {
    const first = renderHook(() => useDraft('card', { n: 1 }))
    act(() => first.result.current.setDraft({ n: 7 }))
    first.unmount()
    const second = renderHook(() => useDraft('card', { n: 1 }))
    expect(second.result.current.draft).toEqual({ n: 7 })
    act(() => second.result.current.revert())
    expect(second.result.current.draft).toEqual({ n: 1 })
    expect(useDraftStore.getState().drafts).toEqual({})
  })

  it('after a save, takes whatever the gateway saved as the form', () => {
    const { result, rerender } = renderHook(({ saved }) => useDraft('card', saved), {
      initialProps: { saved: { t: '8' } },
    })
    act(() => result.current.setDraft({ t: '8.0' }))
    act(() => result.current.settle())
    rerender({ saved: { t: '9' } })
    expect(result.current.draft).toEqual({ t: '9' })
    expect(useDraftStore.getState().drafts).toEqual({})
  })
})
