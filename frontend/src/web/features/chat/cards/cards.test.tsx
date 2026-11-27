import '@/test/web'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FakeArtifactGateway } from '@/test/artifact-gateway'
import { stubMatchMedia } from '@/test/web'
import {
  KEY,
  assistantRow,
  chatRpc,
  emit,
  openChat,
  resetChatState,
  userRow,
} from '@/test/web-chat'

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

/**
 * A chat whose reply carries one card artifact, served by a gateway that
 * answers as the real one does: through a ticket only.
 */
function cardChat(mime: string, body: unknown, name = 'card.json') {
  const gateway = new FakeArtifactGateway(KEY).put(`art-${name}`, body, { name, mime })
  const rpc = chatRpc([
    userRow('u1', 'show'),
    assistantRow('a1', 'Here.', { artifacts: [gateway.artifact(`art-${name}`)] }),
  ])
  gateway.install(rpc)
  return { rpc, gateway }
}

function withArtifact(mime: string, body: unknown, name = 'card.json') {
  return cardChat(mime, body, name).rpc
}

const CARDS_BODY = {
  type: 'cards',
  title: 'Tokens',
  cards: [{ title: 'ETH', fields: [{ label: 'Price', value: '$3,000' }] }],
}

async function tradingCard(): Promise<HTMLElement> {
  return waitFor(() => {
    const el = document.querySelector('[data-trading-card]')
    if (!el) throw new Error('no card yet')
    return el as HTMLElement
  })
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
    await openChat(withArtifact('application/vnd.agentos.cards+json', CARDS_BODY))
    expect(await screen.findByText('ETH')).toBeInTheDocument()
    expect(screen.getByText('$3,000')).toBeInTheDocument()
  })

  it('reads the payload through a ticket for its session, never the bare artifact URL', async () => {
    // A token in the tab must not ride along: the ticket is the credential.
    sessionStorage.setItem('agentos.wsToken', 'tokA')
    const { rpc, gateway } = cardChat('application/vnd.agentos.cards+json', CARDS_BODY)
    await openChat(rpc)
    expect(await screen.findByText('ETH')).toBeInTheDocument()
    expect(rpc.callsTo('artifacts.download')).toEqual([
      { session_key: KEY, artifact_id: 'art-card.json', transport: 'http' },
    ])
    expect(gateway.legacyRequests).toEqual([])
    expect(gateway.ticketUrls()).toHaveLength(1)
    for (const [, init] of vi.mocked(fetch).mock.calls) {
      expect(new Headers(init?.headers).has('authorization')).toBe(false)
    }
    sessionStorage.clear()
  })

  it("says why when the gateway refuses the artifact (another session's file)", async () => {
    const gateway = new FakeArtifactGateway(KEY).put('art-x', CARDS_BODY, {
      name: 'x.json',
      mime: 'application/vnd.agentos.cards+json',
      sessionKey: 'agent:main:webchat:other',
    })
    const rpc = chatRpc([
      userRow('u1', 'show'),
      assistantRow('a1', 'Here.', { artifacts: [gateway.artifact('art-x')] }),
    ])
    gateway.install(rpc)
    await openChat(rpc)
    expect(
      await screen.findByText("Couldn't load this card: artifact not found", {}, { timeout: 3000 }),
    ).toBeInTheDocument()
    expect(gateway.requests).toEqual([])
  })

  it('renders a card that arrives live, through a ticket', async () => {
    const gateway = new FakeArtifactGateway(KEY).put('art-live', CARDS_BODY, {
      name: 'live.json',
      mime: 'application/vnd.agentos.cards+json',
    })
    const rpc = chatRpc([userRow('u1', 'show')])
    gateway.install(rpc)
    await openChat(rpc)
    // Mid-turn, as the engine publishes it.
    emit(rpc, 'text_delta', { text: 'Charting' })
    emit(rpc, 'artifact', { ...gateway.artifact('art-live'), epoch: 0 })
    expect(await screen.findByText('ETH')).toBeInTheDocument()
    expect(rpc.callsTo('artifacts.download')[0]).toMatchObject({ artifact_id: 'art-live' })
    expect(gateway.legacyRequests).toEqual([])
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
    const card = await tradingCard()
    expect(within(card).getByRole('button', { name: 'Refresh' })).toBeInTheDocument()
    expect(within(card).getAllByRole('button')).toHaveLength(1)
    expect(card).toHaveTextContent('Read-only in the web console')
  })

  it('Refresh reads the card again, with a new ticket once the socket changed', async () => {
    const user = userEvent.setup()
    const { rpc, gateway } = cardChat(
      'application/vnd.agentos.lp+json',
      fixture('lp/positions.json'),
      'lp.json',
    )
    await openChat(rpc)
    expect(await tradingCard()).toHaveTextContent('3 rows')
    gateway.put('art-lp.json', fixture('lp/positions-empty.json'), {
      name: 'lp.json',
      mime: 'application/vnd.agentos.lp+json',
    })
    // A reconnect: the old socket's tickets are gone.
    act(() => {
      rpc.setState('connecting')
      rpc.setState('connected')
    })
    gateway.dropTickets()
    await user.click(within(await tradingCard()).getByRole('button', { name: 'Refresh' }))
    await waitFor(async () => expect(await tradingCard()).toHaveTextContent('0 rows'))
    expect(rpc.callsTo('artifacts.download')).toHaveLength(2)
    expect(gateway.legacyRequests).toEqual([])
  })
})

describe('mobile chat layout', () => {
  it('leaves navigation to the shell top bar, has a title that renames in place, and Panels', async () => {
    stubMatchMedia(
      (q) =>
        q.includes('max-width: 768px') ||
        q.includes('pointer: coarse') ||
        q.includes('reduced-motion'),
    )
    const { composer } = await openChat()
    const header = screen.getAllByRole('banner').find((b) => b.querySelector('h1'))!
    // One ☰ on phones: the shell's top bar owns it, not the session header.
    expect(within(header).queryByRole('button', { name: 'Open navigation' })).toBeNull()
    expect(within(header).getByRole('button', { name: 'Show workspace panel' })).toBeInTheDocument()
    // The route picker gives way on a narrow composer; Send was pushed past its edge at 360 px.
    expect((await screen.findByRole('button', { name: /^Model route: / })).className).toMatch(
      /(^|\s)min-w-0(\s|$)/,
    )
    within(header).getByRole('heading', { level: 1 }).click()
    expect(await within(header).findByRole('textbox')).toBeInTheDocument()
    // Inputs are at least 16px on touch so the browser does not zoom.
    expect(composer.className).toContain('touch:text-base')
    expect(composer.closest('[class*="safe-area-inset-bottom"]')).not.toBeNull()
    void KEY
  })
})
