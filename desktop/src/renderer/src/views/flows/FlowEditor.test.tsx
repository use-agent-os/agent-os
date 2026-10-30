import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { RpcError } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'
import { useGateway } from '~/stores/gateway'
import { FlowEditorRoute } from './FlowEditor'
import { DOC, IMG, MODELS, NODES } from './test-fixtures'
import type { FlowDoc } from './types'

// A gateway that speaks the flows contract from the fixtures, and can push events.
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

function rpcError(code: string, details?: unknown): RpcError {
  const err = new RpcError(code)
  err.code = code
  err.details = details
  return err
}

beforeAll(() => {
  // jsdom has neither; xyflow measures nodes with them.
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
  ;(globalThis as { DOMMatrixReadOnly?: unknown }).DOMMatrixReadOnly ??= class {
    m22 = 1
    constructor(transform?: string) {
      const scale = transform?.match(/scale\(([1-9.])\)/)?.[1]
      this.m22 = scale !== undefined ? +scale : 1
    }
  }
})

let stored: FlowDoc

beforeEach(() => {
  listeners.clear()
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
      case 'flows.estimate':
        return {
          nodes: { n5: { kind: 'usd', usd: 0.16, approximate: true } },
          total: { kind: 'usd', usd: 0.29, approximate: true },
          will_run: ['n4', 'n5', 'n7'],
          cached: ['n3', 'n6'],
        }
      case 'flows.save': {
        const flow = params.flow as FlowDoc
        stored = { ...flow, rev: (params.base_rev as number) + 1 }
        return { flow: stored }
      }
      case 'flows.run':
        return { run_id: 'run_1', status: 'queued' }
      case 'flows.validate':
        return { ok: true, node_errors: {} }
      case 'flows.media':
        return { mime: 'image/png', data_b64: 'AAAA', width: 1, height: 1 }
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

function nodeEl(title: string): HTMLElement {
  const el = screen.getByText(title, { selector: '.flw-node__title' }).closest('.flw-node')
  if (!el) throw new Error(`no node ${title}`)
  return el as HTMLElement
}

describe('FlowEditor', () => {
  it('draws every node from its schema, with the selected model and linked widgets', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    // A custom title wins over the schema's display name.
    expect(screen.getByText('Reference', { selector: '.flw-node__title' })).toBeInTheDocument()
    const image = nodeEl('Image')
    // auto resolves to the registry default; Nano Banana shows ratios + tiers, not sizes.
    expect(within(image).getByText('Auto · Nano Banana 2')).toBeInTheDocument()
    expect(within(image).getByLabelText('aspect')).toBeInTheDocument()
    expect(within(image).queryByLabelText('size')).toBeNull()
    // prompt is linked from Prompt Writer: shown disabled, naming its source.
    expect(within(image).getByText('prompt ← Prompt Writer')).toBeInTheDocument()
    // One image connected: edit mode.
    expect(within(image).getByText('edit · 1 ref')).toBeInTheDocument()
  })

  it('shows the estimate chip and per-node price badges', async () => {
    mount()
    expect(await screen.findByText('~$0.29')).toBeInTheDocument()
    expect(screen.getByText('· 2 cached')).toBeInTheDocument()
    expect(within(nodeEl('Image')).getByText('~$0.16')).toBeInTheDocument()
  })

  it('autosaves an edit with the revision it loaded, values by name', async () => {
    mount()
    const text = await screen.findByLabelText('text')
    fireEvent.change(text, { target: { value: 'a lighthouse at dusk' } })
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('flows.save', expect.anything()), {
      timeout: 3000,
    })
    const [, params] = rpcCall.mock.calls.find(([m]) => m === 'flows.save')!
    expect(params.base_rev).toBe(17)
    const sent = params.flow as FlowDoc
    expect(sent.nodes.find((n) => n.id === 'n3')?.values).toEqual({
      text: 'a lighthouse at dusk',
    })
    expect(sent.links).toEqual(DOC.links)
    expect(sent.inputs).toEqual({ idea: 'n3.text' })
    await screen.findByText('· Saved')
  })

  it('on a save conflict reloads the gateway copy', async () => {
    mount()
    const text = await screen.findByLabelText('text')
    stored = { ...structuredClone(DOC), rev: 30, name: 'Changed by the agent' }
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.save') throw rpcError('flows.conflict')
      return base(method, params)
    })
    fireEvent.change(text, { target: { value: 'mine' } })
    await waitFor(
      () => expect(screen.getByLabelText('Flow name')).toHaveValue('Changed by the agent'),
      { timeout: 3000 },
    )
    expect(screen.getByLabelText('text')).toHaveValue('a lighthouse')
  })

  it('renders validation errors from flows.run on the node and its widget', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.run') {
        throw rpcError('flows.invalid', {
          node_errors: {
            n4: [{ input: 'style', code: 'not_in_options', message: 'Pick a listed style' }],
            n5: [{ code: 'model_unavailable', message: 'No image model has a key' }],
          },
        })
      }
      return base(method, params)
    })
    fireEvent.click(screen.getByRole('button', { name: /^Run/ }))
    expect(await screen.findByText('Pick a listed style')).toBeInTheDocument()
    expect(screen.getByText('No image model has a key')).toBeInTheDocument()
    expect(nodeEl('Image')).toHaveAttribute('data-state', 'invalid')
    expect(within(nodeEl('Prompt Writer')).getByText('invalid')).toBeInTheDocument()
  })

  it('paints run events on the nodes in seq order, outputs inline', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    fireEvent.click(screen.getByRole('button', { name: /^Run/ }))
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('flows.run', { flow_id: DOC.id }))
    const base = { run_id: 'run_1', flow_id: DOC.id }
    emit('flows.run', { ...base, seq: 1, status: 'running' })
    emit('flows.node', { ...base, seq: 2, node_id: 'n3', state: 'cached' })
    // seq 4 before 3: held until 3 arrives.
    emit('flows.node', {
      ...base,
      seq: 4,
      node_id: 'n4',
      state: 'done',
      ms: 2100,
      outputs: { text: 'A lone white lighthouse' },
    })
    expect(within(nodeEl('Prompt Writer')).queryByText('A lone white lighthouse')).toBeNull()
    emit('flows.node', { ...base, seq: 3, node_id: 'n4', state: 'running' })
    expect(within(nodeEl('Prompt Writer')).getByText('A lone white lighthouse')).toBeInTheDocument()
    expect(within(nodeEl('Prompt Writer')).getByText('2.1s')).toBeInTheDocument()
    expect(within(nodeEl('Text')).getByText('cached')).toBeInTheDocument()
    emit('flows.node', {
      ...base,
      seq: 5,
      node_id: 'n5',
      state: 'running',
      progress: { value: 2, max: 4 },
    })
    expect(nodeEl('Image')).toHaveAttribute('data-state', 'running')
    // Progress and the elapsed clock.
    expect(within(nodeEl('Image')).getByText(/^2\/4 · 0:0\d$/)).toBeInTheDocument()
    // Another flow's events are not ours.
    emit('flows.node', { run_id: 'x', flow_id: 'other', seq: 1, node_id: 'n5', state: 'error' })
    expect(nodeEl('Image')).toHaveAttribute('data-state', 'running')
  })

  it('opens the add-node menu on the + button and adds a node', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    fireEvent.click(screen.getByRole('button', { name: 'Add node' }))
    const menu = await screen.findByRole('dialog', { name: 'Add node' })
    fireEvent.change(within(menu).getByLabelText('Search nodes'), { target: { value: 'join' } })
    fireEvent.click(within(menu).getByRole('option', { name: /Join Text/ }))
    expect(
      await screen.findByText('Join Text', { selector: '.flw-node__title' }),
    ).toBeInTheDocument()
  })

  it('greys a model without a key in the picker, with the reason', async () => {
    mount()
    const image = await waitFor(() => nodeEl('Image'))
    // xyflow keeps nodes it could not measure (jsdom) visibility: hidden, so by label.
    fireEvent.click(within(image).getByLabelText(/^model:/))
    const picker = await screen.findByRole('dialog', { name: 'Model' })
    const gpt = within(picker).getByRole('option', { name: /GPT Image/ })
    expect(gpt).toHaveAttribute('aria-disabled', 'true')
    expect(within(gpt).getByText('OPENAI_API_KEY is not set')).toBeInTheDocument()
    expect(within(picker).getByText('1 image connected')).toBeInTheDocument()
  })

  it('a refused write shows a banner and a disabled Run, never a silent no-op', async () => {
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.save' || method === 'flows.run') {
        throw rpcError('flows.operator_required', undefined)
      }
      return base(method, params)
    })
    mount()
    const text = await screen.findByLabelText('text')
    fireEvent.change(text, { target: { value: 'mine' } })
    const banner = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(banner).toHaveTextContent("Flows can't be edited or run from this window")
    expect(banner).toHaveTextContent('flows.operator_required')
    expect(screen.getByText('· Read-only')).toBeInTheDocument()
    const runButton = screen.getByRole('button', { name: /^Run/ })
    expect(runButton).toBeDisabled()
    expect(runButton).toHaveAttribute('title', "Flows can't be edited or run from this window")
    // Read-only stops the autosave from retrying.
    const saves = rpcCall.mock.calls.filter(([m]) => m === 'flows.save').length
    fireEvent.change(screen.getByLabelText('text'), { target: { value: 'mine again' } })
    await new Promise((r) => setTimeout(r, 1000))
    expect(rpcCall.mock.calls.filter(([m]) => m === 'flows.save')).toHaveLength(saves)
  })

  it('a refused run (no edits pending) also turns the editor read-only', async () => {
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.run') throw rpcError('flows.operator_required', undefined)
      return base(method, params)
    })
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    fireEvent.click(screen.getByRole('button', { name: /^Run/ }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Run/ })).toBeDisabled()
  })

  it('after Stop nothing looks like it runs; a late result shows as stopped, with its image', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    const base = { run_id: 'run_9', flow_id: DOC.id }
    emit('flows.run', { ...base, seq: 1, status: 'running' })
    emit('flows.node', { ...base, seq: 2, node_id: 'n5', state: 'running' })
    emit('flows.node', { ...base, seq: 3, node_id: 'n7', state: 'queued' })
    expect(nodeEl('Image')).toHaveAttribute('data-state', 'running')
    expect(within(nodeEl('Image')).getAllByText('waiting').length).toBeGreaterThan(0)
    // (Edges are not drawn in jsdom; their dash follows this same node state.)
    // Stop: the engine cancels the run at once; n5's provider call keeps going.
    emit('flows.run', { ...base, seq: 4, status: 'cancelled' })
    const image = nodeEl('Image')
    expect(image).toHaveAttribute('data-state', 'stopping')
    expect(within(image).getByText('stopped · finishing…')).toBeInTheDocument()
    expect(within(image).queryByText('waiting')).toBeNull()
    expect(nodeEl('Save')).toHaveAttribute('data-state', 'skipped')
    expect(screen.getByRole('button', { name: /^Stop/ })).toBeDisabled()
    // The call lands: real cost and the image, then the corrected run cost.
    emit('flows.node', {
      ...base,
      seq: 5,
      node_id: 'n5',
      state: 'done',
      cost_usd: 0.04,
      outputs: { images: [IMG] },
    })
    emit('flows.run', { ...base, seq: 6, status: 'cancelled', cost_usd: 0.04 })
    expect(nodeEl('Image')).toHaveAttribute('data-state', 'done')
    expect(within(nodeEl('Image')).getByText('stopped · $0.04')).toBeInTheDocument()
    expect(nodeEl('Image').querySelectorAll('.flw-grid__cell')).toHaveLength(1)
    const tray = screen.getByRole('region', { name: 'Results' })
    expect(within(tray).getByText(/stopped · \$0\.04/)).toBeInTheDocument()
    expect(within(tray).getAllByRole('button', { name: 'Open image' })).toHaveLength(1)
    // A stray running event for the finished run changes nothing.
    emit('flows.node', { ...base, seq: 7, node_id: 'n5', state: 'running' })
    expect(nodeEl('Image')).toHaveAttribute('data-state', 'done')
  })

  it('the tray counts an image once when Image and Save both report it', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    const base = { run_id: 'run_7', flow_id: DOC.id }
    emit('flows.run', { ...base, seq: 1, status: 'running' })
    emit('flows.node', {
      ...base,
      seq: 2,
      node_id: 'n5',
      state: 'done',
      outputs: { images: [IMG] },
    })
    emit('flows.node', {
      ...base,
      seq: 3,
      node_id: 'n7',
      state: 'done',
      outputs: { images: [IMG] },
    })
    emit('flows.run', { ...base, seq: 4, status: 'done' })
    const tray = screen.getByRole('region', { name: 'Results' })
    expect(within(tray).getByText(/1 image/)).toBeInTheDocument()
    expect(within(tray).getAllByRole('button', { name: 'Open image' })).toHaveLength(1)
  })

  it('a running node without progress shows how long it has been running', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    const base = { run_id: 'run_8', flow_id: DOC.id }
    emit('flows.run', { ...base, seq: 1, status: 'running' })
    emit('flows.node', { ...base, seq: 2, node_id: 'n4', state: 'running' })
    expect(within(nodeEl('Prompt Writer')).getByText(/^running · 0:0\d$/)).toBeInTheDocument()
  })

  it('the add-node search and the model picker take focus once shown', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    fireEvent.click(screen.getByRole('button', { name: 'Add node' }))
    const menu = await screen.findByRole('dialog', { name: 'Add node' })
    await waitFor(() =>
      expect(document.activeElement).toBe(within(menu).getByLabelText('Search nodes')),
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add node' })).toBeNull())
    fireEvent.click(within(nodeEl('Image')).getByLabelText(/^model:/))
    const picker = await screen.findByRole('dialog', { name: 'Model' })
    await waitFor(() => expect(document.activeElement).toBe(within(picker).getByRole('textbox')))
  })

  it('a text-to-image-only model shows no image count, and its socket takes no link', async () => {
    const t2i = {
      ...MODELS.models[0]!,
      id: 'surplus/flux-2-pro',
      display: 'FLUX.2 Pro',
      max_images: 0,
      ops: { t2i: true, edit: false, mask: false },
    }
    stored = {
      ...structuredClone(DOC),
      links: DOC.links.filter((l) => l.to[1] !== 'images'),
    }
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.models') return { default: t2i.id, models: [t2i] }
      return base(method, params)
    })
    mount()
    const image = await waitFor(() => nodeEl('Image'))
    await waitFor(() => expect(within(image).getByText('text-to-image only')).toBeInTheDocument())
    expect(within(image).queryByText('0 / 0')).toBeNull()
  })

  it('switching the model swaps its widgets and drops choices the new model lacks', async () => {
    const gpt = { ...MODELS.models[1]!, available: true, unavailable_reason: null }
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.models') return { ...MODELS, models: [MODELS.models[0]!, gpt] }
      return base(method, params)
    })
    mount()
    const image = await waitFor(() => nodeEl('Image'))
    expect(within(image).getByLabelText('aspect')).toBeInTheDocument()
    fireEvent.click(within(image).getByLabelText(/^model:/))
    const picker = await screen.findByRole('dialog', { name: 'Model' })
    fireEvent.click(within(picker).getByRole('option', { name: /GPT Image/ }))
    await waitFor(() => expect(within(image).queryByLabelText('aspect')).toBeNull())
    expect(within(image).queryByLabelText('resolution')).toBeNull()
    expect(within(image).getByLabelText('size')).toBeInTheDocument()
    expect(within(image).getByLabelText('quality')).toBeInTheDocument()
    expect(within(image).getByLabelText('background')).toBeInTheDocument()
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('flows.save', expect.anything()), {
      timeout: 3000,
    })
    const [, params] = rpcCall.mock.calls.filter(([m]) => m === 'flows.save').at(-1)!
    const values = (params.flow as FlowDoc).nodes.find((n) => n.id === 'n5')!.values
    expect(values.model).toBe('openai/gpt-image-1')
    expect('aspect' in values).toBe(false)
  })

  it('a combo whose value the model lacks shows the first option and saves it on the next edit', async () => {
    // FLUX.2 Pro: sizes only, and the size default "auto" is not one of them.
    const flux = {
      ...MODELS.models[0]!,
      id: 'surplus/flux-2-pro',
      display: 'FLUX.2 Pro',
      max_images: 0,
      ops: { t2i: true, edit: false, mask: false },
      output: { ratios: [], tiers: [], sizes: ['1024x1024', '1024x768'], qualities: [] },
    }
    const nodes = structuredClone(NODES)
    nodes.nodes.find((n) => n.id === 'image')!.inputs.find((i) => i.name === 'size')!.default =
      'auto'
    stored = {
      ...structuredClone(DOC),
      links: DOC.links.filter((l) => l.to[1] !== 'images'),
    }
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.models') return { default: flux.id, models: [flux] }
      if (method === 'flows.nodes') return nodes
      return base(method, params)
    })
    mount()
    const image = await waitFor(() => nodeEl('Image'))
    await waitFor(() => expect(within(image).getByLabelText('size')).toHaveValue('1024x1024'))
    expect(within(image).queryByLabelText('aspect')).toBeNull()
    fireEvent.click(within(image).getByLabelText('Decrease count'))
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('flows.save', expect.anything()), {
      timeout: 3000,
    })
    const [, params] = rpcCall.mock.calls.filter(([m]) => m === 'flows.save').at(-1)!
    const values = (params.flow as FlowDoc).nodes.find((n) => n.id === 'n5')!.values
    expect(values.size).toBe('1024x1024')
  })

  it('a flow the gateway marks read-only shows so on load: banner, Run off, no editing', async () => {
    stored = {
      ...structuredClone(DOC),
      writable: false,
      read_only_reason: 'agent connection',
    } as FlowDoc
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.get') {
        const { writable, read_only_reason, ...flow } = stored
        return { flow, writable, read_only_reason }
      }
      return base(method, params)
    })
    mount()
    const banner = await screen.findByRole('alert')
    expect(banner).toHaveTextContent("Flows can't be edited or run from this window")
    expect(banner).toHaveTextContent('agent connection')
    // One message: the gateway's reason replaces the fallback copy.
    expect(banner).not.toHaveTextContent('treats this window as an agent connection')
    expect(screen.getByRole('button', { name: /^Run/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Add node' })).toBeDisabled()
    expect(screen.getByLabelText('Flow name')).toHaveAttribute('readonly')
    expect(nodeEl('Text').querySelector('.flw-node__widgets')).toHaveAttribute('inert')
    expect(rpcCall).not.toHaveBeenCalledWith('flows.run', expect.anything())
    expect(rpcCall).not.toHaveBeenCalledWith('flows.save', expect.anything())
  })

  it("a settled run lists every new image from the gateway's record, not the cached one", async () => {
    const edit = { ...IMG, sha256: 'c'.repeat(64) }
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.run.get') {
        return {
          run: {
            run_id: 'run_b',
            flow_id: DOC.id,
            rev: 17,
            status: 'done',
            created: '2026-10-29T10:00:00Z',
            preview: IMG,
            origin: 'canvas',
            error: null,
            image_count: 1,
            images: [edit],
            nodes: { n5: { state: 'cached' } },
            outputs: { n5: { images: [IMG] } },
          },
        }
      }
      return base(method, params)
    })
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    const ev = { run_id: 'run_b', flow_id: DOC.id }
    emit('flows.run', { ...ev, seq: 1, status: 'running' })
    emit('flows.node', {
      ...ev,
      seq: 2,
      node_id: 'n5',
      state: 'cached',
      outputs: { images: [IMG] },
    })
    emit('flows.run', { ...ev, seq: 3, status: 'done' })
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('flows.run.get', { run_id: 'run_b' }))
    const tray = screen.getByRole('region', { name: 'Results' })
    await waitFor(() => expect(within(tray).getByText(/1 image/)).toBeInTheDocument())
    const thumbs = within(tray).getAllByRole('button', { name: 'Open image' })
    expect(thumbs).toHaveLength(1)
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('flows.media', { sha256: edit.sha256, max_side: 256 }),
    )
  })

  it('a run that made no images shows a compact "no new images" entry', async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    const ev = { run_id: 'run_t', flow_id: DOC.id }
    emit('flows.run', { ...ev, seq: 1, status: 'running' })
    emit('flows.node', { ...ev, seq: 2, node_id: 'n4', state: 'done', outputs: { text: 'x' } })
    emit('flows.run', { ...ev, seq: 3, status: 'done' })
    const tray = screen.getByRole('region', { name: 'Results' })
    expect(within(tray).getByText(/done · no new images/)).toBeInTheDocument()
    expect(within(tray).queryAllByRole('button', { name: 'Open image' })).toHaveLength(0)
  })

  it("shows a node's provider note under its outputs", async () => {
    mount()
    await screen.findByText('Prompt Writer', { selector: '.flw-node__title' })
    const ev = { run_id: 'run_n', flow_id: DOC.id }
    emit('flows.run', { ...ev, seq: 1, status: 'running' })
    emit('flows.node', {
      ...ev,
      seq: 2,
      node_id: 'n5',
      state: 'done',
      outputs: { images: [IMG], note: 'Provider returned 1024×768 instead of 1536×1024' },
    })
    expect(
      within(nodeEl('Image')).getByText('Provider returned 1024×768 instead of 1536×1024'),
    ).toBeInTheDocument()
  })

  it('a model with neither sizes nor ratios draws no Size or Aspect combo', async () => {
    const flux = {
      ...MODELS.models[0]!,
      id: 'surplus/flux-2-pro',
      display: 'FLUX.2 Pro',
      max_images: 0,
      ops: { t2i: true, edit: false, mask: false },
      output: { ratios: [], tiers: [], sizes: [], qualities: [] },
    }
    const nodes = structuredClone(NODES)
    // Even a combo fed by the model without a show_when rule is left out.
    delete nodes.nodes.find((n) => n.id === 'image')!.inputs.find((i) => i.name === 'size')!
      .show_when
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.models') return { default: flux.id, models: [flux] }
      if (method === 'flows.nodes') return nodes
      return base(method, params)
    })
    mount()
    const image = await waitFor(() => nodeEl('Image'))
    await waitFor(() => expect(within(image).getByText('Auto · FLUX.2 Pro')).toBeInTheDocument())
    for (const name of ['aspect', 'size', 'resolution', 'quality']) {
      expect(within(image).queryByLabelText(name)).toBeNull()
    }
    expect(image.querySelector('.flw-w__value')?.textContent).not.toBe('')
  })
})
