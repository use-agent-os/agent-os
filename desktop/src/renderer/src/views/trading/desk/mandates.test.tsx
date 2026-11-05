import { readFileSync } from 'node:fs'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, renderHook, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { order, renderDesk, WALLET } from '../test-utils'
import type { Mandate, MandatePayload } from '../types'
import { ApprovalsRegion } from './ApprovalsRegion'
import { MandateCard } from './MandateCard'
import {
  countdown,
  dcaFormFromMandate,
  dcaUpdatePatch,
  deskMandates,
  everyAfter,
  everyShort,
  mandateChip,
  mandateRows,
  mandateState,
  parseEvery,
} from './mandate-logic'
import { MissionControls, mandateWord } from './MissionControls'
import { useMissions } from './missions'
import { StatusStrip } from './StatusStrip'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
const toasts = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}))
vi.mock('sonner', () => ({ toast: toasts }))

const PAYLOAD = JSON.parse(
  readFileSync('src/renderer/src/views/trading/desk/__fixtures__/dca/mandate.json', 'utf8'),
) as MandatePayload
const SESSION = 'agent:trading:webchat:desk-test'
// The fixture's next buy is 2026-09-28T09:00Z; the clock sits 3 h 12 m before it.
const NOW = Date.parse('2026-09-28T05:48:00Z')

function mandate(extra: Partial<Mandate> = {}): Mandate {
  return { ...PAYLOAD.mandate, ...extra }
}

const PENDING = mandate({
  id: 'dca_99',
  name: 'DCA cbBTC',
  status: 'awaiting_approval',
  initiator: 'agent',
  approvedAt: null,
  expiresAt: '2026-09-29T06:00:00Z',
  budget: { ...PAYLOAD.mandate.budget, usdPerRun: 150, spentUsd: 0, reservedUsd: 0, progress: 0 },
  guards: { ...PAYLOAD.mandate.guards, buysNeedApproval: true },
  runs: { done: 0, max: null, skipped: 0, failed: 0, attempts: 0 },
  history: [],
})

