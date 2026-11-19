import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useSessionMarks } from '~/stores/session-marks'
import { useSessionSelection } from '~/stores/session-selection'
import type { SessionRow } from '~/stores/sessions'
import { deleteOutcome } from './session-actions'
import { SessionBulkDelete } from './SessionBulk'
import { SessionRowLink, sessionPath } from './SessionRow'

const rpcCall = vi.fn()
const rpc = { call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

function row(key: string, extra: Partial<SessionRow> = {}): SessionRow {
  return { key, title: `Chat ${key}`, updatedAt: 0, live: false, raw: { key }, ...extra }
}

const A = row('agent:main:webchat:a')
const B = row('agent:main:webchat:b')
const C = row('agent:main:webchat:c')
const D = row('agent:main:webchat:d')
const E = row('agent:main:webchat:e')
const ROWS = [A, B, C, D, E]

/** The route, as text, so a test can see where a click landed. */
function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>
}

/** The rows as the sidebar lists them; SessionList publishes the same order. */
function mount({ rows = ROWS, path = '/sessions' }: { rows?: SessionRow[]; path?: string } = {}) {
  useSessionSelection.getState().setVisible(rows.map((r) => ({ row: r, nested: false })))
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  })
  render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        {rows.map((r) => (
          <SessionRowLink key={r.key} row={r} />
        ))}
        <SessionBulkDelete />
        <Where />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

function link(r: SessionRow) {
  // A pinned row's name starts with its "Pinned" glyph.
  return screen.getByRole('link', { name: (name) => name.endsWith(r.title) })
}

function where() {
  return screen.getByTestId('where').textContent
}

function selected(): string[] {
  return [...useSessionSelection.getState().keys].sort()
}

/** Answer `sessions.delete` with `reply`; everything else (the project list) with nothing. */
function gateway(reply: unknown) {
  rpcCall.mockImplementation((method: string) =>
    method === 'sessions.delete' ? Promise.resolve(reply) : Promise.resolve({}),
  )
}

function deleteCalls() {
  return rpcCall.mock.calls.filter(([method]) => method === 'sessions.delete')
}

beforeEach(() => {
  localStorage.clear()
  rpcCall.mockReset()
  gateway({})
  vi.mocked(toast.success).mockClear()
  vi.mocked(toast.error).mockClear()
  useSessionSelection.setState({
    visible: [],
    order: [],
    keys: new Set(),
    anchor: null,
    pendingDelete: null,
  })
  useSessionMarks.setState({ pinned: new Set(), archived: new Set(), unread: new Set() })
})

describe('SessionRowLink: selecting rows', () => {
  it('Cmd-click toggles a row in the selection without opening it', () => {
    mount()
    // false: the click's default (following the link) was prevented.
    expect(fireEvent.click(link(B), { metaKey: true })).toBe(false)
    fireEvent.click(link(D), { metaKey: true })
    expect(selected()).toEqual([B.key, D.key])
    expect(link(B)).toHaveAttribute('data-selected', 'true')
    expect(link(C)).toHaveAttribute('data-selected', 'false')
    expect(where()).toBe('/sessions')

    fireEvent.click(link(B), { metaKey: true })
    expect(selected()).toEqual([D.key])
  })

  it('Shift-click selects the range from the last plain click, in the order shown', () => {
    mount()
    fireEvent.click(link(B))
    expect(where()).toBe(sessionPath(B.key))
    expect(fireEvent.click(link(D), { shiftKey: true })).toBe(false)
    expect(selected()).toEqual([B.key, C.key, D.key])
    // Still on the chat the plain click opened.
    expect(where()).toBe(sessionPath(B.key))
  })

  it('a plain click clears the selection and opens the chat as before', () => {
    mount()
    fireEvent.click(link(A), { metaKey: true })
    fireEvent.click(link(B), { metaKey: true })
    fireEvent.click(link(E))
    expect(selected()).toEqual([])
    expect(where()).toBe(sessionPath(E.key))
  })

  it('Escape clears the selection and Cmd+A selects every row shown', () => {
    mount()
    // Caps Lock on: the key arrives upper-case.
    fireEvent.keyDown(link(A), { key: 'A', metaKey: true })
    expect(selected()).toEqual(ROWS.map((r) => r.key).sort())
    fireEvent.keyDown(link(A), { key: 'Escape' })
    expect(selected()).toEqual([])
  })
})

