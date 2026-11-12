import '@/test/web'
import { readFileSync } from 'node:fs'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useTheme } from '@/stores/theme'
import { renderShell, resetShellState, sessionRow, shellRpc } from '@/test/web-shell'
import { buildActions, type ActionContext } from './actions'

const shellCss = readFileSync('src/web/styles/shell.css', 'utf8')

beforeEach(resetShellState)
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

const rows = [
  sessionRow('agent:main:webchat:dca', { display_name: 'Weekly DCA' }),
  sessionRow('agent:main:webchat:other', { display_name: 'Groceries' }),
]

async function openPalette(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('{Control>}k{/Control}')
  return screen.findByRole('dialog', { name: 'Command palette' })
}

describe('command palette', () => {
  it('jumps to a session with Ctrl+K, a fuzzy query and Enter', async () => {
    const user = userEvent.setup()
    const { router } = renderShell('/home', shellRpc(rows))
    await screen.findByText('Weekly DCA')
    const dialog = await openPalette(user)
    await user.type(within(dialog).getByRole('combobox'), 'weekly')
    await waitFor(() =>
      expect(within(dialog).getAllByRole('option')[0]).toHaveTextContent('Weekly DCA'),
    )
    await user.keyboard('{Enter}')
    expect(router.state.location.pathname).toBe('/chat/main/webchat/dca')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('scrolls the list back to the selected first result when the query changes', async () => {
    const user = userEvent.setup()
    renderShell('/home', shellRpc(rows))
    await screen.findByText('Weekly DCA')
    const dialog = await openPalette(user)
    const list = dialog.querySelector<HTMLElement>('[cmdk-list]')!
    // As if the user had arrowed far down a long list.
    list.scrollTop = 300
    await user.type(within(dialog).getByRole('combobox'), 'gro')
    await waitFor(() =>
      expect(within(dialog).getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true'),
    )
    await waitFor(() => expect(list.scrollTop).toBe(0))
  })

  it('marks the highlighted option with a 3:1 accent bar over a soft fill, not an outline', async () => {
    const user = userEvent.setup()
    renderShell('/home', shellRpc(rows))
    await screen.findByText('Weekly DCA')
    const dialog = await openPalette(user)
    const selected = await waitFor(() => {
      const option = within(dialog)
        .getAllByRole('option')
        .find((o) => o.getAttribute('aria-selected') === 'true')
      expect(option).toBeDefined()
      return option as HTMLElement
    })
    expect(selected).toHaveClass('palette-item')
    expect(selected.className).not.toMatch(/outline-ring/)
    // shell.css draws the selected row's leading bar in the ring colour (3:1).
    expect(shellCss).toMatch(/\.palette-item\[data-selected='true'\]::before \{\s*opacity: 1;/)
    expect(shellCss).toMatch(/\.palette-item::before \{[^}]*background: var\(--ring\);/)
    expect(within(dialog).getByRole('combobox').className).toMatch(/focus:border-ring/)
  })

  it('opens from the rail Search button', async () => {
    const user = userEvent.setup()
    renderShell('/home')
    await user.click(await screen.findByRole('button', { name: 'Search' }))
    expect(await screen.findByRole('dialog', { name: 'Command palette' })).toBeInTheDocument()
  })

  it('runs an action: switch to the light theme', async () => {
    const user = userEvent.setup()
    useTheme.getState().setPreference('dark')
    renderShell('/home')
    const dialog = await openPalette(user)
    await user.type(within(dialog).getByRole('combobox'), 'theme')
    await user.click(await within(dialog).findByRole('option', { name: /Switch to light theme/ }))
    expect(useTheme.getState().preference).toBe('light')
  })

  it('restores focus to the composer on Escape', async () => {
    const user = userEvent.setup()
    renderShell('/home')
    await screen.findByRole('navigation', { name: 'Main navigation' })
    const composer = document.createElement('textarea')
    composer.setAttribute('aria-label', 'Message')
    document.body.appendChild(composer)
    act(() => composer.focus())
    await openPalette(user)
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(composer).toHaveFocus()
    composer.remove()
  })

  it('shows a session search failure but keeps pages and actions', async () => {
    const user = userEvent.setup()
    const rpc = shellRpc().handle('sessions.list', () => {
      throw new Error('boom')
    })
    renderShell('/home', rpc)
    const dialog = await openPalette(user)
    expect(
      await within(dialog).findByText("Couldn't search sessions. Try again."),
    ).toBeInTheDocument()
    await user.type(within(dialog).getByRole('combobox'), 'inbox')
    expect(within(dialog).getByRole('option', { name: /^Inbox/ })).toBeInTheDocument()
    expect(within(dialog).getByRole('option', { name: /Open Inbox/ })).toBeInTheDocument()
    expect(within(dialog).getByText("Couldn't search sessions. Try again.")).toBeInTheDocument()
  })

  it('lists Compact as disabled with its reason while a turn is running', async () => {
    const user = userEvent.setup()
    renderShell(
      '/chat/main/webchat/dca',
      shellRpc([
        sessionRow('agent:main:webchat:dca', {
          display_name: 'Weekly DCA',
          active_task: { status: 'running' },
        }),
      ]),
    )
    await screen.findByText('Weekly DCA')
    const dialog = await openPalette(user)
    await user.type(within(dialog).getByRole('combobox'), 'compact')
    const option = await within(dialog).findByRole('option', { name: /Compact current session/ })
    expect(option).toHaveAttribute('aria-disabled', 'true')
    expect(option).toHaveTextContent('A turn is running in this session.')
  })
})

describe('actions registry', () => {
  const ctx = (patch: Partial<ActionContext> = {}): ActionContext => ({
    connection: 'connected',
    sessionKey: 'agent:main:webchat:a',
    sessionRunning: false,
    newSession: vi.fn(),
    toggleSidebar: vi.fn(),
    setTheme: vi.fn(),
    openShortcuts: vi.fn(),
    retryConnection: vi.fn(),
    openInbox: vi.fn(),
    compact: vi.fn(),
    exportSession: vi.fn(),
    ...patch,
  })

  it('includes every required action', () => {
    expect(buildActions(ctx()).map((a) => a.id)).toEqual([
      'new-session',
      'toggle-sidebar',
      'theme-system',
      'theme-light',
      'theme-dark',
      'shortcuts',
      'retry-connection',
      'inbox',
      'compact',
      'export',
    ])
  })

  it('gives disabled reasons from gate()', () => {
    const offline = buildActions(ctx({ connection: 'disconnected' }))
    expect(offline.find((a) => a.id === 'compact')!.gate.disabled).toBe(true)
    const noSession = buildActions(ctx({ sessionKey: null }))
    expect(noSession.find((a) => a.id === 'export')!.gate.reason).toBe('Open a session first.')
    const c = ctx()
    buildActions(c)
      .find((a) => a.id === 'compact')!
      .run()
    expect(c.compact).toHaveBeenCalledWith('agent:main:webchat:a')
  })
})
