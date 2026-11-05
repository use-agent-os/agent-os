import { readFileSync } from 'node:fs'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { order, renderDesk, WALLET } from '../test-utils'
import type { MandatePayload } from '../types'
import { PLACEHOLDERS } from './desk-logic'
import { useDeskFrame } from './TradingDesk'
import { useDeskInstruments } from './useDeskInstruments'
import { useTradingUi } from '~/stores/trading-ui'
import { useUi } from '~/stores/ui'
import type { DeskMode } from './mode-logic'

const rpcCall = vi.fn()
const rpcOn = vi.fn(() => () => {})
// One client for the whole test, as in the app: effects keyed on `rpc` must not re-run.
const rpc = { call: rpcCall, waitForConnection: async () => {}, on: rpcOn }
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal: vi.fn(async () => {}) } }),
  isDesktop: () => true,
}))
// The session filing is someone else's concern here.
const startFresh = vi.fn()
vi.mock('./useTradingSession', () => ({
  useTradingSession: () => ({ sessionKey: 'sk', ensureFiled: vi.fn(), startFresh }),
}))

const SESSION = 'agent:trading:webchat:trading-t1'
const sendText = vi.fn()
const queueText = vi.fn()

/** The same wiring SessionRoute → ChatView does: one frame, its desk into the instruments. */
function Harness({
  active,
  mode = 'trading',
  busy = false,
}: {
  active: boolean
  mode?: DeskMode
  busy?: boolean
}) {
  const frame = useDeskFrame({
    sessionKey: SESSION,
    mode,
    active,
    entering: false,
    onSwitchMode: () => {},
    onSessionPending: () => {},
  })
  const inst = useDeskInstruments(frame.desk, {
    sessionKey: SESSION,
    sendText,
    queueText,
    submitText: () => {},
    busy,
    composerValue: '',
    idle: true,
    hasMessages: false,
    focusOrderId: null,
    setFocusOrderId: () => {},
  })
  return (
    <div>
      {frame.strip}
      {frame.banner}
      {inst.region}
      {inst.dockAbove}
      {inst.seats}
      <span data-testid="composer-placeholder">{inst.placeholder}</span>
      <button type="button" onClick={frame.desk?.onStartFresh} data-testid="start-fresh">
        fresh
      </button>
    </div>
  )
}

function answers(overrides: Record<string, unknown> = {}) {
  return async (method: string) => {
    if (method in overrides) {
      const v = overrides[method]
      return typeof v === 'function' ? (v as () => unknown)() : v
    }
    switch (method) {
      case 'trading.status':
        return {
          enabled: true,
          apiKeyConfigured: true,
          chains: [{ chainId: 8453 }],
          limits: { approvalThresholdUsd: 100, dailyCapUsd: 1000, approvalTtlSeconds: 900 },
          unlockMode: 'auto',
          unlocked: true,
          syncing: false,
          lastSyncAt: null,
          provider: 'uniswap',
          providers: [],
        }
      case 'wallet.status':
        return { initialized: true, unlocked: true, unlockMode: 'auto', walletCount: 1 }
      case 'wallet.list':
        return { wallets: [WALLET], primary: WALLET.address }
      case 'trading.orders.list':
        return { orders: [order({ sessionKey: SESSION })], pendingApprovals: 1 }
      case 'cron.list':
        return { jobs: [] }
      default:
        return {}
    }
  }
}

beforeEach(() => {
  rpcCall.mockReset()
  rpcOn.mockClear()
  sendText.mockClear()
  queueText.mockClear()
  startFresh.mockClear()
  rpcCall.mockImplementation(answers())
})

describe('useDeskFrame', () => {
  it('listens to cron.run.finished once for the whole desk', async () => {
    renderDesk(<Harness active />)
    await screen.findByTestId('approval-card')
    const finished = rpcOn.mock.calls.filter((c) => (c as unknown[])[0] === 'cron.run.finished')
    expect(finished).toHaveLength(1)
  })

  it('runs no trading query and binds no listener in an ordinary chat', () => {
    renderDesk(<Harness active={false} mode="chat" />)
    expect(rpcCall).not.toHaveBeenCalled()
    expect(rpcOn).not.toHaveBeenCalled()
    expect(screen.getByTestId('mode-chat')).toHaveAttribute('aria-selected', 'true')
  })

  it('shows the pill in the derived mode while the desk is off', () => {
    renderDesk(<Harness active={false} mode="trading" />)
    expect(screen.getByTestId('mode-trading')).toHaveAttribute('aria-selected', 'true')
    expect(rpcCall).not.toHaveBeenCalled()
  })

  it('says the gateway did not answer, with a retry, instead of "create your vault"', async () => {
    let fail = true
    rpcCall.mockImplementation(
      answers({
        'wallet.status': () => {
          if (fail) throw new Error('boom')
          return { initialized: true, unlocked: true, unlockMode: 'auto', walletCount: 1 }
        },
      }),
    )
    renderDesk(<Harness active />)
    const retry = await screen.findByTestId('desk-retry')
    expect(screen.getByTestId('desk-gate')).toHaveTextContent('Waiting for the gateway')
    expect(screen.queryByTestId('vault-setup')).toBeNull()
    fail = false
    fireEvent.click(retry)
    await waitFor(() => expect(screen.queryByTestId('desk-retry')).toBeNull())
  })
})

