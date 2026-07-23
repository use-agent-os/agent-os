import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PendingQueue } from './PendingQueue'
import type { PendingAttachment, PendingItem } from './logic'

const item = (text: string, attachments: PendingAttachment[] = []): PendingItem => ({
  text,
  attachments,
  intent: null,
})

const att = (name: string): PendingAttachment => ({
  kind: 'inline',
  local_id: Math.random(),
  name,
  mime: 'image/png',
  size: 1,
})

describe('PendingQueue', () => {
  it('renders nothing when the queue is empty (chat.js:8476-8480)', () => {
    const { container } = render(
      <PendingQueue queue={[]} onRemove={vi.fn()} onClearAll={vi.fn()} />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('renders the Pending N/5 count label (chat.js:8484)', () => {
    render(<PendingQueue queue={[item('one')]} onRemove={vi.fn()} onClearAll={vi.fn()} />)
    expect(screen.getByText('Pending 1/5')).toBeInTheDocument()
  })

  it('renders one chip per queued item with a 30-char preview (chat.js:8489-8500)', () => {
    const long = 'x'.repeat(50)
    render(<PendingQueue queue={[item(long)]} onRemove={vi.fn()} onClearAll={vi.fn()} />)
    // 30 chars + ellipsis.
    expect(screen.getByText('x'.repeat(30) + '…')).toBeInTheDocument()
  })

  it('shows the attachment count chip when an item carries attachments (chat.js:8492-8493)', () => {
    render(
      <PendingQueue
        queue={[item('with files', [att('a'), att('b')])]}
        onRemove={vi.fn()}
        onClearAll={vi.fn()}
      />,
    )
    expect(screen.getByText('📎2')).toBeInTheDocument()
  })

  it('labels an attachment-only item as (attachment only) (chat.js:8490)', () => {
    render(<PendingQueue queue={[item('', [att('a')])]} onRemove={vi.fn()} onClearAll={vi.fn()} />)
    expect(screen.getByText('(attachment only)')).toBeInTheDocument()
  })

  it('hides Clear all with a single item, shows it at 2+ (chat.js:8482)', () => {
    const { rerender } = render(
      <PendingQueue queue={[item('a')]} onRemove={vi.fn()} onClearAll={vi.fn()} />,
    )
    expect(screen.queryByRole('button', { name: /clear all/i })).not.toBeInTheDocument()
    rerender(
      <PendingQueue queue={[item('a'), item('b')]} onRemove={vi.fn()} onClearAll={vi.fn()} />,
    )
    expect(screen.getByRole('button', { name: /clear all/i })).toBeInTheDocument()
  })

  it('caps the rail at MAX_PENDING (5) items', () => {
    // The queue never exceeds 5 (enforced by enqueuePending); render 5 and
    // assert exactly 5 remove buttons + the 5/5 label.
    const five = Array.from({ length: 5 }, (_, i) => item(`m${i}`))
    render(<PendingQueue queue={five} onRemove={vi.fn()} onClearAll={vi.fn()} />)
    expect(screen.getByText('Pending 5/5')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Remove Pending message/ })).toHaveLength(5)
  })

  it('calls onRemove with the chip index (chat.js:8459-8463)', () => {
    const onRemove = vi.fn()
    render(<PendingQueue queue={[item('a'), item('b')]} onRemove={onRemove} onClearAll={vi.fn()} />)
    const removeButtons = screen.getAllByRole('button', { name: /^Remove Pending message/ })
    fireEvent.click(removeButtons[1] as HTMLElement)
    expect(onRemove).toHaveBeenCalledWith(1)
  })

  it('calls onClearAll when Clear all is clicked (chat.js:8466-8471)', () => {
    const onClearAll = vi.fn()
    render(
      <PendingQueue queue={[item('a'), item('b')]} onRemove={vi.fn()} onClearAll={onClearAll} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /clear all/i }))
    expect(onClearAll).toHaveBeenCalledTimes(1)
  })
})
