import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { RawProject } from '@/views/projects/logic'
import type { SessionRow } from '~/stores/sessions'
import { useUi } from '~/stores/ui'
import { ProjectFolders } from './ProjectFolders'

const rpcCall = vi.fn()
const rpc = { call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))

const PROJECT: RawProject = {
  project_id: 'p1',
  agent_id: 'main',
  name: 'Roadmap',
  knowledge: '',
  created_at: 1_700_000_000_000,
  updated_at: 1_700_000_100_000,
}

/** The route, as text, so a test can see where an action landed. */
function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>
}

function mount({ path = '/sessions', rows = [] }: { path?: string; rows?: SessionRow[] } = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  })
  render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <ProjectFolders
          projects={[PROJECT]}
          filed={{ byProject: new Map([['p1', rows]]), unfiled: [] }}
          loading={false}
        />
        <Where />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  // The count sits beside the link, with the "+", so the link is the name alone.
  return screen.getByRole('link', { name: 'Roadmap' })
}

function openMenu(link: HTMLElement) {
  fireEvent.contextMenu(link, { clientX: 40, clientY: 120 })
  return screen.getByRole('menu', { name: 'Project' })
}

beforeEach(() => {
  localStorage.clear()
  rpcCall.mockReset()
  useUi.setState({ openFolders: new Set(), creatingProject: false })
})