describe('mandate logic', () => {
  it('reads the engine figures into words', () => {
    expect(countdown(3 * 3_600_000 + 12 * 60_000)).toBe('3 h 12 m')
    expect(countdown(12 * 60_000)).toBe('12 m')
    expect(countdown(30_000)).toBe('< 1 m')
    expect(countdown(2 * 86_400_000 + 4 * 3_600_000)).toBe('2 d 4 h')
    expect(everyAfter(3_600)).toBe('1 h')
    expect(everyAfter(86_400)).toBe('1 d')
    expect(everyAfter(1_800)).toBe('30 min')
    expect(parseEvery('30m')).toBe(1_800)
    expect(parseEvery('1d')).toBe(86_400)
    expect(parseEvery('3600')).toBe(3_600)
    expect(parseEvery('soon')).toBeNull()
    expect(everyShort(86_400)).toBe('day')
    expect(everyShort(21_600)).toBe('6 h')
    expect(mandateState(mandate(), NOW)).toEqual({
      key: 'trading.dca.state.active',
      next: '3 h 12 m',
      due: false,
    })
    expect(mandateState(mandate(), Date.parse('2026-09-28T09:00:05Z')).due).toBe(true)
    expect(mandateState(PENDING, NOW).key).toBe('trading.dca.state.awaiting')
  })

  it('counts down in one rule, rounding down: never "59 m" here and "1 h 0 m" there', () => {
    const at = Date.parse(PAYLOAD.mandate.schedule.nextRunAt!)
    const word = (msLeft: number) => mandateWord(mandate(), at - msLeft)
    // 59 m 40 s left is 59 m, whoever asks.
    expect(countdown(59 * 60_000 + 40_000)).toBe('59 m')
    expect(word(59 * 60_000 + 40_000)).toBe('Active · next 59 m')
    expect(countdown(60 * 60_000)).toBe('1 h 0 m')
    expect(countdown(60 * 60_000 + 59_999)).toBe('1 h 0 m')
    expect(countdown(23 * 3_600_000 + 59 * 60_000 + 59_000)).toBe('23 h 59 m')
    // At the moment itself, and past it, the buy is due, not "next 1 m".
    expect(word(0)).toBe('Active · buy due')
    expect(word(-90_000)).toBe('Active · buy due')
    // The status strip's chip reads the same words.
    expect(mandateChip([mandate()], at - (59 * 60_000 + 40_000))).toEqual({
      count: 1,
      word: 'next',
      next: '59 m',
    })
    expect(mandateChip([mandate()], at)).toEqual({ count: 1, word: 'due', next: null })
  })

  it('summarises the live mandates in one chip: soonest buy, or the one awaiting you', () => {
    const later = mandate({
      id: 'dca_l',
      schedule: { ...PAYLOAD.mandate.schedule, nextRunAt: '2026-09-28T12:00:00Z' },
    })
    const soon = mandate({
      id: 'dca_s',
      schedule: { ...PAYLOAD.mandate.schedule, nextRunAt: '2026-09-28T06:00:00Z' },
    })
    const done = mandate({ id: 'dca_d', status: 'completed' })
    expect(mandateChip([later, soon, done], NOW)).toEqual({ count: 2, word: 'next', next: '12 m' })
    expect(mandateChip([later, PENDING], NOW)).toEqual({
      count: 2,
      word: 'awaiting',
      next: null,
    })
    expect(mandateChip([mandate({ status: 'paused' })], NOW)?.word).toBe('paused')
    expect(mandateChip([done], NOW)).toBeNull()
  })

  it('lists every live mandate, and this chat’s finished ones for an hour only', () => {
    const other = mandate({ id: 'dca_o', sessionKey: 'agent:trading:webchat:other' })
    const unfiled = mandate({ id: 'dca_u', sessionKey: null })
    // NOW is 05:48: 05:00 is 48 minutes ago, 04:40 over an hour.
    const done = mandate({ id: 'dca_d', status: 'completed', updatedAt: '2026-09-28T05:00:00Z' })
    const doneEarlier = mandate({
      id: 'dca_e',
      status: 'completed',
      updatedAt: '2026-09-28T04:40:00Z',
    })
    const stopped = mandate({ id: 'dca_s', status: 'stopped', updatedAt: '2026-09-28T05:30:00Z' })
    const rejected = mandate({ id: 'dca_r', status: 'rejected', updatedAt: '2026-09-28T05:10:00Z' })
    const expired = mandate({ id: 'dca_x', status: 'expired', updatedAt: '2026-09-28T05:20:00Z' })
    const ids = deskMandates(
      [done, mandate(), other, unfiled, doneEarlier, stopped, rejected, expired],
      SESSION,
      NOW,
    ).map((m) => m.id)
    // Live first — another chat's too, so nothing trades unseen — then this
    // chat's finished ones newest first, whatever their status.
    expect(ids).toEqual(['dca_1a2b3c4d', 'dca_o', 'dca_u', 'dca_s', 'dca_x', 'dca_r', 'dca_d'])
    // Another chat's finished mandate is that chat's to review.
    expect(
      deskMandates(
        [mandate({ id: 'dca_od', status: 'stopped', sessionKey: 'agent:trading:webchat:other' })],
        SESSION,
        NOW,
      ),
    ).toEqual([])
    // An hour after it ended, a finished mandate is gone.
    expect(
      deskMandates([stopped], SESSION, Date.parse('2026-09-28T06:30:00Z')).map((m) => m.id),
    ).toEqual([])
  })

  it('draws at most two finished rows and counts the rest', () => {
    const listed = deskMandates(
      [
        mandate(),
        mandate({ id: 'a', status: 'completed', updatedAt: '2026-09-28T05:40:00Z' }),
        mandate({ id: 'b', status: 'stopped', updatedAt: '2026-09-28T05:45:00Z' }),
        mandate({ id: 'c', status: 'rejected', updatedAt: '2026-09-28T05:30:00Z' }),
        mandate({ id: 'd', status: 'expired', updatedAt: '2026-09-28T05:20:00Z' }),
      ],
      SESSION,
      NOW,
    )
    const { rows, more } = mandateRows(listed)
    expect(rows.map((m) => m.id)).toEqual(['dca_1a2b3c4d', 'b', 'a'])
    expect(more).toBe(2)
    expect(mandateRows([mandate()]).more).toBe(0)
    // Opened, every finished one is drawn and the fold count stays for "show fewer".
    const all = mandateRows(listed, true)
    expect(all.rows.map((m) => m.id)).toEqual(['dca_1a2b3c4d', 'b', 'a', 'c', 'd'])
    expect(all.more).toBe(2)
  })

  it('clears a limit on update with a 0, as the engine reads it', () => {
    const m = mandate()
    const form = { ...dcaFormFromMandate(m), runs: '', maxPrice: '' }
    expect(dcaUpdatePatch(form, m)).toEqual({ runsMax: 0, maxPriceUsd: 0 })
    expect(dcaUpdatePatch(dcaFormFromMandate(m), m)).toEqual({})
  })
})