describe('SessionRowLink: the menu for a selection', () => {
  it('right-clicking inside a selection of several opens the bulk menu', () => {
    mount()
    fireEvent.click(link(A), { metaKey: true })
    fireEvent.click(link(C), { metaKey: true })
    fireEvent.contextMenu(link(C))
    const menu = screen.getByRole('menu', { name: 'Selected sessions' })
    expect(
      within(menu)
        .getAllByRole('menuitem')
        .map((item) => item.textContent),
    ).toEqual(['Pin', 'Mark as unread', 'Archive', 'Delete 2 sessions…'])
    expect(selected()).toEqual([A.key, C.key])
  })

  it('right-clicking outside the selection makes that row the selection, with its own menu', () => {
    mount()
    fireEvent.click(link(A), { metaKey: true })
    fireEvent.click(link(B), { metaKey: true })
    fireEvent.contextMenu(link(D))
    expect(selected()).toEqual([D.key])
    const menu = screen.getByRole('menu', { name: 'Session' })
    expect(within(menu).getByRole('menuitem', { name: 'Rename…' })).toBeInTheDocument()
  })

  it('without a selection, right-click is the single-row menu as before', () => {
    mount()
    fireEvent.contextMenu(link(B))
    expect(screen.getByRole('menu', { name: 'Session' })).toBeInTheDocument()
    expect(selected()).toEqual([])
  })

  it('follows the selection while it is open: a row the list drops leaves the count', () => {
    mount()
    for (const r of [A, B, C]) fireEvent.click(link(r), { metaKey: true })
    fireEvent.contextMenu(link(A))
    expect(screen.getByRole('menuitem', { name: 'Delete 3 sessions…' })).toBeInTheDocument()
    act(() =>
      useSessionSelection
        .getState()
        .setVisible([A, B, D, E].map((r) => ({ row: r, nested: false }))),
    )
    expect(screen.getByRole('menuitem', { name: 'Delete 2 sessions…' })).toBeInTheDocument()
  })

  const pinCalls = () => rpcCall.mock.calls.filter(([method]) => method === 'sessions.pin')

  it('bulk Pin pins every selected row on the gateway', async () => {
    mount()
    fireEvent.click(link(A), { metaKey: true })
    fireEvent.click(link(B), { metaKey: true })
    fireEvent.contextMenu(link(A))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Pin' }))
    await waitFor(() =>
      expect(pinCalls()).toEqual([
        ['sessions.pin', { key: A.key, pinned: true }],
        ['sessions.pin', { key: B.key, pinned: true }],
      ]),
    )
    // Nothing is kept on this Mac any more.
    expect(useSessionMarks.getState().pinned.size).toBe(0)
  })

  it('bulk offers Unpin only when every selected row is pinned, and pins the rest otherwise', async () => {
    const pinnedA = { ...A, raw: { key: A.key, pinned: true } }
    const pinnedB = { ...B, raw: { key: B.key, pinned: true } }
    mount({ rows: [pinnedA, pinnedB, C] })
    fireEvent.click(link(pinnedA), { metaKey: true })
    fireEvent.click(link(C), { metaKey: true })
    fireEvent.contextMenu(link(C))
    expect(screen.queryByRole('menuitem', { name: 'Unpin' })).toBeNull()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Pin' }))
    await waitFor(() => expect(pinCalls()).toHaveLength(2))
    expect(pinCalls().every(([, p]) => (p as { pinned: boolean }).pinned)).toBe(true)

    rpcCall.mockClear()
    fireEvent.click(link(C), { metaKey: true })
    fireEvent.click(link(pinnedB), { metaKey: true })
    fireEvent.contextMenu(link(pinnedB))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Unpin' }))
    await waitFor(() =>
      expect(pinCalls()).toEqual([
        ['sessions.pin', { key: A.key, pinned: false }],
        ['sessions.pin', { key: B.key, pinned: false }],
      ]),
    )
  })

  it('reports a pin the gateway refused', async () => {
    rpcCall.mockImplementation((method: string) =>
      method === 'sessions.pin' ? Promise.reject(new Error('offline')) : Promise.resolve({}),
    )
    mount()
    fireEvent.contextMenu(link(A))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Pin' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'Could not pin the session: offline',
        expect.anything(),
      ),
    )
  })
})

