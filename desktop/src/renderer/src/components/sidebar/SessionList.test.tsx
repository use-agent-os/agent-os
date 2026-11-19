import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useSessionMarks } from '~/stores/session-marks'
import { useSessionSelection } from '~/stores/session-selection'
import { useUi } from '~/stores/ui'
import { SessionList } from './SessionList'

const rpcCall = vi.fn<(...args: unknown[]) => Promise<object>>(async () => ({}))
const rpc = { call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))

const NOW = Date.now()

function raw(key: string, title: string, project?: string, pinned = false) {
  return {
    key,
    derived_title: title,
    updated_at: NOW,
    ...(project ? { project_id: project } : {}),
    ...(pinned ? { pinned: true } : {}),
  }
}

/**
 * The list as the gateway would return it, seeded into the query cache: with
 * no connection the queries stay idle and SessionList renders these.
 * Folder "Roadmap": [f1, f2]. Loose: [a, b].
 */
function mount(pinned: readonly string[] = []) {
  const on = (key: string) => pinned.includes(key)
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: Infinity } },
  })
  client.setQueryData(['projects'], {
    projects: [{ project_id: 'p1', name: 'Roadmap', agent_id: 'main' }],
  })
  client.setQueryData(['sessions'], {
    sessions: [
      raw('k:f1', 'Folder one', 'p1', on('k:f1')),
      raw('k:f2', 'Folder two', 'p1', on('k:f2')),
      raw('k:a', 'Loose A', undefined, on('k:a')),
      raw('k:b', 'Loose B', undefined, on('k:b')),
    ],
  })
  render(
    <MemoryRouter initialEntries={['/sessions']}>
      <QueryClientProvider client={client}>
        <SessionList />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** A row by its title (the link's name also carries the age). */
function link(title: string) {
  return screen.getByTitle(title)
}

function selected(): string[] {
  return [...useSessionSelection.getState().keys].sort()
}

beforeEach(() => {
  localStorage.clear()
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  useUi.setState({ openFolders: new Set(['p1']), sessionQuery: '', creatingProject: false })
  useSessionMarks.setState({ pinned: new Set(), archived: new Set(), unread: new Set() })
  rpcCall.mockClear()
  useSessionSelection.setState({
    visible: [],
    order: [],
    keys: new Set(),
    anchor: null,
    pendingDelete: null,
  })
})

describe('SessionList: what a selection can reach', () => {
  it('Shift-click ranges from a chat in an open folder into the loose list', () => {
    mount()
    fireEvent.click(link('Folder two'), { metaKey: true })
    fireEvent.click(link('Loose B'), { shiftKey: true })
    expect(selected()).toEqual(['k:a', 'k:b', 'k:f2'])
  })

  it('closing the folder deselects the chats it hides', () => {
    mount()
    fireEvent.click(link('Folder one'), { metaKey: true })
    fireEvent.click(link('Loose A'), { metaKey: true })
    act(() => useUi.getState().toggleFolder('p1'))
    expect(selected()).toEqual(['k:a'])
  })

  it('a search that hides a selected row deselects it', () => {
    mount()
    fireEvent.click(link('Loose A'), { metaKey: true })
    fireEvent.click(link('Loose B'), { metaKey: true })
    act(() => useUi.getState().setSessionQuery('Loose B'))
    expect(selected()).toEqual(['k:b'])
  })

  it('offers the batch delete from the list, once, for the whole selection', () => {
    mount()
    fireEvent.click(link('Folder one'), { metaKey: true })
    fireEvent.click(link('Loose A'), { metaKey: true })
    fireEvent.contextMenu(link('Loose A'))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete 2 sessions…' }))
    expect(screen.getAllByRole('alertdialog')).toHaveLength(1)
    expect(screen.getByRole('alertdialog', { name: 'Delete 2 sessions?' })).toBeInTheDocument()
  })
})

describe('SessionList: the Pinned block', () => {
  const pinned = () => screen.queryByRole('region', { name: 'Pinned' })
  /** Every row title on screen, top to bottom. */
  const titles = () =>
    screen
      .getAllByRole('link')
      .map((el) => el.getAttribute('title') ?? '')
      .filter((title) => title !== 'Roadmap')

  it('is absent until something is pinned', () => {
    mount()
    expect(pinned()).toBeNull()
  })

  it('sits above the project folders and takes its rows out of the list below', () => {
    mount(['k:b'])
    expect(within(pinned()!).getByTitle('Loose B')).toBeInTheDocument()
    const region = pinned()!
    const projects = screen.getByText('Projects')
    expect(region.compareDocumentPosition(projects) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // Once, on top; not again under "Sessions".
    expect(titles()).toEqual(['Loose B', 'Folder one', 'Folder two', 'Loose A'])
  })

  it('shows a pinned chat that is filed in a project both on top and in its folder', () => {
    mount(['k:f2'])
    expect(titles()).toEqual(['Folder two', 'Folder one', 'Folder two', 'Loose A', 'Loose B'])
  })

  it('right-click → Pin asks the gateway and moves the row up at once', async () => {
    mount()
    fireEvent.contextMenu(link('Loose A'))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Pin' }))
    await waitFor(() => expect(pinned()).not.toBeNull())
    expect(within(pinned()!).getByTitle('Loose A')).toBeInTheDocument()
    expect(rpcCall).toHaveBeenCalledWith('sessions.pin', { key: 'k:a', pinned: true })
  })

  it('right-click → Unpin on a pinned row takes it back out', async () => {
    mount(['k:a'])
    fireEvent.contextMenu(within(pinned()!).getByTitle('Loose A'))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Unpin' }))
    await waitFor(() => expect(pinned()).toBeNull())
    expect(rpcCall).toHaveBeenCalledWith('sessions.pin', { key: 'k:a', pinned: false })
  })

  it('is part of what a Shift-click range runs over, from the top', () => {
    mount(['k:b'])
    fireEvent.click(within(pinned()!).getByTitle('Loose B'), { metaKey: true })
    fireEvent.click(link('Folder two'), { shiftKey: true })
    expect(selected()).toEqual(['k:b', 'k:f1', 'k:f2'])
  })

  it('a search narrows the block like the rest of the list', () => {
    mount(['k:a', 'k:b'])
    act(() => useUi.getState().setSessionQuery('Loose B'))
    expect(titles()).toEqual(['Loose B'])
  })
})

describe('SessionList: pins an older build kept on this Mac', () => {
  const pinCalls = () => rpcCall.mock.calls.filter(([method]) => method === 'sessions.pin')

  it('hands them to the gateway once and forgets them, dropping ones whose session is gone', async () => {
    useSessionMarks.setState({ pinned: new Set(['k:a', 'k:gone']) })
    mount()
    await waitFor(() => expect(useSessionMarks.getState().pinned.size).toBe(0))
    expect(pinCalls()).toEqual([['sessions.pin', { key: 'k:a', pinned: true }]])
  })

  it('does not re-pin one the gateway already has, and keeps one it refused for next time', async () => {
    rpcCall.mockImplementation(async (method: unknown, params: unknown) => {
      if (method === 'sessions.pin' && (params as { key: string }).key === 'k:b') {
        throw new Error('offline')
      }
      return {}
    })
    useSessionMarks.setState({ pinned: new Set(['k:a', 'k:b']) })
    mount(['k:a'])
    await waitFor(() => expect(useSessionMarks.getState().pinned.has('k:a')).toBe(false))
    expect(pinCalls()).toEqual([['sessions.pin', { key: 'k:b', pinned: true }]])
    expect([...useSessionMarks.getState().pinned]).toEqual(['k:b'])
    rpcCall.mockImplementation(async () => ({}))
  })
})
