import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { RpcError } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'
import { useGateway } from '~/stores/gateway'
import { FlowEditorRoute } from './FlowEditor'
import { DOC, MODELS, NODES } from './test-fixtures'
import type { FlowDoc, FlowRevision } from './types'

// Flow authoring by the agent: version history, restore, and what the editor
// says when the agent changes the flow on screen.

const toast = vi.hoisted(() =>
  Object.assign(vi.fn(), {
    message: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
    success: vi.fn(),
  }),
)
vi.mock('sonner', () => ({ toast }))

type Handler = (...args: unknown[]) => void
const listeners = new Map<string, Set<Handler>>()
const rpcCall = vi.fn()
const rpc = {
  call: rpcCall,
  waitForConnection: async () => {},
  on: (event: string, handler: Handler) => {
    if (!listeners.has(event)) listeners.set(event, new Set())
    listeners.get(event)!.add(handler)
    return () => listeners.get(event)?.delete(handler)
  },
}
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))

function emit(event: string, payload: unknown) {
  act(() => {
    for (const h of listeners.get(event) ?? []) h(payload)
  })
}

function conflict(): RpcError {
  const err = new RpcError('flows.conflict')
  err.code = 'flows.conflict'
  return err
}

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
  ;(globalThis as { DOMMatrixReadOnly?: unknown }).DOMMatrixReadOnly ??= class {
    m22 = 1
  }
})

const MIN = 60_000
const REVISIONS: FlowRevision[] = [
  {
    rev: 15,
    author: 'user',
    created: new Date(Date.now() - 3 * 60 * MIN).toISOString(),
    summary: 'Changed the prompt',
  },
  {
    rev: 17,
    author: 'agent',
    session_key: 'agent:main:webchat:abc',
    created: new Date(Date.now() - 5 * MIN).toISOString(),
    summary: 'Added a Save node; count 4',
  },
  { rev: 16, author: 'user', created: new Date(Date.now() - 60 * MIN).toISOString() },
]

let stored: FlowDoc

