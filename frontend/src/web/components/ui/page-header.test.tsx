import '@/test/web'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PageHeader } from './page-header'

describe('PageHeader', () => {
  it('stacks on phones and puts the actions beside the title from sm up', () => {
    render(
      <PageHeader
        title="Home"
        description="How the runtime is doing right now."
        actions={<button type="button">Refresh</button>}
        note={<span>Offline</span>}
      />,
    )
    const header = screen.getByRole('heading', { level: 1, name: 'Home' }).closest('header')!
    // At 360 px a side-by-side row squeezed the description into a narrow column.
    const classes = header.className.split(/\s+/)
    expect(classes).toContain('flex-col')
    expect(classes).toContain('sm:flex-row')
    expect(classes).not.toContain('flex-row')
    const description = screen.getByText('How the runtime is doing right now.')
    expect(description.parentElement?.className).not.toMatch(/(^|\s)flex-1(\s|$)/)
    const actions = screen.getByRole('button', { name: 'Refresh' }).parentElement!.parentElement!
    expect(actions.className).toMatch(/(^|\s)items-start(\s|$)/)
    expect(actions.className).toMatch(/(^|\s)sm:items-end(\s|$)/)
    expect(screen.getByText('Offline').parentElement).toBe(actions)
  })

  it('leaves out the empty parts', () => {
    render(<PageHeader title="Sessions" />)
    const header = screen.getByRole('heading', { name: 'Sessions' }).closest('header')!
    expect(header.querySelector('[data-page-header-description]')).toBeNull()
    expect(header.children).toHaveLength(1)
  })
})