describe('SessionRowLink: deleting a selection', () => {
  function openBulkDelete(rows: [SessionRow, ...SessionRow[]]) {
    for (const r of rows) fireEvent.click(link(r), { metaKey: true })
    fireEvent.contextMenu(link(rows[0]))
    fireEvent.click(screen.getByRole('menuitem', { name: `Delete ${rows.length} sessions…` }))
    return screen.getByRole('alertdialog', { name: `Delete ${rows.length} sessions?` })
  }

  it('asks once, naming the first few and how many are running; Cancel deletes nothing', () => {
    const rows: [SessionRow, ...SessionRow[]] = [A, { ...B, live: true }, C, D, E]
    mount({ rows })
    const alert = openBulkDelete(rows)
    expect(alert).toHaveTextContent('Chat agent:main:webchat:a')
    expect(alert).toHaveTextContent('Chat agent:main:webchat:c')
    expect(alert).not.toHaveTextContent('Chat agent:main:webchat:d')
    expect(alert).toHaveTextContent('and 2 more')
    expect(alert).toHaveTextContent('One of them is running')
    fireEvent.click(within(alert).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(deleteCalls()).toHaveLength(0)
    expect(selected()).toHaveLength(5)
  })

  it('sends one sessions.delete with every key, forgets their marks and leaves a deleted open chat', async () => {
    gateway({ deleted: [B.key, C.key], errors: [] })
    useSessionMarks.getState().setPinned(B.key, true)
    mount({ path: sessionPath(C.key) })
    const alert = openBulkDelete([B, C])
    fireEvent.click(within(alert).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(deleteCalls()).toEqual([['sessions.delete', { keys: [B.key, C.key] }]])
    expect(useSessionMarks.getState().pinned.has(B.key)).toBe(false)
    await waitFor(() => expect(where()).toBe('/sessions'))
    expect(toast.success).toHaveBeenCalledWith('2 sessions deleted', expect.anything())
    expect(selected()).toEqual([])
  })

  it('reports a partial failure and keeps the rows it could not delete selected', async () => {
    gateway({ deleted: [A.key], errors: [`${B.key}: storage locked`] })
    useSessionMarks.getState().setPinned(B.key, true)
    mount({ path: sessionPath(E.key) })
    const alert = openBulkDelete([A, B])
    fireEvent.click(within(alert).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(toast.error).toHaveBeenCalledWith('Deleted 1 of 2 sessions', expect.anything())
    expect(selected()).toEqual([B.key])
    expect(useSessionMarks.getState().pinned.has(B.key)).toBe(true)
    expect(where()).toBe(sessionPath(E.key))
  })

  it('keeps the whole selection when the call itself fails', async () => {
    rpcCall.mockImplementation((method: string) =>
      method === 'sessions.delete' ? Promise.reject(new Error('offline')) : Promise.resolve({}),
    )
    mount()
    const alert = openBulkDelete([A, B])
    fireEvent.click(within(alert).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(toast.error).toHaveBeenCalledWith(
      'Could not delete the sessions: offline',
      expect.anything(),
    )
    expect(selected()).toEqual([A.key, B.key])
  })
})

describe('SessionRowLink: deleting one row', () => {
  it('reads the errors the gateway reports instead of calling it deleted', async () => {
    gateway({ deleted: [], errors: [`${B.key}: storage locked`] })
    useSessionMarks.getState().setPinned(B.key, true)
    mount({ path: sessionPath(B.key) })
    fireEvent.contextMenu(link(B))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete…' }))
    const alert = screen.getByRole('alertdialog', { name: 'Delete this session?' })
    fireEvent.click(within(alert).getByRole('button', { name: 'Delete' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'Could not delete the session: storage locked',
        expect.anything(),
      ),
    )
    expect(toast.success).not.toHaveBeenCalled()
    expect(useSessionMarks.getState().pinned.has(B.key)).toBe(true)
    expect(where()).toBe(sessionPath(B.key))
  })

  it('says the delete failed without a dangling reason when the gateway gives none', async () => {
    gateway({ deleted: [], errors: [] })
    mount({ path: sessionPath(B.key) })
    fireEvent.contextMenu(link(B))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Could not delete the session', expect.anything()),
    )
  })

  it('still deletes with one key as before', async () => {
    gateway({ deleted: [B.key], errors: [] })
    mount({ path: sessionPath(B.key) })
    fireEvent.contextMenu(link(B))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(where()).toBe('/sessions'))
    expect(deleteCalls()).toEqual([['sessions.delete', { key: B.key }]])
    expect(toast.success).toHaveBeenCalledWith('Session deleted', expect.anything())
  })
})

describe('deleteOutcome', () => {
  const keys = ['agent:main:webchat:a', 'agent:main:webchat:b']

  it('trusts the deleted list and strips the key off each reason', () => {
    expect(
      deleteOutcome(keys, { deleted: [keys[0]], errors: [`${keys[1]}: storage locked`] }),
    ).toEqual({ deleted: [keys[0]], failed: [keys[1]], reasons: ['storage locked'] })
  })

  it('without a deleted list, counts every key the errors do not name', () => {
    expect(deleteOutcome(keys, { errors: [`${keys[0]}: nope`] })).toEqual({
      deleted: [keys[1]],
      failed: [keys[0]],
      reasons: ['nope'],
    })
    expect(deleteOutcome(keys, undefined).deleted).toEqual(keys)
  })

  it('does not take a key for one that merely starts the same', () => {
    const [short, long] = ['agent:main:webchat:s1', 'agent:main:webchat:s10']
    expect(deleteOutcome([short, long], { errors: [`${long}: gone`] }).failed).toEqual([long])
  })
})