describe('MandateCard', () => {
  it('says the whole mandate before the one button that starts it', () => {
    const onApprove = vi.fn()
    const onReject = vi.fn()
    renderDesk(
      <MandateCard
        mandate={PENDING}
        wallets={[WALLET]}
        deciding={false}
        onApprove={onApprove}
        onReject={onReject}
      />,
    )
    const card = screen.getByTestId('mandate-card')
    expect(card).toHaveAttribute('data-status', 'awaiting_approval')
    expect(screen.getByTestId('mandate-legs')).toHaveTextContent('ETH←USDC$150 every day')
    const fact = (key: string) => card.querySelector(`[data-fact='${key}'] dd`)?.textContent
    expect(fact('buy')).toBe('$150 of USDC')
    expect(fact('every')).toBe('every day')
    expect(fact('cap')).toBe('$300')
    expect(fact('buys')).toBe('until the cap')
    expect(fact('maxPrice')).toBe('$3,000')
    expect(fact('wallet')).toBe('Main · 0x1111…1111')
    expect(fact('chain')).toBe('Base')
    expect(fact('first')).toBe('on approval')
    // Short enough to sit whole: "Sep 29 06:00", no comma, no AM/PM (any time zone).
    expect(fact('expires')).toMatch(/^Sep (28|29) \d{2}:\d{2}$/)
    // The proposal's deadline, not the DCA's end: it says so.
    expect(card.querySelector(`[data-fact='expires'] dt`)).toHaveTextContent('Decide by')
    expect(screen.getByTestId('mandate-needs-approval')).toBeInTheDocument()
    expect(screen.getByTestId('mandate-warnings')).toHaveTextContent(
      'Each buy of $150 is above the $100 approval threshold and will wait for you.',
    )
    fireEvent.click(screen.getByTestId('mandate-approve'))
    expect(onApprove).toHaveBeenCalledWith(PENDING)
    expect(screen.getByTestId('mandate-approve')).toHaveTextContent('Approve & start')
    fireEvent.click(screen.getByTestId('mandate-reject'))
    expect(onReject).toHaveBeenCalledWith(PENDING)
  })

  it('says a later first buy as the interval after approval', () => {
    renderDesk(
      <MandateCard
        mandate={{
          ...PENDING,
          schedule: { ...PENDING.schedule, startNow: false, everySeconds: 3_600 },
        }}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    )
    expect(
      screen.getByTestId('mandate-card').querySelector(`[data-fact='first'] dd`),
    ).toHaveTextContent(/^after 1 h$/)
  })

  it('locks both buttons while the decision is in flight', () => {
    renderDesk(
      <MandateCard
        mandate={PENDING}
        wallets={[WALLET]}
        deciding
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    )
    expect(screen.getByTestId('mandate-approve')).toBeDisabled()
    expect(screen.getByTestId('mandate-reject')).toBeDisabled()
  })

  it('docks above the pending orders in the approvals region', () => {
    const onApproveMandate = vi.fn()
    renderDesk(
      <ApprovalsRegion
        pending={[order({ orderId: 'o1', status: 'awaiting_approval', initiator: 'agent' })]}
        settled={[]}
        wallets={[WALLET]}
        deciding={null}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOrderId={null}
        mandates={[PENDING]}
        onApproveMandate={onApproveMandate}
        onRejectMandate={vi.fn()}
      />,
    )
    const region = screen.getByTestId('approvals-region')
    const cards = [...region.querySelectorAll('article')]
    expect(cards[0]).toHaveAttribute('data-testid', 'mandate-card')
    expect(cards[1]).toHaveAttribute('data-testid', 'approval-card')
    fireEvent.click(within(cards[0] as HTMLElement).getByTestId('mandate-approve'))
    expect(onApproveMandate).toHaveBeenCalledWith(PENDING)
  })

  it('renders the region for a pending mandate alone', () => {
    renderDesk(
      <ApprovalsRegion
        pending={[]}
        settled={[]}
        wallets={[WALLET]}
        deciding={null}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOrderId={null}
        mandates={[PENDING]}
      />,
    )
    expect(screen.getByTestId('mandate-card')).toBeInTheDocument()
  })
})

