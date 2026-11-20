import { act, render, renderHook, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
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

describe('useDraft and the parked copy', () => {
  it('parks an edit from the event, never while another component renders', () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    // A second subscriber, like the pane's "unsaved" dot on the Setup tab.
    function Dot() {
      const unsaved = useDraftStore((s) => Object.keys(s.drafts).length > 0)
      return <span>{unsaved ? 'unsaved' : 'clean'}</span>
    }
    let api: ReturnType<typeof useDraft<{ n: number }>> | undefined
    function Card() {
      api = useDraft('card', { n: 1 })
      return <span>{api.draft.n}</span>
    }
    render(
      <>
        <Dot />
        <Card />
      </>,
    )
    act(() => api!.setDraft((d) => ({ n: d.n + 1 })))
    act(() => api!.setDraft((d) => ({ n: d.n + 1 })))
    expect(screen.getByText('unsaved')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(useDraftStore.getState().drafts).toEqual({ card: { n: 3 } })
    act(() => api!.setDraft({ n: 1 }))
    expect(screen.getByText('clean')).toBeInTheDocument()
    expect(errors).not.toHaveBeenCalled()
    errors.mockRestore()
  })
})
