import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Composer } from './Composer'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

// The Composer is a real, idiomatic React component (unlike the imperative
// transcript region). It owns the command line: a growing textarea, the
// bracket Send/Abort buttons, Enter-to-send, sent-message history cycling on
// ↑/↓, and autofocus. Send/abort are injected callbacks — ChatPage wires them
// to useTranscript's chat.send / chat.abort actions. These RTL tests exercise
// the component's behavior directly; the RPC payload/flow is covered where the
// wiring lives (useTranscript).

const textbox = () => screen.getByRole('textbox') as HTMLTextAreaElement

describe('Composer', () => {
  it('disables send when empty and enables on input', () => {
    render(<Composer onSend={() => {}} busy={false} />)
    const send = screen.getByRole('button', { name: /send/i })
    expect(send).toBeDisabled()
    fireEvent.change(textbox(), { target: { value: 'hi' } })
    expect(send).toBeEnabled()
  })

  it('keeps send disabled for whitespace-only input', () => {
    render(<Composer onSend={() => {}} busy={false} />)
    fireEvent.change(textbox(), { target: { value: '   ' } })
    expect(screen.getByRole('button', { name: /send/i })).toBeDisabled()
  })

  it('shows an abort affordance while busy', () => {
    render(<Composer onSend={() => {}} onAbort={() => {}} busy={true} />)
    expect(screen.getByRole('button', { name: /abort|stop/i })).toBeInTheDocument()
  })

  it('hides the abort affordance when idle', () => {
    render(<Composer onSend={() => {}} onAbort={() => {}} busy={false} />)
    expect(screen.queryByRole('button', { name: /abort|stop/i })).not.toBeInTheDocument()
  })

  it('sends on the Send button click and clears the input', () => {
    const onSend = vi.fn()
    render(<Composer onSend={onSend} busy={false} />)
    fireEvent.change(textbox(), { target: { value: 'hello there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    expect(onSend).toHaveBeenCalledWith('hello there')
    expect(textbox().value).toBe('')
  })

  it('sends on Enter (no shift) and clears', () => {
    const onSend = vi.fn()
    render(<Composer onSend={onSend} busy={false} />)
    const ta = textbox()
    fireEvent.change(ta, { target: { value: 'ping' } })
    fireEvent.keyDown(ta, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledWith('ping')
    expect(ta.value).toBe('')
  })

  it('does not send on Shift+Enter (inserts a newline instead)', () => {
    const onSend = vi.fn()
    render(<Composer onSend={onSend} busy={false} />)
    const ta = textbox()
    fireEvent.change(ta, { target: { value: 'line one' } })
    fireEvent.keyDown(ta, { key: 'Enter', shiftKey: true })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('does not send an empty composer on Enter', () => {
    const onSend = vi.fn()
    render(<Composer onSend={onSend} busy={false} />)
    fireEvent.keyDown(textbox(), { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('aborts on Escape while busy (chat.js:2530 _onDocKeydown → _onStop)', () => {
    const onAbort = vi.fn()
    render(<Composer onSend={() => {}} onAbort={onAbort} busy={true} />)
    fireEvent.keyDown(textbox(), { key: 'Escape' })
    expect(onAbort).toHaveBeenCalledTimes(1)
  })

  it('clears the input on Escape when not busy (chat.js:2449)', () => {
    render(<Composer onSend={() => {}} busy={false} />)
    const ta = textbox()
    fireEvent.change(ta, { target: { value: 'draft' } })
    fireEvent.keyDown(ta, { key: 'Escape' })
    expect(ta.value).toBe('')
  })

  it('cycles backwards through sent history on ArrowUp when empty (chat.js:8711)', () => {
    render(<Composer onSend={() => {}} busy={false} history={['first', 'second']} />)
    const ta = textbox()
    fireEvent.keyDown(ta, { key: 'ArrowUp' })
    expect(ta.value).toBe('second')
    fireEvent.keyDown(ta, { key: 'ArrowUp' })
    expect(ta.value).toBe('first')
  })

  it('cycles forward with ArrowDown and restores the draft at the newest edge', () => {
    render(<Composer onSend={() => {}} busy={false} history={['first', 'second']} />)
    const ta = textbox()
    // Enter nav mode: draft is empty, walk to newest then past it → draft.
    fireEvent.keyDown(ta, { key: 'ArrowUp' }) // 'second'
    fireEvent.keyDown(ta, { key: 'ArrowUp' }) // 'first'
    fireEvent.keyDown(ta, { key: 'ArrowDown' }) // 'second'
    expect(ta.value).toBe('second')
    fireEvent.keyDown(ta, { key: 'ArrowDown' }) // past newest → restored draft ('')
    expect(ta.value).toBe('')
  })

  it('preserves a typed draft when entering history nav and restoring it', () => {
    render(<Composer onSend={() => {}} busy={false} history={['old']} />)
    const ta = textbox()
    fireEvent.change(ta, { target: { value: 'my draft' } })
    // ArrowUp with a non-empty textarea does NOT enter nav (chat.js:2475-2476).
    fireEvent.keyDown(ta, { key: 'ArrowUp' })
    expect(ta.value).toBe('my draft')
  })

  it('does not cycle forward with ArrowDown when not navigating (chat.js:2486)', () => {
    render(<Composer onSend={() => {}} busy={false} history={['a']} />)
    const ta = textbox()
    fireEvent.keyDown(ta, { key: 'ArrowDown' })
    expect(ta.value).toBe('')
  })

  it('enables send with empty text when attachments are pending (chat.js:6064)', () => {
    render(<Composer onSend={() => {}} busy={false} hasPendingAttachments={true} />)
    // Empty composer, but a pending attachment → Send is enabled.
    expect(screen.getByRole('button', { name: /send/i })).toBeEnabled()
  })

  it('sends an attachments-only composer (empty text) on click (chat.js:6118)', () => {
    const onSend = vi.fn()
    render(<Composer onSend={onSend} busy={false} hasPendingAttachments={true} />)
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    expect(onSend).toHaveBeenCalledWith('')
  })

  it('blocks send and warns while attachment work is in flight (chat.js:6067)', async () => {
    const onSend = vi.fn()
    const { toast } = await import('sonner')
    render(
      <Composer onSend={onSend} busy={false} hasPendingAttachments={true} hasPendingWork={true} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    expect(onSend).not.toHaveBeenCalled()
    expect(toast.warning).toHaveBeenCalledWith('Wait for file attachment processing to finish')
  })

  it('exposes an attach-files picker when onAttachFiles is provided', () => {
    const onAttachFiles = vi.fn()
    render(<Composer onSend={() => {}} busy={false} onAttachFiles={onAttachFiles} />)
    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    expect(input).not.toBeNull()
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'a.png', { type: 'image/png' })] },
    })
    expect(onAttachFiles).toHaveBeenCalledTimes(1)
  })

  it('grows the textarea with content (auto-resize, chat.js:2584)', () => {
    render(<Composer onSend={() => {}} busy={false} />)
    const ta = textbox()
    // Empty: no explicit inline height (legacy clears it, chat.js:2586-2588).
    expect(ta.style.height).toBe('')
    Object.defineProperty(ta, 'scrollHeight', { configurable: true, value: 120 })
    fireEvent.change(ta, { target: { value: 'a\nb\nc' } })
    expect(ta.style.height).not.toBe('')
  })
})
