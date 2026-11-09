import '@/test/web'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Button } from './button'
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from './dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from './dropdown-menu'
import { EmptyState, ErrorState, Skeleton, StaleBadge } from './states'
import { SimpleTooltip, TooltipProvider } from './tooltip'

afterEach(() => {
  vi.useRealTimers()
})

function DemoDialog() {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button>Open</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogTitle>Delete session</DialogTitle>
        <DialogDescription>This cannot be undone.</DialogDescription>
        <Button>First</Button>
        <Button>Second</Button>
      </DialogContent>
    </Dialog>
  )
}

describe('Dialog', () => {
  it('has the dialog role and name, traps focus, closes on Escape and returns focus', async () => {
    const user = userEvent.setup()
    render(<DemoDialog />)
    const trigger = screen.getByRole('button', { name: 'Open' })
    await user.click(trigger)

    const dialog = await screen.findByRole('dialog', { name: 'Delete session' })
    expect(dialog).toContainElement(document.activeElement as HTMLElement)

    // Tab cycles inside: First, Second, Close, then back to First.
    for (let i = 0; i < 5; i++) {
      await user.tab()
      expect(dialog).toContainElement(document.activeElement as HTMLElement)
    }

    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('offers a named close button', async () => {
    const user = userEvent.setup()
    render(<DemoDialog />)
    await user.click(screen.getByRole('button', { name: 'Open' }))
    await user.click(await screen.findByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})

describe('DropdownMenu', () => {
  it('moves with arrow keys, Home and End, activates with Enter, closes on Escape', async () => {
    const user = userEvent.setup()
    const onRename = vi.fn()
    render(
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button>Actions</Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem>Pin</DropdownMenuItem>
          <DropdownMenuItem onSelect={onRename}>Rename</DropdownMenuItem>
          <DropdownMenuItem variant="danger">Delete</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>,
    )
    const trigger = screen.getByRole('button', { name: 'Actions' })
    trigger.focus()
    await user.keyboard('{Enter}')
    const menu = await screen.findByRole('menu')
    expect(menu).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('menuitem', { name: 'Pin' })).toHaveFocus())

    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('menuitem', { name: 'Rename' })).toHaveFocus()
    await user.keyboard('{End}')
    expect(screen.getByRole('menuitem', { name: 'Delete' })).toHaveFocus()
    await user.keyboard('{Home}')
    expect(screen.getByRole('menuitem', { name: 'Pin' })).toHaveFocus()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()

    await user.keyboard('{Enter}')
    await screen.findByRole('menu')
    await user.keyboard('{ArrowDown}{Enter}')
    expect(onRename).toHaveBeenCalledOnce()
  })
})

describe('Tooltip', () => {
  it('appears on keyboard focus and dismisses with Escape', async () => {
    const user = userEvent.setup()
    render(
      <TooltipProvider delayDuration={0}>
        <SimpleTooltip label="New session">
          <Button aria-label="New session">+</Button>
        </SimpleTooltip>
      </TooltipProvider>,
    )
    await user.tab()
    expect(screen.getByRole('button', { name: 'New session' })).toHaveFocus()
    expect(await screen.findByRole('tooltip')).toHaveTextContent('New session')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('tooltip')).not.toBeInTheDocument())
  })
})

describe('view states', () => {
  it('shows the skeleton only after 300 ms, while always reserving its box', () => {
    vi.useFakeTimers()
    const { container } = render(<Skeleton className="h-6 w-24" />)
    const block = container.querySelector('[data-slot="skeleton"]')!
    expect(block).toHaveClass('h-6', 'w-24', 'opacity-0')
    act(() => vi.advanceTimersByTime(299))
    expect(block).not.toHaveAttribute('data-visible')
    act(() => vi.advanceTimersByTime(1))
    expect(block).toHaveAttribute('data-visible')
    expect(block).toHaveClass('opacity-100')
  })

  it('renders an empty state with its primary action', () => {
    render(
      <EmptyState
        title="No automations yet"
        description="Scheduled jobs appear here."
        action={<Button>New automation</Button>}
      />,
    )
    expect(screen.getByRole('heading', { name: 'No automations yet' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New automation' })).toBeInTheDocument()
  })

  it('keeps earlier data visible beneath an error notice with Retry', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn()
    render(
      <ErrorState message="Usage refresh failed" onRetry={onRetry}>
        <table aria-label="Usage">
          <tbody>
            <tr>
              <td>$1.20</td>
            </tr>
          </tbody>
        </table>
      </ErrorState>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Usage refresh failed')
    expect(screen.getByRole('table', { name: 'Usage' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it('marks snapshot data as stale in words, not only colour', () => {
    render(<StaleBadge since="10:42" />)
    expect(screen.getByText('Snapshot from 10:42')).toBeInTheDocument()
  })
})
