import '@/test/web'
import { act, screen, waitFor, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { stubMatchMedia } from '@/test/web'
import { KEY, assistantRow, chatRpc, openChat, resetChatState, userRow } from '@/test/web-chat'

vi.mock('lightweight-charts', () => {
  const series = { setData: vi.fn(), priceScale: () => ({ applyOptions: vi.fn() }) }
  return {
    CandlestickSeries: 'candles',
    HistogramSeries: 'histogram',
    createChart: vi.fn(() => ({
      addSeries: vi.fn(() => series),
      timeScale: () => ({ fitContent: vi.fn() }),
      remove: vi.fn(),
    })),
  }
})

const FIXTURES = 'src/views/chat/transcript/__fixtures__'
const fixture = (path: string) => JSON.parse(readFileSync(`${FIXTURES}/${path}`, 'utf8')) as unknown

const originalMatchMedia = window.matchMedia
beforeEach(resetChatState)
afterEach(() => {
  window.matchMedia = originalMatchMedia
  vi.unstubAllGlobals()
})

function withArtifact(mime: string, body: unknown, name = 'card.json') {
  const artifact = {
    id: `art-${name}`,
    name,
    mime,
    size: 10,
    download_url: `/api/v1/artifacts/art-${name}`,
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
    ),
  )
  return chatRpc([userRow('u1', 'show'), assistantRow('a1', 'Here.', { artifacts: [artifact] })])
}

describe('data cards', () => {
  it('renders a chart artifact', async () => {
    const candles = Array.from({ length: 5 }, (_, i) => ({
      time: 1_700_000_000 + i * 60,
      open: 1,
      high: 2,
      low: 0.5,
      close: 1.5,
    }))
    await openChat(
      withArtifact('application/vnd.agentos.chart+json', {
        type: 'candlestick',
        title: 'BTC 1m',
        candles,
      }),
    )
    expect(await screen.findByRole('heading', { name: 'BTC 1m' })).toBeInTheDocument()
    expect(screen.getByTestId('chart-canvas')).toBeInTheDocument()
  })

  it('renders a cards artifact', async () => {
    await openChat(
      withArtifact('application/vnd.agentos.cards+json', {
        type: 'cards',
        title: 'Tokens',
        cards: [{ title: 'ETH', fields: [{ label: 'Price', value: '$3,000' }] }],
      }),
    )
    expect(await screen.findByText('ETH')).toBeInTheDocument()
    expect(screen.getByText('$3,000')).toBeInTheDocument()
  })

  it('renders a flow-change artifact', async () => {
    await openChat(
      withArtifact('application/vnd.agentos.flow-change+json', {
        kind: 'flow_change',
        flow_id: 'f1',
        name: 'Thumbnails',
        rev: 2,
        change: 'updated',
        summary: 'Added a resize node',
      }),
    )
    expect(await screen.findByText('Added a resize node')).toBeInTheDocument()
  })

  it('renders a flow run and follows live node updates', async () => {
    const snapshot = {
      kind: 'flow_run',
      run_id: 'r1',
      flow_id: 'f1',
      flow_name: 'Thumbnails',
      status: 'running',
      progress: { done: 1, total: 3 },
    }
    // flows.run.get answers with the run record (node states).
    const record = (states: string[]) => ({
      run_id: 'r1',
      flow_id: 'f1',
      status: 'running',
      graph: { nodes: states.map((_, i) => ({ id: `n${i}`, type: 'text' })) },
      nodes: Object.fromEntries(states.map((state, i) => [`n${i}`, { state }])),
      outputs: {},
    })
    let current = record(['done', 'running', 'queued'])
    const rpc = withArtifact('application/vnd.agentos.flow-run+json', snapshot, 'run.json').handle(
      'flows.run.get',
      () => current,
    )
    await openChat(rpc)
    const status = await screen.findByTestId('flow-status')
    await waitFor(() => expect(status).toHaveTextContent('1/3'))
    current = record(['done', 'done', 'running'])
    act(() => rpc.emit('flows.node', { run_id: 'r1', node_id: 'n2', status: 'done' }))
    await waitFor(() => expect(screen.getByTestId('flow-status')).toHaveTextContent('2/3'))
  })
})

describe('trading cards', () => {
  it.each([
    ['dca', 'application/vnd.agentos.dca+json', 'dca/mandate-active.json'],
    ['dca list', 'application/vnd.agentos.dca+json', 'dca/mandates.json'],
    ['lp pool', 'application/vnd.agentos.lp+json', 'lp/pool.json'],
    ['lp positions', 'application/vnd.agentos.lp+json', 'lp/positions.json'],
  ])('renders %s read-only with Refresh and no write action', async (_label, mime, path) => {
    await openChat(withArtifact(mime, fixture(path), path.replace('/', '-')))
    const card = await waitFor(() => {
      const el = document.querySelector('[data-trading-card]')
      if (!el) throw new Error('no card yet')
      return el as HTMLElement
    })
    expect(within(card).getByRole('button', { name: 'Refresh' })).toBeInTheDocument()
    expect(within(card).getAllByRole('button')).toHaveLength(1)
    expect(card).toHaveTextContent('Read-only in the web console')
  })
})

describe('mobile chat layout', () => {
  it('has navigation, a title that opens Details as a sheet, and Panels', async () => {
    stubMatchMedia(
      (q) =>
        q.includes('max-width: 768px') ||
        q.includes('pointer: coarse') ||
        q.includes('reduced-motion'),
    )
    const { composer } = await openChat()
    const header = screen.getAllByRole('banner').find((b) => b.querySelector('h1'))!
    expect(within(header).getByRole('button', { name: 'Open navigation' })).toBeInTheDocument()
    expect(within(header).getByRole('button', { name: 'Panels' })).toBeInTheDocument()
    within(header).getByRole('heading', { level: 1 }).click()
    expect(await screen.findByRole('dialog', { name: 'Session details' })).toBeInTheDocument()
    // Inputs are at least 16px on touch so the browser does not zoom.
    expect(composer.className).toContain('touch:text-base')
    expect(composer.closest('[class*="safe-area-inset-bottom"]')).not.toBeNull()
    void KEY
  })
})