describe('useDeskInstruments · reject', () => {
  it('rejects once for a double Enter on the reason, and posts one chat message', async () => {
    let release: (() => void) | null = null
    rpcCall.mockImplementation(
      answers({
        'trading.orders.reject': () =>
          new Promise<unknown>((resolve) => {
            release = () => resolve({ order: order({ status: 'rejected' }) })
          }),
      }),
    )
    renderDesk(<Harness active />)
    await screen.findByTestId('approval-card')
    fireEvent.click(screen.getByTestId('card-reject'))
    const reason = screen.getByTestId('reject-reason')
    fireEvent.change(reason, { target: { value: 'not today' } })
    fireEvent.keyDown(reason, { key: 'Enter' })
    fireEvent.keyDown(reason, { key: 'Enter' })
    const rejects = () => rpcCall.mock.calls.filter((c) => c[0] === 'trading.orders.reject')
    await waitFor(() => expect(rejects()).toHaveLength(1))
    expect(rejects()[0]?.[1]).toEqual({ orderId: 'o1', reason: 'not today' })
    // Once the pending state has landed the card is locked too.
    await waitFor(() => expect(screen.getByTestId('card-reject')).toBeDisabled())
    fireEvent.keyDown(screen.getByTestId('reject-reason'), { key: 'Enter' })
    fireEvent.click(screen.getByTestId('card-reject'))
    await act(async () => {
      release?.()
      await Promise.resolve()
    })
    await waitFor(() => expect(sendText).toHaveBeenCalledTimes(1))
    expect(String(sendText.mock.calls[0]?.[0])).toBe('Rejected order o1: not today')
    expect(rejects()).toHaveLength(1)
  })
})

describe('useDeskInstruments · reject with an empty note', () => {
  it('sends no reason at all, and the agent still hears it was rejected', async () => {
    rpcCall.mockImplementation(
      answers({ 'trading.orders.reject': () => ({ order: order({ status: 'rejected' }) }) }),
    )
    renderDesk(<Harness active />)
    await screen.findByTestId('approval-card')
    fireEvent.click(screen.getByTestId('card-reject'))
    fireEvent.keyDown(screen.getByTestId('reject-reason'), { key: 'Enter' })
    const rejects = () => rpcCall.mock.calls.filter((c) => c[0] === 'trading.orders.reject')
    await waitFor(() => expect(rejects()).toHaveLength(1))
    // Not `reason: 'user'`: the engine prefixes "user: " and stored "user: user".
    expect(rejects()[0]?.[1]).toEqual({ orderId: 'o1' })
    await waitFor(() => expect(sendText).toHaveBeenCalledTimes(1))
    expect(String(sendText.mock.calls[0]?.[0])).toBe(
      'Rejected order o1. Do not retry it without asking first.',
    )
  })
})

describe('useDeskInstruments · orders placed without a session', () => {
  const kinds = ['swap', 'send', 'lp_collect', 'lp_remove', 'lp_add'] as const
  it.each(kinds)('shows the full card for a %s the operator placed from the CLI', async (kind) => {
    rpcCall.mockImplementation(
      answers({
        'trading.orders.list': () => ({
          orders: [
            order({ orderId: 'cli', kind, sessionKey: null, initiator: 'manual' }),
            // Another chat's ask stays that chat's.
            order({ orderId: 'elsewhere', sessionKey: 'agent:trading:webchat:other' }),
          ],
          pendingApprovals: 2,
        }),
      }),
    )
    renderDesk(<Harness active />)
    const cards = await screen.findAllByTestId('approval-card')
    expect(cards).toHaveLength(1)
    expect(document.querySelector('[data-order="cli"]')).not.toBeNull()
    expect(document.querySelector('[data-order="elsewhere"]')).toBeNull()
  })

  it('tells no agent when an operator order is rejected: nobody asked', async () => {
    rpcCall.mockImplementation(
      answers({
        'trading.orders.list': () => ({
          orders: [order({ orderId: 'cli', kind: 'lp_remove', sessionKey: null })],
          pendingApprovals: 1,
        }),
        'trading.orders.reject': () => ({ order: order({ orderId: 'cli', status: 'rejected' }) }),
      }),
    )
    renderDesk(<Harness active />)
    await screen.findByTestId('approval-card')
    fireEvent.click(screen.getByTestId('card-reject'))
    fireEvent.keyDown(screen.getByTestId('reject-reason'), { key: 'Enter' })
    await waitFor(() =>
      expect(rpcCall.mock.calls.filter((c) => c[0] === 'trading.orders.reject')).toHaveLength(1),
    )
    await act(async () => {
      await Promise.resolve()
    })
    expect(sendText).not.toHaveBeenCalled()
    expect(queueText).not.toHaveBeenCalled()
  })
})

