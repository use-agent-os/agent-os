import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ElevatedPill } from './ElevatedPill'

describe('ElevatedPill', () => {
  it('renders the neutral "Approval prompts" label when no mode is set (chat.js:2339)', () => {
    render(<ElevatedPill sessionMode="" globalMode="" unavailable={false} onToggle={vi.fn()} />)
    const pill = screen.getByRole('button')
    expect(pill).toHaveTextContent('Approval prompts')
    expect(pill).not.toHaveClass('is-active')
  })

  it('shows the SESSION override in caps + is-active when a session mode is set (chat.js:2330-2331)', () => {
    render(
      <ElevatedPill sessionMode="bypass" globalMode="" unavailable={false} onToggle={vi.fn()} />,
    )
    const pill = screen.getByRole('button')
    expect(pill).toHaveTextContent('Session BYPASS')
    expect(pill).toHaveClass('is-active')
  })

  it('shows the GLOBAL default when only a global mode is set (chat.js:2334-2335)', () => {
    render(<ElevatedPill sessionMode="" globalMode="on" unavailable={false} onToggle={vi.fn()} />)
    const pill = screen.getByRole('button')
    expect(pill).toHaveTextContent('Global ON')
    expect(pill).toHaveClass('is-active')
  })

  it('renders the disabled "Bypass N/A" state when unavailable (chat.js:2316-2322)', () => {
    render(<ElevatedPill sessionMode="" globalMode="" unavailable={true} onToggle={vi.fn()} />)
    const pill = screen.getByRole('button')
    expect(pill).toHaveTextContent('Bypass N/A')
    expect(pill).toHaveAttribute('aria-disabled', 'true')
  })

  it('calls onToggle when clicked', () => {
    const onToggle = vi.fn()
    render(<ElevatedPill sessionMode="" globalMode="" unavailable={false} onToggle={onToggle} />)
    fireEvent.click(screen.getByRole('button'))
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('carries the danger tone gutter (design system .tone-*)', () => {
    render(
      <ElevatedPill sessionMode="bypass" globalMode="" unavailable={false} onToggle={vi.fn()} />,
    )
    expect(screen.getByRole('button').className).toMatch(/tone-/)
  })
})
