import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  it('autofocuses without moving the persistent route scroller', () => {
    const focus = vi.spyOn(HTMLTextAreaElement.prototype, 'focus')
    try {
      render(<Composer onSend={() => {}} busy={false} />)
      expect(focus).toHaveBeenCalledWith({ preventScroll: true })
    } finally {
      focus.mockRestore()
    }
  })

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
    const stop = screen.getByRole('button', { name: /abort|stop/i })
    expect(stop).toBeInTheDocument()
    // A single SVG child keeps the filled stop square geometrically centred.
    // The previous hidden "Abort" text remained a flex item and `btn-term`'s
    // inherited 8px gap pushed the pseudo-glyph visibly to the left.
    expect(stop.childElementCount).toBe(1)
    expect(stop.querySelector('svg')).not.toBeNull()
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

  it('keeps conversation-level New chat out of the composer and uses an SVG Send icon', () => {
    render(<Composer onSend={() => {}} busy={false} />)
    expect(screen.queryByRole('button', { name: /new chat/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' }).querySelector('svg')).not.toBeNull()
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

  it('keeps router status above composer-local trays and the input row', () => {
    const { container } = render(
      <Composer
        onSend={() => {}}
        busy={false}
        routerFxDock={<div data-testid="router-status" />}
        tray={<div data-testid="attachment-tray" />}
        slashMenu={<div data-testid="slash-menu" />}
      />,
    )
    const dock = screen.getByTestId('router-status')
    const tray = screen.getByTestId('attachment-tray')
    const slashMenu = screen.getByTestId('slash-menu')
    const composer = container.querySelector('.chat-composer')
    expect(Array.from(container.querySelector('.chat-composer-shell')!.children)).toEqual([
      dock,
      tray,
      slashMenu,
      composer,
    ])
  })

  // ── ESC priority chain (chat.js:2530-2538 / 2449) ──────────────────────────

  describe('ESC priority chain (abort > pending-recover > clear)', () => {
    it('ESC while streaming aborts the turn — the top rung (chat.js:2530)', () => {
      const onAbort = vi.fn()
      const onRecoverPending = vi.fn()
      render(
        <Composer
          onSend={() => {}}
          busy={true}
          onAbort={onAbort}
          pendingCount={3}
          onRecoverPending={onRecoverPending}
        />,
      )
      fireEvent.keyDown(textbox(), { key: 'Escape' })
      expect(onAbort).toHaveBeenCalledTimes(1)
      // The recover rung is NOT reached while streaming.
      expect(onRecoverPending).not.toHaveBeenCalled()
    })

    it('ESC while idle with a non-empty queue recovers pending — the middle rung (chat.js:2535)', () => {
      const onRecoverPending = vi.fn(() => true)
      render(
        <Composer
          onSend={() => {}}
          busy={false}
          pendingCount={2}
          onRecoverPending={onRecoverPending}
        />,
      )
      const ta = textbox()
      fireEvent.change(ta, { target: { value: 'draft' } })
      fireEvent.keyDown(ta, { key: 'Escape' })
      expect(onRecoverPending).toHaveBeenCalledTimes(1)
      // The clear rung is skipped — the draft was folded into the recovery, so
      // the composer does not additionally blank the input here.
      expect(ta.value).toBe('draft')
    })

    it('ESC while idle with an empty queue clears the input — the bottom rung (chat.js:2449)', () => {
      const onRecoverPending = vi.fn()
      render(
        <Composer
          onSend={() => {}}
          busy={false}
          pendingCount={0}
          onRecoverPending={onRecoverPending}
        />,
      )
      const ta = textbox()
      fireEvent.change(ta, { target: { value: 'some text' } })
      fireEvent.keyDown(ta, { key: 'Escape' })
      expect(onRecoverPending).not.toHaveBeenCalled()
      expect(ta.value).toBe('')
    })
  })

  describe('pending Alt-key affordances', () => {
    it('Alt+↑ pops the most-recent pending item when the queue is non-empty (chat.js:2457)', () => {
      const onPopPendingTail = vi.fn()
      render(
        <Composer
          onSend={() => {}}
          busy={false}
          pendingCount={2}
          onPopPendingTail={onPopPendingTail}
        />,
      )
      fireEvent.keyDown(textbox(), { key: 'ArrowUp', altKey: true })
      expect(onPopPendingTail).toHaveBeenCalledTimes(1)
    })

    it('Alt+↓ enqueues the current text when there is room (chat.js:2464)', () => {
      const onEnqueueCurrent = vi.fn()
      render(
        <Composer
          onSend={() => {}}
          busy={false}
          pendingCount={0}
          onEnqueueCurrent={onEnqueueCurrent}
        />,
      )
      const ta = textbox()
      fireEvent.change(ta, { target: { value: 'queue me' } })
      fireEvent.keyDown(ta, { key: 'ArrowDown', altKey: true })
      expect(onEnqueueCurrent).toHaveBeenCalledTimes(1)
    })

    it('Alt+↓ does NOT enqueue when the queue is at the cap (chat.js:2464)', () => {
      const onEnqueueCurrent = vi.fn()
      render(
        <Composer
          onSend={() => {}}
          busy={false}
          pendingCount={5}
          onEnqueueCurrent={onEnqueueCurrent}
        />,
      )
      const ta = textbox()
      fireEvent.change(ta, { target: { value: 'over cap' } })
      fireEvent.keyDown(ta, { key: 'ArrowDown', altKey: true })
      expect(onEnqueueCurrent).not.toHaveBeenCalled()
    })
  })

  it('exposes setValue on the imperative handle — the pending-recover write (chat.js:8608)', () => {
    const ref = { current: null } as React.RefObject<import('./Composer').ComposerHandle | null>
    render(<Composer onSend={() => {}} busy={false} composerRef={ref} />)
    ref.current?.setValue('recovered text')
    expect(textbox().value).toBe('recovered text')
    expect(ref.current?.getValue()).toBe('recovered text')
  })

  it('opens the settings popover on the trigger and closes it on an outside click', async () => {
    render(
      <Composer onSend={() => {}} busy={false} toolbar={<div data-testid="tb">settings</div>} />,
    )
    const trigger = screen.getByRole('button', { name: /run modes/i })
    expect(trigger).toHaveAttribute('aria-controls', 'chat-toolbar-popover')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    // Open.
    fireEvent.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('dialog', { name: 'Run modes' })).toBeInTheDocument()
    expect(screen.getByTestId('tb')).toBeInTheDocument()
    // A mousedown outside the toolbar wrap closes it (previously stayed open).
    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(screen.queryByTestId('tb')).not.toBeInTheDocument())
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
  })

  it('closes the settings popover on Escape and restores focus to its trigger', async () => {
    render(
      <Composer onSend={() => {}} busy={false} toolbar={<div data-testid="tb2">settings</div>} />,
    )
    fireEvent.change(textbox(), { target: { value: 'keep this draft' } })
    const trigger = screen.getByRole('button', { name: /run modes/i })
    fireEvent.click(trigger)
    expect(screen.getByTestId('tb2')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Close run modes' })).toHaveFocus()
    fireEvent.keyDown(textbox(), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('tb2')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
    expect(textbox()).toHaveValue('keep this draft')
  })

  it('exposes a labelled close control inside the settings dialog', async () => {
    render(
      <Composer onSend={() => {}} busy={false} toolbar={<div data-testid="tb3">settings</div>} />,
    )
    const trigger = screen.getByRole('button', { name: /run modes/i })
    fireEvent.click(trigger)

    const close = screen.getByRole('button', { name: 'Close run modes' })
    expect(close).toHaveFocus()
    fireEvent.click(close)

    await waitFor(() => expect(screen.queryByTestId('tb3')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })
})