describe('useDeskInstruments · reject while the turn streams', () => {
  it('queues the reason for the next turn instead of dropping it', async () => {
    rpcCall.mockImplementation(
      answers({ 'trading.orders.reject': () => ({ order: order({ status: 'rejected' }) }) }),
    )
    renderDesk(<Harness active busy />)
    await screen.findByTestId('approval-card')
    fireEvent.click(screen.getByTestId('card-reject'))
    const reason = screen.getByTestId('reject-reason')
    fireEvent.change(reason, { target: { value: 'not today' } })
    fireEvent.keyDown(reason, { key: 'Enter' })
    await waitFor(() => expect(queueText).toHaveBeenCalledTimes(1))
    expect(String(queueText.mock.calls[0]?.[0])).toBe('Rejected order o1: not today')
    // A direct send is a no-op while a turn streams; it must not be tried.
    expect(sendText).not.toHaveBeenCalled()
  })
})

describe('useDeskInstruments · a multisend card is the whole batch', () => {
  const A = '0x2222222222222222222222222222222222222222'
  const leg = (orderId: string, recipient: string) =>
    order({
      orderId,
      kind: 'send',
      batchId: 'bat_1',
      recipient,
      recipientLabel: null,
      tokenOut: order().tokenIn,
      expectedOut: null,
      minOut: null,
      priceImpactPct: null,
      amountIn: '0.1',
      valueUsd: 10,
      sessionKey: SESSION,
    })
  it("shows every awaiting leg of the batch, not only the page's, and rejects for all of them", async () => {
    rpcCall.mockImplementation(
      answers({
        // The page holds one leg; the batch has three awaiting and one already settled.
        'trading.orders.list': () => ({ orders: [leg('a', A)], pendingApprovals: 1 }),
        'trading.orders.batch': () => ({
          batchId: 'bat_1',
          orders: [
            leg('a', A),
            leg('b', '0x3333333333333333333333333333333333333333'),
            leg('c', '0x4444444444444444444444444444444444444444'),
            { ...leg('d', '0x5555555555555555555555555555555555555555'), status: 'rejected' },
          ],
        }),
        'trading.orders.reject': () => ({ order: leg('a', A) }),
      }),
    )
    renderDesk(<Harness active />)
    const legs = await screen.findByTestId('card-legs')
    await waitFor(() => expect(legs).toHaveTextContent('3 recipients'))
    expect(screen.getByTestId('card-legs')).toHaveTextContent('0.3 ETH')
    expect(rpcCall.mock.calls.find((c) => c[0] === 'trading.orders.batch')?.[1]).toEqual({
      batchId: 'bat_1',
    })
    expect(screen.getAllByTestId('approval-card')).toHaveLength(1)
    fireEvent.click(screen.getByTestId('card-reject'))
    fireEvent.keyDown(screen.getByTestId('reject-reason'), { key: 'Enter' })
    await waitFor(() => expect(sendText).toHaveBeenCalledTimes(1))
    expect(String(sendText.mock.calls[0]?.[0])).toContain('batch bat_1 (3 sends)')
  })
})

describe('useDeskFrame · start fresh', () => {
  const job = { id: 'j1', name: 'DCA ETH', enabled: true, targetSessionKey: SESSION }
  it('mints a new session and leaves the old session’s missions running', async () => {
    const focusBefore = useUi.getState().composerFocusRequest
    rpcCall.mockImplementation(
      answers({
        'cron.list': () => ({ jobs: [job, { ...job, id: 'j2', name: 'Off', enabled: false }] }),
      }),
    )
    renderDesk(<Harness active />)
    await screen.findByTestId('approval-card')
    await waitFor(() => expect(rpcCall.mock.calls.some((c) => c[0] === 'cron.list')).toBe(true))
    await act(async () => {
      fireEvent.click(screen.getByTestId('start-fresh'))
      await Promise.resolve()
    })
    await waitFor(() => expect(startFresh).toHaveBeenCalledTimes(1))
    // The engine runs a mission whatever chat is open: nothing is paused
    // behind the user's back (a DCA paused this way stayed paused for good).
    expect(rpcCall.mock.calls.some((c) => c[0] === 'cron.update')).toBe(false)
    expect(rpcCall.mock.calls.some((c) => c[0] === 'trading.dca.pause')).toBe(false)
    // Issue #3524: the chat stays mounted across the new key, so the fresh
    // session asks it for the composer.
    expect(useUi.getState().composerFocusRequest).toBe(focusBefore + 1)
  })
})

