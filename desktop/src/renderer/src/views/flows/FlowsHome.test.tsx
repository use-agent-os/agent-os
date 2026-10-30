import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { useGateway } from '~/stores/gateway'
import { FlowsHome } from './FlowsHome'
import { DOC, IMG } from './test-fixtures'
import type { FlowSummary } from './types'

const rpcCall = vi.fn()
const rpc = { call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))

const FLOWS: FlowSummary[] = [
  {
    id: 'flw_old',
    name: 'Product shot cleanup',
    rev: 3,
    updated: '2026-10-20T10:00:00Z',
    node_count: 4,
    preview: null,
  },
  {
    id: DOC.id,
    name: DOC.name,
    rev: 17,
    updated: '2026-10-29T10:00:00Z',
    node_count: 5,
    preview: IMG,
  },
]

function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  })
  render(
    <MemoryRouter initialEntries={['/flows']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/flows" element={<FlowsHome />} />
          <Route path="*" element={null} />
        </Routes>
        <Where />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  let flows = [...FLOWS]
  rpcCall.mockReset()
  rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
    switch (method) {
      case 'flows.list':
        return { flows }
      case 'flows.create':
        return { flow: { ...DOC, id: 'flw_new', name: params.name } }
      case 'flows.delete':
        flows = flows.filter((f) => f.id !== params.id)
        return { ok: true }
      case 'flows.media':
        return { mime: 'image/png', data_b64: 'AAAA', width: 1, height: 1 }
      default:
        return {}
    }
  })
  useConnection.getState().setState('connected')
  useGateway.setState({ status: { state: 'running', pid: 1, url: 'http://x', error: null } })
})

describe('FlowsHome', () => {
  it('lists flows newest first with a preview thumbnail from flows.media', async () => {
    mount()
    const links = await screen.findAllByRole('link', { name: /^Open / })
    expect(links.map((l) => l.getAttribute('aria-label'))).toEqual([
      'Open Lighthouse study',
      'Open Product shot cleanup',
    ])
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('flows.media', { sha256: IMG.sha256, max_side: 256 }),
    )
  })

  it('creates from a template and opens it', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /Text to image/ }))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/flows/flw_new'))
    expect(rpcCall).toHaveBeenCalledWith('flows.create', {
      name: 'Text to image',
      template: 'text_to_image',
    })
  })

  it('deletes behind an in-app confirmation', async () => {
    mount()
    await screen.findAllByRole('link', { name: /^Open / })
    fireEvent.click(screen.getAllByRole('button', { name: 'More' })[1]!)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete…' }))
    const alert = screen.getByRole('alertdialog', { name: 'Delete this flow?' })
    expect(rpcCall).not.toHaveBeenCalledWith('flows.delete', expect.anything())
    fireEvent.click(within(alert).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('flows.delete', { id: 'flw_old' }))
    await waitFor(() => expect(screen.queryByText('Product shot cleanup')).toBeNull())
  })

  it('a gateway that refuses this window says so up front and offers no writes', async () => {
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.list') {
        return { flows: FLOWS, writable: false, read_only_reason: 'agent connection' }
      }
      return base(method, params)
    })
    mount()
    const title = await screen.findByText("Flows can't be edited or run from this window")
    const banner = title.closest('.flw-banner')!
    expect(banner).toHaveTextContent('agent connection')
    expect(screen.getByRole('button', { name: /New flow/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Text to image/ })).toBeDisabled()
    fireEvent.click(screen.getAllByRole('button', { name: 'More' })[0]!)
    expect(screen.getByRole('menuitem', { name: /Delete…/ })).toBeDisabled()
  })
})
