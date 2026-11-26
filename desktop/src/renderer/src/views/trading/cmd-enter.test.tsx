import { fireEvent, render, screen } from '@testing-library/react'
import { useRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { Button } from '~/components/ui/button'
import { useCmdEnter } from './cmd-enter'
import { Sheet } from './parts'

const cmdEnter = (el: Element, init: KeyboardEventInit = {}) =>
  fireEvent.keyDown(el, { key: 'Enter', metaKey: true, ...init })

function Scope({ onGo, disabled }: { onGo: () => void; disabled?: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  useCmdEnter(() => ref.current)
  return (
    <div ref={ref} data-testid="scope">
      <input data-testid="field" />
      <button type="button" onClick={onGo} disabled={disabled} data-cmd-enter>
        go
      </button>
    </div>
  )
}

describe('useCmdEnter', () => {
  it('presses the marked button when ⌘↵ comes from inside the scope', () => {
    const onGo = vi.fn()
    render(<Scope onGo={onGo} />)
    cmdEnter(screen.getByTestId('field'))
    expect(onGo).toHaveBeenCalledTimes(1)
  })

  it('ignores a plain Enter, other modifiers, a held key and IME composition', () => {
    const onGo = vi.fn()
    render(<Scope onGo={onGo} />)
    const field = screen.getByTestId('field')
    fireEvent.keyDown(field, { key: 'Enter' })
    cmdEnter(field, { ctrlKey: true })
    cmdEnter(field, { shiftKey: true })
    cmdEnter(field, { altKey: true })
    cmdEnter(field, { repeat: true })
    cmdEnter(field, { isComposing: true })
    fireEvent.keyDown(field, { key: 'Enter', ctrlKey: true })
    expect(onGo).not.toHaveBeenCalled()
  })

  it('leaves a disabled button alone', () => {
    const onGo = vi.fn()
    render(<Scope onGo={onGo} disabled />)
    cmdEnter(screen.getByTestId('field'))
    expect(onGo).not.toHaveBeenCalled()
  })

  it('does nothing for ⌘↵ outside the scope', () => {
    const onGo = vi.fn()
    render(
      <>
        <input data-testid="elsewhere" />
        <Scope onGo={onGo} />
      </>,
    )
    cmdEnter(screen.getByTestId('elsewhere'))
    expect(onGo).not.toHaveBeenCalled()
  })
})

function Confirm({
  onSend,
  onCancel = vi.fn(),
  sendDisabled,
  danger,
}: {
  onSend: () => void
  onCancel?: () => void
  sendDisabled?: boolean
  danger?: boolean
}) {
  return (
    <Sheet
      title="Confirm"
      onClose={vi.fn()}
      foot={
        <>
          <Button onClick={onCancel}>Cancel</Button>
          <Button variant={danger ? 'danger' : 'primary'} disabled={sendDisabled} onClick={onSend}>
            Send
          </Button>
        </>
      }
    >
      <input data-testid="sheet-field" />
    </Sheet>
  )
}

describe('Sheet · ⌘↵ presses the footer’s primary action', () => {
  it('presses the primary button from anywhere in the sheet', () => {
    const onSend = vi.fn()
    const onCancel = vi.fn()
    render(<Confirm onSend={onSend} onCancel={onCancel} />)
    cmdEnter(screen.getByTestId('sheet-field'))
    cmdEnter(screen.getByRole('heading', { name: 'Confirm' }))
    expect(onSend).toHaveBeenCalledTimes(2)
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('skips a disabled primary and never presses a danger button', () => {
    const onSend = vi.fn()
    const { unmount } = render(<Confirm onSend={onSend} sendDisabled />)
    cmdEnter(screen.getByTestId('sheet-field'))
    unmount()
    render(<Confirm onSend={onSend} danger />)
    cmdEnter(screen.getByTestId('sheet-field'))
    expect(onSend).not.toHaveBeenCalled()
  })

  it('a sheet opened over a ticket takes ⌘↵ for itself', () => {
    const onReview = vi.fn()
    const onSend = vi.fn()
    function TicketWithSheet() {
      const [open] = useState(true)
      const ref = useRef<HTMLElement>(null)
      useCmdEnter(() => ref.current)
      return (
        <aside ref={ref}>
          <button type="button" onClick={onReview} data-cmd-enter>
            Review
          </button>
          {open ? <Confirm onSend={onSend} sendDisabled /> : null}
        </aside>
      )
    }
    render(<TicketWithSheet />)
    // The sheet's own Send is disabled: the key must still not fall through
    // the React tree to the ticket that opened it.
    cmdEnter(screen.getByTestId('sheet-field'))
    expect(onReview).not.toHaveBeenCalled()
    expect(onSend).not.toHaveBeenCalled()
  })
})