// Issue #3519: the shell's ⌘N reaches the desk's "Start fresh" through the
// trading UI store, so it stays at the desk like the pen button does.
describe('useDeskFrame · start fresh from the shell', () => {
  const job = { id: 'j1', name: 'DCA ETH', enabled: true, targetSessionKey: SESSION }
  it('publishes start fresh while the desk is up, and pauses nothing', async () => {
    rpcCall.mockImplementation(answers({ 'cron.list': () => ({ jobs: [job] }) }))
    const view = renderDesk(<Harness active />)
    await screen.findByTestId('approval-card')
    await waitFor(() => expect(rpcCall.mock.calls.some((c) => c[0] === 'cron.list')).toBe(true))
    const fromShell = useTradingUi.getState().startFreshDesk
    expect(fromShell).toBeTypeOf('function')

    await act(async () => {
      fromShell!()
      await Promise.resolve()
    })
    await waitFor(() => expect(startFresh).toHaveBeenCalledTimes(1))
    expect(rpcCall.mock.calls.some((c) => c[0] === 'cron.update')).toBe(false)

    view.unmount()
    expect(useTradingUi.getState().startFreshDesk).toBeNull()
  })

  it('publishes nothing in an ordinary chat', () => {
    renderDesk(<Harness active={false} mode="chat" />)
    expect(useTradingUi.getState().startFreshDesk).toBeNull()
  })
})

describe('useDeskInstruments · the composer hint', () => {
  const PAYLOAD = JSON.parse(
    readFileSync('src/renderer/src/views/trading/desk/__fixtures__/dca/mandate.json', 'utf8'),
  ) as MandatePayload
  const dcaList = {
    version: 1,
    kind: 'mandates',
    fetchedAt: PAYLOAD.fetchedAt,
    warnings: [],
    mandates: [{ ...PAYLOAD.mandate, name: 'Test DCA C', sessionKey: SESSION }],
    totals: { count: 1, active: 1, spentUsd: 120, capUsd: 300, acquiredUsd: null },
  }

  it('keeps the normal hint while a DCA mandate runs', async () => {
    rpcCall.mockImplementation(answers({ 'trading.dca.list': dcaList }))
    renderDesk(<Harness active />)
    // The mandate is on the desk (its row above the composer names it)…
    await screen.findByTestId('mandate-row')
    // …and the composer still says what it always says.
    const hint = screen.getByTestId('composer-placeholder')
    expect(hint).not.toHaveTextContent('Test DCA C')
    expect(PLACEHOLDERS).toContain(hint.textContent)
  })

  it('names a DCA once, in its row; the dot strip above the composer leaves it out', async () => {
    rpcCall.mockImplementation(answers({ 'trading.dca.list': dcaList }))
    renderDesk(<Harness active />)
    await screen.findByTestId('mandate-row')
    expect(screen.getAllByText('Test DCA C')).toHaveLength(1)
    expect(screen.queryByTestId('mission-strip')).toBeNull()
  })

  it('keeps a cron mission’s state in the dot strip: its row has no state word', async () => {
    rpcCall.mockImplementation(
      answers({
        'trading.dca.list': dcaList,
        'cron.list': {
          jobs: [
            {
              id: 'j1',
              name: 'Watch ETH',
              enabled: true,
              sessionKey: SESSION,
              scheduleKind: 'every',
              scheduleRaw: 3600,
            },
          ],
        },
      }),
    )
    renderDesk(<Harness active />)
    const strip = await screen.findByTestId('mission-strip')
    expect(strip).toHaveTextContent(/^Watch ETH/)
    expect(strip.querySelector('.trd-mstrip__word')).not.toBeEmptyDOMElement()
    expect(strip).not.toHaveTextContent('Test DCA C')
  })

  it('still puts a cron mission’s state in the hint', async () => {
    rpcCall.mockImplementation(
      answers({
        'trading.dca.list': dcaList,
        'cron.list': {
          jobs: [
            {
              id: 'j1',
              name: 'Watch ETH',
              enabled: true,
              sessionKey: SESSION,
              scheduleKind: 'every',
              scheduleRaw: 3600,
            },
          ],
        },
      }),
    )
    renderDesk(<Harness active />)
    await waitFor(() =>
      expect(screen.getByTestId('composer-placeholder')).toHaveTextContent(/^Watch ETH · /),
    )
  })
})