beforeEach(() => {
  listeners.clear()
  toast.mockClear()
  toast.message.mockClear()
  toast.warning.mockClear()
  toast.error.mockClear()
  toast.success.mockClear()
  stored = structuredClone(DOC)
  rpcCall.mockReset()
  rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
    switch (method) {
      case 'flows.get':
        return { flow: stored }
      case 'flows.nodes':
        return NODES
      case 'flows.models':
        return MODELS
      case 'flows.runs':
        return { runs: [] }
      case 'flows.revisions':
        return { revisions: REVISIONS }
      case 'flows.restore':
        stored = {
          ...structuredClone(DOC),
          name: 'As it was',
          rev: (params.base_rev as number) + 1,
        }
        return { flow: stored }
      case 'flows.save': {
        const flow = params.flow as FlowDoc
        stored = { ...flow, rev: (params.base_rev as number) + 1 }
        return { flow: stored }
      }
      case 'flows.estimate':
        return { nodes: {}, total: { kind: 'free', approximate: false }, will_run: [], cached: [] }
      default:
        return {}
    }
  })
  useConnection.getState().setState('connected')
  useGateway.setState({ status: { state: 'running', pid: 1, url: 'http://x', error: null } })
})

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  })
  return render(
    <MemoryRouter initialEntries={[`/flows/${DOC.id}`]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/flows/:id" element={<FlowEditorRoute />} />
          <Route path="*" element={null} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

async function openHistory() {
  await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
  fireEvent.click(screen.getByRole('button', { name: 'Version history' }))
  return screen.findByRole('dialog', { name: 'Version history' })
}

describe('version history', () => {
  it('lists versions newest first: who, when, what changed; the current one is marked', async () => {
    mount()
    const panel = await openHistory()
    const rows = await within(panel).findAllByRole('listitem')
    expect(rows.map((r) => within(r).getByText(/^Version \d+$/).textContent)).toEqual([
      'Version 17',
      'Version 16',
      'Version 15',
    ])
    expect(within(rows[0]!).getByText('Agent')).toBeInTheDocument()
    expect(within(rows[0]!).getByText('5m ago')).toBeInTheDocument()
    expect(within(rows[0]!).getByText('Added a Save node; count 4')).toBeInTheDocument()
    expect(within(rows[0]!).getByText('Current')).toBeInTheDocument()
    expect(within(rows[0]!).queryByRole('button', { name: /Restore/ })).toBeNull()
    expect(within(rows[1]!).getByText('You')).toBeInTheDocument()
    expect(within(rows[2]!).getByText('3h ago')).toBeInTheDocument()
    expect(rpcCall).toHaveBeenCalledWith('flows.revisions', { id: DOC.id })
  })

  it('restores behind an in-app confirmation, against the current revision, then reloads', async () => {
    mount()
    const panel = await openHistory()
    fireEvent.click(await within(panel).findByRole('button', { name: 'Restore Version 15' }))
    const confirm = screen.getByRole('alertdialog', { name: 'Restore version 15?' })
    // The popover closes when the confirmation opens; nothing sits above the dialog.
    expect(screen.queryByRole('dialog', { name: 'Version history' })).toBeNull()
    expect(rpcCall).not.toHaveBeenCalledWith('flows.restore', expect.anything())
    fireEvent.click(within(confirm).getByRole('button', { name: 'Restore' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('flows.restore', { id: DOC.id, rev: 15, base_rev: 17 }),
    )
    await waitFor(() => expect(screen.getByLabelText('Flow name')).toHaveValue('As it was'))
    expect(toast.success).toHaveBeenCalledWith('Restored version 15')
    expect(screen.queryByRole('alertdialog')).toBeNull()
    // Its own change event is not news: no "updated elsewhere" reload.
    emit('flows.changed', { flow_id: DOC.id, rev: 18, author: 'user' })
    expect(toast.message).not.toHaveBeenCalled()
  })

  it('saves pending edits as a version before restoring over them', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    fireEvent.change(screen.getByLabelText('text'), { target: { value: 'unsaved' } })
    const panel = await openHistory()
    fireEvent.click(await within(panel).findByRole('button', { name: 'Restore Version 15' }))
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Restore' }),
    )
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('flows.restore', { id: DOC.id, rev: 15, base_rev: 18 }),
    )
    const order = rpcCall.mock.calls.map(([m]) => m)
    expect(order.indexOf('flows.save')).toBeLessThan(order.indexOf('flows.restore'))
  })

  it('a read-only window can read the history but not restore', async () => {
    stored = { ...structuredClone(DOC), writable: false, read_only_reason: 'agent connection' }
    mount()
    const panel = await openHistory()
    const restore = await within(panel).findByRole('button', { name: 'Restore Version 15' })
    expect(restore).toBeDisabled()
  })
})

describe('changes made by the agent', () => {
  it('a clean editor reloads and says the agent did it, with a way to the history', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    stored = { ...structuredClone(DOC), rev: 18, name: 'Agent edit', updated_by: 'agent' }
    emit('flows.changed', { flow_id: DOC.id, rev: 18, author: 'agent' })
    await waitFor(() => expect(screen.getByLabelText('Flow name')).toHaveValue('Agent edit'))
    expect(toast.message).toHaveBeenCalledWith(
      'Updated by the agent',
      expect.objectContaining({ action: expect.objectContaining({ label: 'Version history' }) }),
    )
    const action = toast.message.mock.calls[0]![1].action as { onClick: () => void }
    act(() => action.onClick())
    expect(await screen.findByRole('dialog', { name: 'Version history' })).toBeInTheDocument()
  })

  it('a change by the person elsewhere keeps the old wording', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    stored = { ...structuredClone(DOC), rev: 18 }
    emit('flows.changed', { flow_id: DOC.id, rev: 18, author: 'user' })
    await waitFor(() => expect(toast.message).toHaveBeenCalled())
    expect(toast.message.mock.calls[0]![0]).toBe('Updated from another window')
  })

  it('a dirty editor keeps the conflict path, and the message names the agent', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.save') throw conflict()
      return base(method, params)
    })
    fireEvent.change(screen.getByLabelText('text'), { target: { value: 'mine' } })
    stored = { ...structuredClone(DOC), rev: 18, name: 'Agent edit', updated_by: 'agent' }
    emit('flows.changed', { flow_id: DOC.id, rev: 18, author: 'agent' })
    await waitFor(() => expect(toast.warning).toHaveBeenCalled(), { timeout: 3000 })
    expect(toast.warning.mock.calls[0]![0]).toBe(
      'The agent changed this flow while you were editing. Its version is loaded; your last edits were not saved.',
    )
    expect(screen.getByLabelText('Flow name')).toHaveValue('Agent edit')
  })

  it('a flow the agent made says so beside its name', async () => {
    stored = { ...structuredClone(DOC), created_by: 'agent' }
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    expect(screen.getByText('by agent')).toBeInTheDocument()
  })
})
