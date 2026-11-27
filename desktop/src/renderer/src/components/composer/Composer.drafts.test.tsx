import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { readDraft, useComposerDrafts } from '~/stores/composer-drafts'
import { Composer } from './Composer'

// Unsent text is kept per `draftKey`: it survives the composer unmounting
// (another page) and does not follow the user into another session.

const textbox = () => screen.getByRole('textbox') as HTMLTextAreaElement
const type = (value: string) => fireEvent.change(textbox(), { target: { value } })

describe('Composer · drafts', () => {
  it('puts the draft back after the composer was unmounted', () => {
    const first = render(<Composer onSend={vi.fn()} busy={false} draftKey="new" />)
    type('line one\nline two')
    first.unmount()

    const onValueChange = vi.fn()
    render(<Composer onSend={vi.fn()} busy={false} draftKey="new" onValueChange={onValueChange} />)
    expect(textbox().value).toBe('line one\nline two')
    // The slash menu and the send button read the value from here.
    expect(onValueChange).toHaveBeenLastCalledWith('line one\nline two')
  })

  it('gives each key its own draft when the key changes under a mounted composer', () => {
    const { rerender } = render(<Composer onSend={vi.fn()} busy={false} draftKey="new" />)
    type('for the new session')

    rerender(<Composer onSend={vi.fn()} busy={false} draftKey="session-a" />)
    expect(textbox().value).toBe('')
    type('for a')

    rerender(<Composer onSend={vi.fn()} busy={false} draftKey="new" />)
    expect(textbox().value).toBe('for the new session')

    rerender(<Composer onSend={vi.fn()} busy={false} draftKey="session-a" />)
    expect(textbox().value).toBe('for a')
  })

  it('does not take focus when it restores a draft on a key change', () => {
    useComposerDrafts.getState().setDraft('session-a', 'waiting')
    const { rerender } = render(
      <Composer onSend={vi.fn()} busy={false} draftKey="new" autoFocus={false} />,
    )
    rerender(<Composer onSend={vi.fn()} busy={false} draftKey="session-a" autoFocus={false} />)
    expect(textbox().value).toBe('waiting')
    expect(document.activeElement).not.toBe(textbox())
  })

  it('forgets the draft once it is sent or cleared with Escape', () => {
    const onSend = vi.fn()
    render(<Composer onSend={onSend} busy={false} draftKey="new" />)
    type('send me')
    expect(readDraft('new')).toBe('send me')
    fireEvent.keyDown(textbox(), { key: 'Enter' })
    expect(onSend).toHaveBeenCalledWith('send me')
    expect(readDraft('new')).toBe('')

    type('never mind')
    fireEvent.keyDown(textbox(), { key: 'Escape' })
    expect(readDraft('new')).toBe('')
  })

  it('keeps nothing without a draft key', () => {
    const first = render(<Composer onSend={vi.fn()} busy={false} />)
    type('gone with the composer')
    first.unmount()
    expect(useComposerDrafts.getState().drafts).toEqual({})
    render(<Composer onSend={vi.fn()} busy={false} />)
    expect(textbox().value).toBe('')
  })
})