describe('ProjectFolders: the folder context menu', () => {
  it('opens at the pointer on right-click with what the project page offers', () => {
    const link = mount()
    // false: the handler took the event, so no native menu shows under ours.
    expect(fireEvent.contextMenu(link, { clientX: 40, clientY: 120 })).toBe(false)
    const menu = screen.getByRole('menu', { name: 'Project' })
    expect(menu.style.left).toBe('40px')
    expect(menu.style.top).toBe('120px')
    expect(
      within(menu)
        .getAllByRole('menuitem')
        .map((item) => item.textContent),
    ).toEqual(['New chat', 'Rename…', 'Delete…'])
    expect(link.closest('.proj-folder__row')).toHaveAttribute('data-menu', 'true')
  })

  it('also answers a right-click on the disclosure chevron', () => {
    mount()
    fireEvent.contextMenu(screen.getByRole('button', { name: 'Show chats' }))
    expect(screen.getByRole('menu', { name: 'Project' })).toBeInTheDocument()
  })

  it('opens under the row from Shift-F10 or the menu key and gives focus back on Escape', () => {
    const link = mount()
    vi.spyOn(link, 'getBoundingClientRect').mockReturnValue({
      left: 24,
      right: 204,
      top: 100,
      bottom: 128,
    } as DOMRect)
    link.focus()
    fireEvent.keyDown(link, { key: 'F10', shiftKey: true })
    const menu = screen.getByRole('menu', { name: 'Project' })
    expect(menu.style.left).toBe('24px')
    expect(menu.style.top).toBe('132px')
    expect(within(menu).getByRole('menuitem', { name: 'New chat' })).toHaveFocus()

    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(link).toHaveFocus()

    fireEvent.keyDown(link, { key: 'ContextMenu' })
    expect(screen.getByRole('menu', { name: 'Project' })).toBeInTheDocument()
  })

  it('renames in place: Return saves through projects.update, conflict-checked', async () => {
    rpcCall.mockResolvedValue({ project: { ...PROJECT, name: 'Launch plan' } })
    const link = mount()
    fireEvent.click(within(openMenu(link)).getByRole('menuitem', { name: 'Rename…' }))
    const field = screen.getByRole('textbox', { name: 'Project name' })
    expect(field).toHaveValue('Roadmap')
    expect(field).toHaveFocus()

    fireEvent.change(field, { target: { value: '  Launch plan ' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('projects.update', {
        projectId: 'p1',
        name: 'Launch plan',
        expectedUpdatedAt: 1_700_000_100_000,
      }),
    )
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.getByRole('link', { name: 'Roadmap' })).toHaveFocus()
  })

  it('Escape in the name field keeps the name', () => {
    const link = mount()
    fireEvent.click(within(openMenu(link)).getByRole('menuitem', { name: 'Rename…' }))
    const field = screen.getByRole('textbox', { name: 'Project name' })
    fireEvent.change(field, { target: { value: 'Something else' } })
    fireEvent.keyDown(field, { key: 'Escape' })
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(rpcCall).not.toHaveBeenCalled()
    expect(screen.getByRole('link', { name: 'Roadmap' })).toHaveFocus()
  })

  it('leaving the name field saves it and leaves focus where the user sent it', async () => {
    rpcCall.mockResolvedValue({ project: { ...PROJECT, name: 'Launch plan' } })
    const link = mount()
    fireEvent.click(within(openMenu(link)).getByRole('menuitem', { name: 'Rename…' }))
    const field = screen.getByRole('textbox', { name: 'Project name' })
    fireEvent.change(field, { target: { value: 'Launch plan' } })
    // A click or Tab elsewhere blurs the field first. Chromium lets React
    // commit before focus lands on what was clicked, and a focus() in that
    // commit cancels the move, so nothing may take focus at this point.
    act(() => field.blur())
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(document.activeElement).toBe(document.body)
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('projects.update', {
        projectId: 'p1',
        name: 'Launch plan',
        expectedUpdatedAt: 1_700_000_100_000,
      }),
    )
  })

  it('asks with the page alert before deleting; Cancel keeps the project', () => {
    const link = mount()
    fireEvent.click(within(openMenu(link)).getByRole('menuitem', { name: 'Delete…' }))
    const alert = screen.getByRole('alertdialog', { name: 'Delete this project?' })
    expect(alert).toHaveTextContent('Roadmap')
    fireEvent.click(within(alert).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(rpcCall).not.toHaveBeenCalled()
    expect(link).toHaveFocus()
  })

  it('deletes through projects.delete and stays put when its page is not on screen', async () => {
    rpcCall.mockResolvedValue({ deleted: true })
    const link = mount({ path: '/sessions/agent%3Amain%3As1' })
    fireEvent.click(within(openMenu(link)).getByRole('menuitem', { name: 'Delete…' }))
    const alert = screen.getByRole('alertdialog', { name: 'Delete this project?' })
    fireEvent.click(within(alert).getByRole('button', { name: 'Delete' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('projects.delete', { projectId: 'p1' }),
    )
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(screen.getByTestId('where').textContent).toBe('/sessions/agent%3Amain%3As1')
  })

  it('leaves the project page when the project on screen is deleted', async () => {
    rpcCall.mockResolvedValue({ deleted: true })
    const link = mount({ path: '/projects/p1' })
    fireEvent.click(within(openMenu(link)).getByRole('menuitem', { name: 'Delete…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/sessions'))
  })

  it('keeps the alert up when the delete fails', async () => {
    let fail: (err: Error) => void = () => {}
    rpcCall.mockImplementation(() => new Promise((_, reject) => (fail = reject)))
    const link = mount({ path: '/projects/p1' })
    fireEvent.click(within(openMenu(link)).getByRole('menuitem', { name: 'Delete…' }))
    const confirm = screen.getByRole('button', { name: 'Delete' })
    fireEvent.click(confirm)
    await waitFor(() => expect(confirm).toBeDisabled())
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('projects.delete', { projectId: 'p1' }),
    )

    fail(new Error('gateway busy'))
    await waitFor(() => expect(confirm).toBeEnabled())
    // A task later the failed delete has fully settled.
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)))
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
    expect(screen.getByTestId('where').textContent).toBe('/projects/p1')
  })

  it('New chat starts a chat filed in the project with its agent and opens it', async () => {
    // The folder's opening animation measures its height, then restores the
    // page scroll through window.scrollTo, which jsdom does not implement.
    vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
    rpcCall.mockResolvedValue({ key: 'agent:main:webchat:new' })
    // From a chat on screen: its composer stays mounted, so it does not take focus again.
    const link = mount({ path: '/sessions/agent%3Amain%3As1' })
    fireEvent.click(within(openMenu(link)).getByRole('menuitem', { name: 'New chat' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('sessions.create', { agentId: 'main', projectId: 'p1' }),
    )
    await waitFor(() =>
      expect(screen.getByTestId('where').textContent).toBe(
        `/sessions/${encodeURIComponent('agent:main:webchat:new')}`,
      ),
    )
    // The folder opens so the new chat shows under it.
    expect(useUi.getState().openFolders.has('p1')).toBe(true)
    // The keys are for the new chat: left on the row, Return would open the project page.
    expect(link).not.toHaveFocus()
  })

  it("leaves a filed chat's right-click to the chat's own menu", () => {
    useUi.setState({ openFolders: new Set(['p1']) })
    const row: SessionRow = {
      key: 'agent:main:webchat:s1',
      title: 'Filed chat',
      updatedAt: 0,
      live: false,
      raw: { key: 'agent:main:webchat:s1', project_id: 'p1' },
    }
    mount({ rows: [row] })
    fireEvent.contextMenu(screen.getByRole('link', { name: 'Filed chat' }))
    expect(screen.getAllByRole('menu')).toHaveLength(1)
    expect(screen.getByRole('menu', { name: 'Session' })).toBeInTheDocument()
  })
})

describe('ProjectFolders: the "+" on a folder row', () => {
  const FILED: SessionRow = {
    key: 'agent:main:webchat:s1',
    title: 'Filed chat',
    updatedAt: 0,
    live: false,
    raw: { key: 'agent:main:webchat:s1', project_id: 'p1' },
  }

  function plus() {
    return screen.getByRole('button', { name: 'New chat in Roadmap' })
  }

  it('sits beside the link, not inside it, just left of the count', () => {
    const link = mount({ rows: [FILED] })
    const button = plus()
    expect(link).not.toContainElement(button)
    expect(button.closest('.proj-folder__row')).toBe(link.closest('.proj-folder__row'))
    expect(button).toHaveAttribute('title', 'New chat in Roadmap')
    // Reachable with Tab.
    expect(button).not.toHaveAttribute('tabindex', '-1')
    const count = button.nextElementSibling
    expect(count).toHaveClass('proj-folder__count')
    expect(count).toHaveTextContent('1')
  })

  it("starts a chat in the project with its agent, opens it, and lets go of the row's focus", async () => {
    vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
    rpcCall.mockResolvedValue({ key: 'agent:main:webchat:new' })
    mount({ path: '/sessions/agent%3Amain%3As1' })
    const button = plus()
    button.focus()
    fireEvent.click(button)
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('sessions.create', { agentId: 'main', projectId: 'p1' }),
    )
    await waitFor(() =>
      expect(screen.getByTestId('where').textContent).toBe(
        `/sessions/${encodeURIComponent('agent:main:webchat:new')}`,
      ),
    )
    expect(useUi.getState().openFolders.has('p1')).toBe(true)
    // Left on the "+", Return in the new chat would start yet another one.
    expect(button).not.toHaveFocus()
  })

  it('neither opens the project page nor toggles the folder on the click itself', () => {
    rpcCall.mockImplementation(() => new Promise(() => {}))
    mount()
    fireEvent.click(plus())
    expect(screen.getByTestId('where').textContent).toBe('/sessions')
    expect(useUi.getState().openFolders.has('p1')).toBe(false)
  })

  it('creates one session for a quick double click', async () => {
    rpcCall.mockImplementation(() => new Promise(() => {}))
    mount()
    const button = plus()
    fireEvent.click(button)
    await waitFor(() => expect(button).toBeDisabled())
    fireEvent.click(button)
    expect(rpcCall.mock.calls.filter(([method]) => method === 'sessions.create')).toHaveLength(1)
  })

  it('names the project verbatim, "$" and all', () => {
    const project = { ...PROJECT, project_id: 'p9', name: "Cash $$ plan $& x$'y" }
    const client = new QueryClient()
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <ProjectFolders
            projects={[project]}
            filed={{ byProject: new Map(), unfiled: [] }}
            loading={false}
          />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    expect(
      screen.getByRole('button', { name: "New chat in Cash $$ plan $& x$'y" }),
    ).toBeInTheDocument()
  })

  it('goes away while the folder is being renamed', () => {
    const link = mount({ rows: [FILED] })
    fireEvent.click(within(openMenu(link)).getByRole('menuitem', { name: 'Rename…' }))
    expect(screen.queryByRole('button', { name: 'New chat in Roadmap' })).toBeNull()
  })
})

describe('ProjectFolders: collapse / expand all', () => {
  const SECOND: RawProject = { ...PROJECT, project_id: 'p2', name: 'Launch' }

  function mountTwo(projects: RawProject[] = [PROJECT, SECOND]) {
    render(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient()}>
          <ProjectFolders
            projects={projects}
            filed={{ byProject: new Map(), unfiled: [] }}
            loading={false}
          />
        </QueryClientProvider>
      </MemoryRouter>,
    )
  }

  it('folds every open folder shut, then opens them all again', () => {
    useUi.setState({ openFolders: new Set(['p1']) })
    mountTwo()
    fireEvent.click(screen.getByRole('button', { name: 'Collapse all projects' }))
    expect(useUi.getState().openFolders.size).toBe(0)
    expect(screen.getAllByRole('button', { name: 'Show chats' })).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: 'Expand all projects' }))
    expect([...useUi.getState().openFolders].sort()).toEqual(['p1', 'p2'])
    expect(screen.getByRole('button', { name: 'Collapse all projects' })).toBeInTheDocument()
  })

  it('offers expand when every folder is shut', () => {
    mountTwo()
    expect(screen.getByRole('button', { name: 'Expand all projects' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Collapse all projects' })).toBeNull()
  })

  it('ignores an open id left over from a deleted project', () => {
    useUi.setState({ openFolders: new Set(['gone']) })
    mountTwo()
    expect(screen.getByRole('button', { name: 'Expand all projects' })).toBeInTheDocument()
  })

  it('remembers the result across a relaunch', () => {
    mountTwo()
    fireEvent.click(screen.getByRole('button', { name: 'Expand all projects' }))
    expect(JSON.parse(localStorage.getItem('agentos-desktop.openFolders') ?? '[]').sort()).toEqual([
      'p1',
      'p2',
    ])
  })

  it('is not there without projects', () => {
    mountTwo([])
    expect(screen.queryByRole('button', { name: /all projects/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'New project' })).toBeInTheDocument()
  })
})

describe('ProjectFolders: "N more" in a long folder', () => {
  const MANY: SessionRow[] = Array.from({ length: 13 }, (_, i) => ({
    key: `agent:main:webchat:s${i}`,
    title: `Chat ${i}`,
    updatedAt: 0,
    live: false,
    raw: { key: `agent:main:webchat:s${i}`, project_id: 'p1' },
  }))

  beforeEach(() => {
    useUi.setState({ openFolders: new Set(['p1']), expandedFolders: new Set() })
  })

  it('shows the older chats in place, without leaving the page', () => {
    mount({ rows: MANY })
    expect(screen.queryByRole('link', { name: 'Chat 12' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '7 more' }))
    expect(screen.getByRole('link', { name: 'Chat 12' })).toBeInTheDocument()
    expect(screen.getByTestId('where')).toHaveTextContent('/sessions')
    expect(screen.queryByRole('button', { name: '7 more' })).toBeNull()
  })

  it('folds back to the preview with "Show less"', () => {
    mount({ rows: MANY })
    fireEvent.click(screen.getByRole('button', { name: '7 more' }))
    fireEvent.click(screen.getByRole('button', { name: 'Show less' }))
    expect(screen.queryByRole('link', { name: 'Chat 12' })).toBeNull()
    expect(screen.getByRole('button', { name: '7 more' })).toBeInTheDocument()
  })

  it('offers neither control when everything fits', () => {
    mount({ rows: MANY.slice(0, 6) })
    expect(screen.queryByRole('button', { name: /more$/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Show less' })).toBeNull()
  })
})