describe('mandates among the missions', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
  })
  afterEach(() => vi.useRealTimers())

  function controls(mandates: Mandate[], extra: Record<string, unknown> = {}) {
    const handlers = {
      onMandatePause: vi.fn(),
      onMandateResume: vi.fn(),
      onMandateRun: vi.fn(),
      onMandateEdit: vi.fn(),
      onMandateStop: vi.fn(),
    }
    renderDesk(
      <MissionControls
        missions={[]}
        running={new Set()}
        pendingApprovals={0}
        busy={false}
        onStart={vi.fn()}
        onEdit={vi.fn()}
        onRun={vi.fn()}
        onSetEnabled={vi.fn()}
        onRemove={vi.fn()}
        showStart={false}
        mandates={mandates}
        {...handlers}
        {...extra}
      />,
    )
    return handlers
  }

  it('shows an active mandate with its countdown, spent of cap, and Pause · Buy now · Edit · Stop', () => {
    const h = controls([mandate()])
    const row = screen.getByTestId('mandate-row')
    expect(row).toHaveAttribute('data-state', 'active')
    expect(row).toHaveTextContent('DCA ETH')
    expect(screen.getByTestId('mandate-word')).toHaveTextContent('Active · next 3 h 12 m')
    expect(screen.getByTestId('mandate-runs')).toHaveTextContent('12/30 buys · 1 skipped')
    expect(screen.getByTestId('mandate-progress')).toHaveTextContent('$120 / $300')
    expect(screen.getByTestId('mandate-progress')).toHaveAttribute('aria-valuenow', '40')
    expect(screen.queryByTestId('mandate-resume')).toBeNull()
    fireEvent.click(screen.getByTestId('mandate-pause'))
    expect(h.onMandatePause).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('mandate-run'))
    expect(h.onMandateRun).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('mandate-edit'))
    expect(h.onMandateEdit).toHaveBeenCalledWith(expect.objectContaining({ id: 'dca_1a2b3c4d' }))
  })

  it('asks for a second click before a Stop, inline and never through a dialog', () => {
    const confirm = vi.spyOn(window, 'confirm')
    const h = controls([mandate()])
    fireEvent.click(screen.getByTestId('mandate-stop'))
    expect(h.onMandateStop).not.toHaveBeenCalled()
    expect(screen.getByTestId('mandate-stop')).toHaveTextContent('Stop — click again')
    fireEvent.click(screen.getByTestId('mandate-stop'))
    expect(h.onMandateStop).toHaveBeenCalledTimes(1)
    expect(confirm).not.toHaveBeenCalled()
  })

  it('offers Resume on a paused mandate, only a pointer on a pending one, nothing on a done one', () => {
    controls([
      mandate({ id: 'p', status: 'paused', statusReason: 'user' }),
      PENDING,
      mandate({ id: 'd', status: 'completed', statusReason: 'cap reached' }),
    ])
    const [paused, pending, done] = screen.getAllByTestId('mandate-row') as HTMLElement[]
    expect(within(paused!).getByTestId('mandate-word')).toHaveTextContent('Paused')
    expect(within(paused!).getByTestId('mandate-resume')).toBeInTheDocument()
    expect(within(paused!).getByTestId('mandate-run')).toBeInTheDocument()
    expect(within(pending!).getByTestId('mandate-word')).toHaveTextContent('Awaiting approval')
    expect(within(pending!).queryByTestId('mandate-pause')).toBeNull()
    expect(within(pending!).queryByTestId('mandate-stop')).toBeNull()
    expect(within(done!).getByTestId('mandate-word')).toHaveTextContent('Done')
    expect(within(done!).queryByRole('button')).toBeNull()
  })

  it('locks the row of a mandate with a write in flight', () => {
    controls([mandate()], { mandateBusy: 'dca_1a2b3c4d' })
    expect(screen.getByTestId('mandate-pause')).toBeDisabled()
    expect(screen.getByTestId('mandate-run')).toBeDisabled()
  })

  it('never calls a DCA that bought nothing "Done", and says why it ended on hover', () => {
    controls([
      mandate({
        id: 'e',
        status: 'completed',
        statusReason: 'cap reached',
        updatedAt: '2026-09-28T05:40:00Z',
        runs: { done: 0, max: 1, skipped: 1, failed: 0, attempts: 1 },
        budget: { ...PAYLOAD.mandate.budget, capUsd: 0.08, spentUsd: 0, progress: 0 },
      }),
      mandate({
        id: 'f',
        status: 'stopped',
        statusReason: 'user',
        updatedAt: '2026-09-28T05:30:00Z',
        runs: { done: 2, max: null, skipped: 0, failed: 3, attempts: 5 },
      }),
    ])
    const [ended, stopped] = screen.getAllByTestId('mandate-row') as HTMLElement[]
    expect(within(ended!).getByTestId('mandate-word')).toHaveTextContent('Ended · no buys')
    expect(within(ended!).getByTestId('mandate-runs')).toHaveTextContent('0/1 buys · 1 skipped')
    expect(ended).toHaveAttribute('title', 'cap reached')
    expect(within(stopped!).getByTestId('mandate-word')).toHaveTextContent('Stopped')
    expect(within(stopped!).getByTestId('mandate-runs')).toHaveTextContent('2 buys · 3 failed')
    expect(stopped).toHaveAttribute('title', 'Stopped by you')
    // A live one has no reason to show.
    controls([mandate({ id: 'live', statusReason: 'something stale' })])
    const live = screen
      .getAllByTestId('mandate-row')
      .find((r) => r.getAttribute('data-mandate') === 'live')!
    expect(live).not.toHaveAttribute('title')
  })

  it('shows two finished rows at most, newest first, then "+N more"', () => {
    controls([
      mandate(),
      mandate({ id: 'b', status: 'stopped', updatedAt: '2026-09-28T05:45:00Z' }),
      mandate({ id: 'a', status: 'completed', updatedAt: '2026-09-28T05:40:00Z' }),
      mandate({ id: 'c', status: 'rejected', updatedAt: '2026-09-28T05:30:00Z' }),
    ])
    expect(screen.getAllByTestId('mandate-row').map((r) => r.getAttribute('data-mandate'))).toEqual(
      ['dca_1a2b3c4d', 'b', 'a'],
    )
    const toggle = screen.getByTestId('mandate-more')
    expect(toggle.tagName).toBe('BUTTON')
    expect(toggle).toHaveTextContent('+1 more')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    // The hidden one opens, and folds back.
    fireEvent.click(toggle)
    expect(screen.getAllByTestId('mandate-row').map((r) => r.getAttribute('data-mandate'))).toEqual(
      ['dca_1a2b3c4d', 'b', 'a', 'c'],
    )
    expect(screen.getByTestId('mandate-more')).toHaveTextContent('show fewer')
    expect(screen.getByTestId('mandate-more')).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(screen.getByTestId('mandate-more'))
    expect(screen.getAllByTestId('mandate-row')).toHaveLength(3)
    expect(screen.getByTestId('mandate-more')).toHaveTextContent('+1 more')
  })

  it('sums mandates in one status strip chip', () => {
    renderDesk(
      <StatusStrip mode="trading" onSwitchMode={vi.fn()} mandates={[mandate(), PENDING]} />,
    )
    const chip = screen.getByTestId('strip-mandates')
    expect(chip).toHaveTextContent(/^DCA ×2 · awaiting$/)
    expect(chip).toHaveAttribute('data-state', 'awaiting')
    // No names, no progress: one compact chip.
    expect(chip).not.toHaveTextContent('$')
  })

  it('gives the strip chip the row’s own countdown', () => {
    renderDesk(
      <>
        <MissionControls
          missions={[]}
          running={new Set()}
          pendingApprovals={0}
          busy={false}
          onStart={vi.fn()}
          onEdit={vi.fn()}
          onRun={vi.fn()}
          onSetEnabled={vi.fn()}
          onRemove={vi.fn()}
          mandates={[mandate()]}
        />
        <StatusStrip mode="trading" onSwitchMode={vi.fn()} mandates={[mandate()]} />
      </>,
    )
    expect(screen.getByTestId('strip-mandates')).toHaveTextContent(/^DCA · next 3 h 12 m$/)
    expect(screen.getByTestId('mandate-word')).toHaveTextContent('Active · next 3 h 12 m')
  })
})

