import '@/test/web'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { renderShell, resetShellState, sessionRow, shellRpc } from '@/test/web-shell'
import { LIST_PANE_WIDTH_KEY, resetShellForTests, useShell } from './shell-store'

beforeEach(resetShellState)

function rail() {
  return screen.getByRole('navigation', { name: 'Main navigation' })
}

describe('navigation rail', () => {
  it('orders agent switcher, New session, Sessions, Pages, Search, then Home, Inbox, status', async () => {
    renderShell()
    await screen.findByRole('navigation', { name: 'Main navigation' })
    // The two rail menus load just after first paint.
    await within(rail()).findByRole('button', { name: 'Agent: main' })
    await within(rail()).findByRole('button', { name: /^Status and account/ })
    const names = [...rail().querySelectorAll('button, a')].map((el) =>
      el.getAttribute('aria-label'),
    )
    expect(names).toEqual([
      'Agent: main',
      'New session',
      'Sessions',
      'Pages',
      'Search',
      'Home',
      'Inbox',
      'Status and account: Connected',
    ])
  })

  it('labels icon buttons and shows a tooltip on keyboard focus', async () => {
    renderShell()
    const pages = await within(
      await screen.findByRole('navigation', { name: 'Main navigation' }),
    ).findByRole('button', { name: 'Pages' })
    act(() => pages.focus())
    expect(await screen.findByRole('tooltip')).toHaveTextContent('Pages')
  })

  it('opens /new from New session with the active agent preselected', async () => {
    const user = userEvent.setup()
    const { router } = renderShell()
    useShell.getState().chooseAgent('research')
    await user.click(await within(rail()).findByRole('button', { name: 'New session' }))
    expect(router.state.location.pathname).toBe('/new')
    expect(router.state.location.search).toBe('?agent=research')
  })

  it('switching agent filters Sessions with a visible, clearable filter', async () => {
    const user = userEvent.setup()
    renderShell(
      '/home',
      shellRpc([
        sessionRow('agent:main:webchat:a', { display_name: 'Main chat' }),
        sessionRow('agent:research:webchat:b', { display_name: 'Research chat' }),
      ]),
    )
    expect(await screen.findByText('Main chat')).toBeInTheDocument()
    await user.click(await within(rail()).findByRole('button', { name: 'Agent: main' }))
    await user.click(await screen.findByRole('menuitemradio', { name: 'research' }))
    await waitFor(() => expect(screen.queryByText('Main chat')).not.toBeInTheDocument())
    expect(screen.getByText('Research chat')).toBeInTheDocument()
    const filters = screen.getByRole('group', { name: 'Active filters' })
    expect(within(filters).getByText('Agent: research')).toBeInTheDocument()
    await user.click(within(filters).getByRole('button', { name: 'Clear filters' }))
    expect(await screen.findByText('Main chat')).toBeInTheDocument()
    // New session still preselects the chosen agent.
    expect(useShell.getState().activeAgent).toBe('research')
  })
})

describe('list pane', () => {
  it('collapses when the selected view is re-selected, keeping the rail', async () => {
    const user = userEvent.setup()
    renderShell()
    const sessions = await within(rail()).findByRole('button', { name: 'Sessions' })
    expect(sessions).toHaveAttribute('aria-pressed', 'true')
    await user.click(sessions)
    expect(screen.queryByTestId('list-pane')).not.toBeInTheDocument()
    expect(within(rail()).getByRole('button', { name: 'Sessions' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    expect(within(rail()).getByRole('button', { name: 'Pages' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    expect(rail()).toBeInTheDocument()
  })

  it('reopens on the last selected view with Ctrl+B', async () => {
    const user = userEvent.setup()
    renderShell()
    await user.click(await within(rail()).findByRole('button', { name: 'Pages' }))
    await user.keyboard('{Control>}b{/Control}')
    expect(screen.queryByTestId('list-pane')).not.toBeInTheDocument()
    await user.keyboard('{Control>}b{/Control}')
    expect(await screen.findByRole('navigation', { name: 'Pages' })).toBeInTheDocument()
  })

  it('remembers its width across a reload and starts expanded', async () => {
    renderShell()
    const handle = await screen.findByRole('separator', { name: 'Resize sidebar' })
    fireEvent.pointerDown(handle, { clientX: 288 })
    fireEvent.pointerMove(handle, { clientX: 320 })
    fireEvent.pointerUp(handle, { clientX: 320 })
    expect(localStorage.getItem(LIST_PANE_WIDTH_KEY)).toBe('320')
    useShell.setState({ paneOpen: false })
    // "Reload": fresh store state, same storage.
    resetShellForTests()
    expect(useShell.getState().paneWidth).toBe(320)
    expect(useShell.getState().paneOpen).toBe(true)
  })

  it('resizes from the keyboard within bounds', async () => {
    const user = userEvent.setup()
    renderShell()
    const handle = await screen.findByRole('separator', { name: 'Resize sidebar' })
    act(() => handle.focus())
    await user.keyboard('{End}')
    expect(handle).toHaveAttribute('aria-valuenow', '420')
    await user.keyboard('{Home}')
    expect(handle).toHaveAttribute('aria-valuenow', '240')
  })
})

describe('pages and titles', () => {
  it('lists every page with its description and marks the current one', async () => {
    const user = userEvent.setup()
    const { router } = renderShell('/home')
    await user.click(await within(rail()).findByRole('button', { name: 'Pages' }))
    const pages = await screen.findByRole('navigation', { name: 'Pages' })
    const links = within(pages).getAllByRole('link')
    expect(links.map((l) => l.querySelector('.font-medium')?.textContent)).toEqual([
      'Home',
      'Sessions',
      'Projects',
      'Usage',
      'Automations',
      'Inbox',
    ])
    expect(within(pages).getByText('Scheduled jobs and their runs')).toBeInTheDocument()
    await user.click(within(pages).getByRole('link', { name: /Automations/ }))
    expect(router.state.location.pathname).toBe('/automations')
    expect(within(pages).getByRole('link', { name: /Automations/ })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  it('titles the tab after the destination', async () => {
    renderShell('/usage')
    await waitFor(() => expect(document.title).toBe('Usage · AgentOS'))
  })

  it('titles a chat route after the session', async () => {
    renderShell(
      '/chat/main/webchat/dca',
      shellRpc([sessionRow('agent:main:webchat:dca', { display_name: 'Weekly DCA' })]),
    )
    await waitFor(() => expect(document.title).toBe('Weekly DCA · AgentOS'))
  })
})
