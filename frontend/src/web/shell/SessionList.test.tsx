import '@/test/web'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { testQueryClient, WebTestProviders } from '@/test/render-web'
import { renderShell, resetShellState, sessionRow, shellRpc } from '@/test/web-shell'
import { webRoutes } from '@/web/app/WebApp'
import { createSessionOptimistic, SESSIONS_REFRESH_MS } from './session-data'
import { PINS_KEY } from './session-list'

beforeEach(resetShellState)
afterEach(() => vi.useRealTimers())

const A = 'agent:main:webchat:a'
const B = 'agent:main:webchat:b'

function list() {
  return screen.getByRole('region', { name: 'Sessions and pages' })
}

async function openRowMenu(user: ReturnType<typeof userEvent.setup>, title: string) {
  await user.click(await screen.findByRole('button', { name: `Actions for ${title}` }))
  return screen.findByRole('menu')
}

describe('session rows', () => {
  it('renders rows as links to the chat URL, with title, channel and running state', async () => {
    renderShell(
      '/home',
      shellRpc([
        sessionRow(A, { display_name: 'Weekly DCA', derived_title: 'Set up a DCA for ETH' }),
        sessionRow('agent:main:telegram:dm', {
          channel: 'telegram',
          active_task: { status: 'running' },
        }),
      ]),
    )
    const link = await screen.findByRole('link', { name: /Weekly DCA/ })
    expect(link).toHaveAttribute('href', '/chat/main/webchat/a')
    const tg = screen.getByRole('link', { name: /telegram · dm/ })
    expect(within(tg).getByRole('img', { name: 'Channel: telegram' })).toBeInTheDocument()
    expect(within(tg).getByRole('img', { name: 'Running' })).toBeInTheDocument()
  })

  it('leaves modifier and middle clicks to the browser (new tab)', async () => {
    const { router } = renderShell('/home', shellRpc([sessionRow(A, { display_name: 'Alpha' })]))
    const link = await screen.findByRole('link', { name: /Alpha/ })
    fireEvent.click(link, { ctrlKey: true })
    fireEvent.click(link, { button: 1 })
    expect(router.state.location.pathname).toBe('/home')
  })

  it('nests a child under its parent with a count while collapsed', async () => {
    const user = userEvent.setup()
    renderShell(
      '/home',
      shellRpc([
        sessionRow(A, { display_name: 'Parent' }),
        sessionRow(B, { display_name: 'Child', parentSessionKey: A }),
      ]),
    )
    const parent = await screen.findByRole('link', { name: /Parent/ })
    expect(within(parent).getByText('1 child session')).toBeInTheDocument()
    expect(screen.queryByText('Child')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Show child sessions' }))
    expect(screen.getByRole('link', { name: /Child/ })).toBeInTheDocument()
  })
})

describe('filters and grouping', () => {
  const rows = [
    sessionRow(A, { display_name: 'Weekly DCA' }),
    sessionRow(B, { display_name: 'Groceries', updated_at: Date.now() - 40 * 86_400_000 }),
  ]

  it('filters by text and offers Clear filters when nothing matches', async () => {
    const user = userEvent.setup()
    renderShell('/home', shellRpc(rows))
    const input = await screen.findByRole('searchbox', { name: 'Filter sessions' })
    await user.type(input, 'dca')
    expect(screen.getByText('Weekly DCA')).toBeInTheDocument()
    expect(screen.queryByText('Groceries')).not.toBeInTheDocument()
    await user.clear(input)
    await user.type(input, 'zzz')
    expect(await screen.findByText('No sessions match')).toBeInTheDocument()
    expect(screen.getAllByText('Text: zzz').length).toBeGreaterThan(0)
    const empty = screen
      .getByText('No sessions match')
      .closest('[data-slot="empty-state"]') as HTMLElement
    await user.click(within(empty).getByRole('button', { name: 'Clear filters' }))
    expect(await screen.findByText('Groceries')).toBeInTheDocument()
  })

  it('groups by date by default and by project on request, persisted', async () => {
    const user = userEvent.setup()
    renderShell(
      '/home',
      shellRpc([
        ...rows,
        sessionRow('agent:main:webchat:c', { display_name: 'Proj', project_id: 'p1' }),
      ]),
    )
    await screen.findByText('Weekly DCA')
    const headings = () =>
      within(list())
        .getAllByRole('heading', { level: 3 })
        .map((h) => h.textContent)
    expect(headings()).toEqual(['Today', 'Older'])
    await user.click(screen.getByRole('button', { name: 'Group sessions' }))
    await user.click(await screen.findByRole('menuitemradio', { name: 'By project' }))
    await waitFor(() => expect(headings()).toEqual(['Alpha', 'No project']))
    expect(localStorage.getItem('agentos.ui.sessionGrouping')).toBe('project')
  })
})

describe('row actions', () => {
  it('opens a row in a new tab at the full console path, keeping this tab’s session', async () => {
    const user = userEvent.setup()
    const opened = { opener: window as Window | null }
    const open = vi.spyOn(window, 'open').mockReturnValue(opened as unknown as Window)
    const router = createMemoryRouter(webRoutes, {
      basename: '/control',
      initialEntries: ['/control/home'],
    })
    render(
      <WebTestProviders
        rpc={shellRpc([sessionRow(A, { display_name: 'Alpha' })])}
        queryClient={testQueryClient()}
      >
        <RouterProvider router={router} />
      </WebTestProviders>,
    )
    const menu = await openRowMenu(user, 'Alpha')
    await user.click(within(menu).getByRole('menuitem', { name: 'Open in new tab' }))
    expect(open).toHaveBeenCalledWith('/control/chat/main/webchat/a', '_blank')
    // No `noopener` feature (which would start the tab without this tab's
    // sessionStorage token); the opener link is cut after the fact.
    expect(opened.opener).toBeNull()
    open.mockRestore()
  })

  it('resets a session only after the confirmation', async () => {
    const user = userEvent.setup()
    const rpc = shellRpc([sessionRow(A, { display_name: 'Alpha' })]).handle('sessions.reset', {
      ok: true,
    })
    renderShell('/home', rpc)
    let menu = await openRowMenu(user, 'Alpha')
    await user.click(within(menu).getByRole('menuitem', { name: 'Reset' }))
    let dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Reset “Alpha”?')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(rpc.callsTo('sessions.reset')).toEqual([])
    menu = await openRowMenu(user, 'Alpha')
    await user.click(within(menu).getByRole('menuitem', { name: 'Reset' }))
    dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Reset session' }))
    await waitFor(() => expect(rpc.callsTo('sessions.reset')).toEqual([{ key: A }]))
  })

  it('renames through sessions.rename and shows the new title', async () => {
    const user = userEvent.setup()
    const sessions = [sessionRow(A, { display_name: 'Old' })]
    const rpc = shellRpc()
      .handle('sessions.list', () => ({ sessions: structuredClone(sessions) }))
      .handle('sessions.rename', (p: Record<string, unknown>) => {
        sessions[0]!.display_name = p.name
        return { ok: true }
      })
    renderShell('/home', rpc)
    const menu = await openRowMenu(user, 'Old')
    await user.click(within(menu).getByRole('menuitem', { name: 'Rename' }))
    const input = screen.getByRole('textbox', { name: 'Session name' })
    await user.clear(input)
    await user.type(input, 'Research notes{Enter}')
    await waitFor(() =>
      expect(rpc.callsTo('sessions.rename')).toEqual([{ key: A, name: 'Research notes' }]),
    )
    expect(await screen.findByRole('link', { name: /Research notes/ })).toBeInTheDocument()
  })

  it('keeps a failed rename local to that row', async () => {
    const user = userEvent.setup()
    const rpc = shellRpc([
      sessionRow(A, { display_name: 'First' }),
      sessionRow(B, { display_name: 'Second' }),
    ]).handle('sessions.rename', () => {
      throw new Error('name taken')
    })
    renderShell('/home', rpc)
    const menu = await openRowMenu(user, 'First')
    await user.click(within(menu).getByRole('menuitem', { name: 'Rename' }))
    await user.type(screen.getByRole('textbox', { name: 'Session name' }), 'X{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't rename: name taken")
    expect(screen.getByRole('textbox', { name: 'Session name' })).toHaveAttribute(
      'aria-invalid',
      'true',
    )
    expect(screen.getByRole('link', { name: /Second/ })).toBeInTheDocument()
  })

  it('asks before deleting, names the session, and returns focus on cancel', async () => {
    const user = userEvent.setup()
    const rpc = shellRpc([sessionRow(A, { display_name: 'Doomed' })]).handle('sessions.delete', {
      ok: true,
    })
    renderShell('/home', rpc)
    const menu = await openRowMenu(user, 'Doomed')
    await user.click(within(menu).getByRole('menuitem', { name: 'Delete' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Delete “Doomed”?' })
    expect(rpc.callsTo('sessions.delete')).toEqual([])
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Actions for Doomed' })).toHaveFocus()
    void dialog

    await user.click(screen.getByRole('button', { name: 'Actions for Doomed' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Delete' }))
    await user.click(await screen.findByRole('button', { name: 'Delete session' }))
    await waitFor(() => expect(rpc.callsTo('sessions.delete')).toEqual([{ key: A }]))
  })

  it('opens the menu with Shift+F10 on a focused row', async () => {
    const user = userEvent.setup()
    renderShell('/home', shellRpc([sessionRow(A, { display_name: 'Keyboard' })]))
    const link = await screen.findByRole('link', { name: /Keyboard/ })
    act(() => link.focus())
    await user.keyboard('{Shift>}{F10}{/Shift}')
    expect(await screen.findByRole('menu')).toBeInTheDocument()
  })

  it('pins A then B, moves B above A, and keeps the order', async () => {
    const user = userEvent.setup()
    renderShell(
      '/home',
      shellRpc([sessionRow(A, { display_name: 'Alpha' }), sessionRow(B, { display_name: 'Beta' })]),
    )
    for (const title of ['Alpha', 'Beta']) {
      const menu = await openRowMenu(user, title)
      await user.click(within(menu).getByRole('menuitem', { name: 'Pin' }))
    }
    const menu = await openRowMenu(user, 'Beta')
    await user.click(within(menu).getByRole('menuitem', { name: 'Move up' }))
    const pinned = screen.getByRole('region', { name: 'Pinned' })
    expect(
      within(pinned)
        .getAllByRole('link')
        .map((l) => l.textContent),
    ).toEqual([expect.stringContaining('Beta'), expect.stringContaining('Alpha')])
    expect(JSON.parse(localStorage.getItem(PINS_KEY)!)).toEqual([B, A])
  })

  it('hides a pin whose session is gone but keeps it stored', async () => {
    localStorage.setItem(PINS_KEY, JSON.stringify(['agent:main:webchat:gone', A]))
    renderShell('/home', shellRpc([sessionRow(A, { display_name: 'Alive' })]))
    const pinned = await screen.findByRole('region', { name: 'Pinned' })
    expect(within(pinned).getAllByRole('link')).toHaveLength(1)
    expect(JSON.parse(localStorage.getItem(PINS_KEY)!)).toHaveLength(2)
  })
})

describe('live session list', () => {
  it('shows a session created elsewhere within 5 s', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const sessions: Record<string, unknown>[] = [sessionRow(A, { display_name: 'Existing' })]
    const rpc = shellRpc().handle('sessions.list', () => ({ sessions: structuredClone(sessions) }))
    renderShell('/home', rpc)
    await screen.findByText('Existing')
    sessions.push(
      sessionRow('agent:main:telegram:new', { display_name: 'From Telegram', channel: 'telegram' }),
    )
    await act(() => vi.advanceTimersByTimeAsync(SESSIONS_REFRESH_MS))
    expect(await screen.findByText('From Telegram')).toBeInTheDocument()
  })

  it('refreshes at once on sessions.changed', async () => {
    const sessions: Record<string, unknown>[] = []
    const rpc = shellRpc().handle('sessions.list', () => ({ sessions: structuredClone(sessions) }))
    renderShell('/home', rpc)
    await screen.findByText('No sessions yet')
    sessions.push(sessionRow(A, { display_name: 'Pushed' }))
    act(() => rpc.emit('sessions.changed', {}))
    expect(await screen.findByText('Pushed')).toBeInTheDocument()
  })

  it('shows a provisional row at once and removes it when creation fails', async () => {
    let reject: (e: Error) => void = () => {}
    const rpc = shellRpc().handle(
      'sessions.create',
      () => new Promise((_, r) => (reject = r as (e: Error) => void)),
    )
    const { queryClient } = renderShell('/home', rpc)
    await screen.findByText('No sessions yet')
    let outcome: Promise<string> = Promise.resolve('')
    act(() => {
      outcome = createSessionOptimistic(rpc.asClient(), queryClient, {
        agentId: 'main',
        displayName: 'Brand new',
      })
    })
    expect(await screen.findByText('Brand new')).toBeInTheDocument()
    expect(screen.getByText('Creating…')).toBeInTheDocument()
    await act(async () => {
      reject(new Error('boom'))
      await outcome.catch(() => {})
    })
    await waitFor(() => expect(screen.queryByText('Brand new')).not.toBeInTheDocument())
  })
})
