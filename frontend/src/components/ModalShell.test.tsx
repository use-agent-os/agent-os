import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
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

function NestedModalHarness() {
  const [outerOpen, setOuterOpen] = useState(false)
  const [innerOpen, setInnerOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOuterOpen(true)}>
        Open editor
      </button>
      {outerOpen ? (
        <ModalShell
          role="dialog"
          labelledBy="outer-title"
          onClose={() => setOuterOpen(false)}
          overlayClassName="outer-overlay"
          className="outer-dialog"
        >
          <h2 id="outer-title">Editor</h2>
          <button type="button" onClick={() => setInnerOpen(true)}>
            Discard changes
          </button>
          <button type="button">Outer last action</button>
          {innerOpen ? (
            <ModalShell
              role="alertdialog"
              labelledBy="inner-title"
              onClose={() => setInnerOpen(false)}
              overlayClassName="inner-overlay"
              className="inner-dialog"
            >
              <h2 id="inner-title">Confirm discard</h2>
              <button type="button">Keep editing</button>
              <button type="button">Confirm discard</button>
            </ModalShell>
          ) : null}
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

  it('keeps Shift+Tab inside the topmost nested modal', () => {
    render(<NestedModalHarness />)
    fireEvent.click(screen.getByRole('button', { name: 'Open editor' }))
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))

    const firstInner = screen.getByRole('button', { name: 'Keep editing' })
    const lastInner = screen.getByRole('button', { name: 'Confirm discard' })
    expect(firstInner).toHaveFocus()

    fireEvent.keyDown(firstInner, { key: 'Tab', shiftKey: true })

    expect(lastInner).toHaveFocus()
    expect(screen.getByRole('button', { name: 'Outer last action' })).not.toHaveFocus()
  })

  it('swallows Escape and backdrop presses when the dialog is not dismissible', () => {
    const onClose = vi.fn()
    render(
      <ModalShell
        role="alertdialog"
        labelledBy="blocking-title"
        onClose={onClose}
        dismissible={false}
        overlayClassName="blocking-overlay"
      >
        <h2 id="blocking-title">Blocking operation</h2>
        <button type="button">Continue</button>
      </ModalShell>,
    )

    fireEvent.keyDown(screen.getByRole('alertdialog'), { key: 'Escape' })
    fireEvent.mouseDown(document.querySelector('.blocking-overlay')!)

    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })
})
