import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { ModalShell } from './ModalShell'

function ModalHarness() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open dialog
      </button>
      {open ? (
        <ModalShell
          role="dialog"
          labelledBy="modal-test-title"
          onClose={() => setOpen(false)}
          overlayClassName="test-overlay"
          className="test-dialog"
        >
          <h2 id="modal-test-title">Keyboard-safe dialog</h2>
          <button type="button">First action</button>
          <button type="button" onClick={() => setOpen(false)}>
            Last action
          </button>
        </ModalShell>
      ) : null}
    </>
  )
}

describe('ModalShell', () => {
  it('focuses the dialog, contains Tab, and restores focus to its trigger', () => {
    render(<ModalHarness />)
    const trigger = screen.getByRole('button', { name: 'Open dialog' })
    trigger.focus()
    fireEvent.click(trigger)

    const first = screen.getByRole('button', { name: 'First action' })
    const last = screen.getByRole('button', { name: 'Last action' })
    expect(first).toHaveFocus()

    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(first).toHaveFocus()

    first.focus()
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()

    fireEvent.click(last)
    expect(trigger).toHaveFocus()
  })
})