describe('useMissions · mandates', () => {
  beforeEach(() => {
    useConnection.getState().setState('connected')
    rpcCall.mockReset()
    for (const fn of Object.values(toasts)) fn.mockReset()
  })

  function wrapper({ children }: { children: ReactNode }) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
    })
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }

  const other = mandate({ id: 'dca_o', sessionKey: 'agent:trading:webchat:other' })
  const unfiled = mandate({ id: 'dca_u', sessionKey: null })
  const pendingElsewhere = { ...PENDING, sessionKey: 'agent:main:webchat:x' }

  function answer(method: string, params: Record<string, unknown>) {
    if (method === 'cron.list') return []
    if (method === 'trading.dca.list') {
      return {
        version: 1,
        kind: 'mandates',
        fetchedAt: PAYLOAD.fetchedAt,
        warnings: [],
        mandates: params.all ? [mandate(), other, unfiled, pendingElsewhere] : [],
        totals: { count: 4, active: 3, spentUsd: 360, capUsd: 1200, acquiredUsd: null },
      }
    }
    if (method.startsWith('trading.dca.')) return PAYLOAD
    return {}
  }

  it('lists every live mandate and every pending one, whichever chat filed them', async () => {
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) =>
      answer(method, params),
    )
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.mandates.length).toBeGreaterThan(0))
    expect(rpcCall).toHaveBeenCalledWith('trading.dca.list', { all: true })
    expect(result.current.mandates.map((m) => m.id)).toEqual(['dca_1a2b3c4d', 'dca_o', 'dca_u'])
    expect(result.current.awaitingMandates.map((m) => m.id)).toEqual(['dca_99'])
    // Nothing is paused behind the user's back: the hook has no bulk pause.
    expect(rpcCall).not.toHaveBeenCalledWith('trading.dca.pause', expect.anything())
  })

  it('creates, steers and toasts a mandate through trading.dca.*', async () => {
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) =>
      answer(method, params),
    )
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.mandates.length).toBeGreaterThan(0))
    await act(async () => {
      await result.current.mandate.create({ chainId: 8453, token: 'ETH', usdPerRun: 10 })
    })
    expect(rpcCall).toHaveBeenCalledWith('trading.dca.create', {
      chainId: 8453,
      token: 'ETH',
      usdPerRun: 10,
    })
    expect(toasts.success).toHaveBeenLastCalledWith('DCA started · DCA ETH', { id: 'dca-new' })
    const m = result.current.mandates[0]!
    for (const [action, method] of [
      ['approve', 'trading.dca.approve'],
      ['pause', 'trading.dca.pause'],
      ['resume', 'trading.dca.resume'],
      ['run', 'trading.dca.run'],
      ['stop', 'trading.dca.stop'],
    ] as const) {
      await act(async () => {
        await result.current.mandate[action](m)
      })
      // The write, then the refetch its invalidation sets off.
      expect(rpcCall).toHaveBeenCalledWith(method, { mandateId: 'dca_1a2b3c4d' })
    }
    await act(async () => {
      await result.current.mandate.update(m, { capUsd: 500 })
    })
    expect(rpcCall).toHaveBeenCalledWith('trading.dca.update', {
      mandateId: 'dca_1a2b3c4d',
      capUsd: 500,
    })
  })

  it('says why when the engine refuses, and resolves null so the caller stays put', async () => {
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'trading.dca.stop') throw new Error('trading.dca.bad_state: already stopped')
      return answer(method, params)
    })
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.mandates.length).toBeGreaterThan(0))
    let res: unknown = 'unset'
    await act(async () => {
      res = await result.current.mandate.stop(result.current.mandates[0]!)
    })
    expect(res).toBeNull()
    expect(toasts.error).toHaveBeenCalledWith(
      'Could not update the DCA: trading.dca.bad_state: already stopped',
      { id: 'dca-dca_1a2b3c4d' },
    )
  })

  it('says what a Buy now did: bought, placed, parked, skipped and why, failed and why', async () => {
    const run = (extra: Partial<NonNullable<MandatePayload['run']>>) => ({
      ...PAYLOAD.mandate.history[0]!,
      manual: true,
      reason: null,
      ...extra,
    })
    let answerRun: unknown = null
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) =>
      method === 'trading.dca.run'
        ? { ...PAYLOAD, ...(answerRun ? { run: answerRun } : {}) }
        : answer(method, params),
    )
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.mandates.length).toBeGreaterThan(0))
    const m = result.current.mandates[0]!
    const id = { id: 'dca-dca_1a2b3c4d' }
    const buyNow = async (r: unknown) => {
      answerRun = r
      await act(async () => {
        await result.current.mandate.run(m)
      })
    }
    await buyNow(run({ status: 'skipped', reason: 'ETH at $3,148 above $3,100' }))
    expect(toasts.warning).toHaveBeenLastCalledWith(
      'Buy skipped: ETH at $3,148 above $3,100 · DCA ETH',
      id,
    )
    expect(toasts.success).not.toHaveBeenCalled()
    await buyNow(run({ status: 'failed', reason: 'quote failed' }))
    expect(toasts.error).toHaveBeenLastCalledWith('Buy failed: quote failed · DCA ETH', id)
    await buyNow(run({ status: 'filled' }))
    expect(toasts.success).toHaveBeenLastCalledWith('Bought · DCA ETH', id)
    await buyNow(run({ status: 'pending' }))
    expect(toasts.success).toHaveBeenLastCalledWith('Buy placed · DCA ETH', id)
    await buyNow(run({ status: 'parked' }))
    expect(toasts.info).toHaveBeenLastCalledWith(
      'Buy placed; it waits for your approval · DCA ETH',
      id,
    )
    // No `run` in the answer and no manual run in the history: said, not silent.
    await buyNow(null)
    expect(toasts.info).toHaveBeenLastCalledWith('No buy ran · DCA ETH', id)
  })
})
