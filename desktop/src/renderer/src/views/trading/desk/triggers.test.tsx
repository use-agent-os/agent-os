import { readFileSync } from 'node:fs'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, renderHook, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { useTouchIdPrompt } from '~/lib/biometric-gate'
import { order, renderDesk, WALLET } from '../test-utils'
import type { MandatePayload, Trigger, TriggerFire, TriggerPayload } from '../types'
import { ApprovalsRegion } from './ApprovalsRegion'
import { MissionControls, MissionStrip } from './MissionControls'
import { useMissions } from './missions'
import { StatusStrip } from './StatusStrip'
import { TriggerCard } from './TriggerCard'
import {
  deskTriggers,
  distanceText,
  heroText,
  isNear,
  nowText,
  planText,
  priceText,
  sizeText,
  triggerChip,
  triggerChipText,
  triggerRows,
  triggerWord,
} from './trigger-logic'

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
  readFileSync('src/renderer/src/views/trading/desk/__fixtures__/trigger/trigger.json', 'utf8'),
) as TriggerPayload
const MANDATE = (
  JSON.parse(
    readFileSync('src/renderer/src/views/trading/desk/__fixtures__/dca/mandate.json', 'utf8'),
  ) as MandatePayload
).mandate
const SESSION = 'agent:trading:webchat:desk-test'
const NOW = Date.parse('2026-10-04T06:00:00Z')

function trigger(extra: Partial<Trigger> = {}): Trigger {
  return { ...PAYLOAD.trigger, ...extra }
}

/** The fixture, armed and 0.3 % above its line. */
function near(extra: Partial<Trigger> = {}): Trigger {
  return trigger({
    market: { ...PAYLOAD.trigger.market, priceUsd: 3811.4, distancePct: -0.3 },
    ...extra,
  })
}

const PENDING = trigger({
  id: 'trg_99',
  status: 'awaiting_approval',
  approvedAt: null,
  armedAt: null,
  fires: [],
  expiresAt: '2026-10-05T08:00:00Z',
})

const BUY = trigger({
  id: 'trg_buy',
  name: 'Buy ETH under $3,500',
  kind: 'buy',
  condition: { ...PAYLOAD.trigger.condition, priceUsd: 3500, label: 'under $3,500' },
  action: {
    ...PAYLOAD.trigger.action,
    kind: 'buy',
    amountPct: null,
    amountUsd: 50,
    estimatedUsd: 50,
    needsApproval: false,
    label: 'buy $50 of ETH with USDC',
  },
})

const ALERT = trigger({
  id: 'trg_al',
  name: 'Alert ETH over $5,000',
  kind: 'alert',
  condition: {
    ...PAYLOAD.trigger.condition,
    direction: 'above',
    priceUsd: 5000,
    label: 'over $5,000',
  },
  action: {
    ...PAYLOAD.trigger.action,
    kind: 'alert',
    amountPct: null,
    estimatedUsd: null,
    needsApproval: false,
    label: 'notify',
  },
  market: { ...PAYLOAD.trigger.market, balance: null, distancePct: 28.8 },
})

describe('trigger logic', () => {
  it('says prices and distances the way a trader reads them', () => {
    expect(priceText(3790)).toBe('$3,790')
    expect(priceText(3881.6)).toBe('$3,882')
    expect(priceText(1.25)).toBe('$1.25')
    expect(priceText(0.9998)).toBe('$0.9998')
    expect(priceText(0.00001234)).toBe('$0.00001234')
    expect(priceText(null)).toBe('—')
    expect(distanceText(-2.1)).toBe('−2.1 %')
    expect(distanceText(4)).toBe('+4.0 %')
    expect(distanceText(0)).toBe('0 %')
    expect(distanceText(-0.05)).toBe('−0.05 %')
    expect(distanceText(null)).toBe('')
  })

  it('reads the sentence, the size and the plan off the engine figures', () => {
    expect(heroText(trigger())).toBe('sell 50 % of ETH when under $3,800')
    expect(heroText(BUY)).toBe('buy $50 of ETH when under $3,500')
    expect(heroText(ALERT)).toBe('notify when ETH is over $5,000')
    const fixed = trigger({
      action: {
        ...PAYLOAD.trigger.action,
        amountPct: null,
        amount: { raw: '50000000000000000', human: '0.05', usd: 194 },
      },
    })
    expect(heroText(fixed)).toBe('sell 0.05 ETH when under $3,800')
    expect(sizeText(trigger())).toBe('50 % · ≈ $194')
    expect(sizeText(fixed)).toBe('0.05 ETH · ≈ $194')
    expect(sizeText(BUY)).toBe('$50')
    expect(sizeText(ALERT)).toBe('—')
    expect(planText(trigger())).toBe('sell 50 % ETH · under $3,800')
    expect(planText(ALERT)).toBe('ETH over $5,000')
    // A trail reads its own label.
    const trail = trigger({
      condition: {
        ...PAYLOAD.trigger.condition,
        direction: 'trail',
        priceUsd: null,
        trailPct: 10,
        label: '',
      },
    })
    expect(heroText(trail)).toBe('sell 50 % of ETH when 10 % below peak')
  })

  it('says each state in the contract’s words', () => {
    expect(triggerWord(trigger())).toBe('Armed · ETH $3,882 · −2.1 %')
    expect(triggerWord(near())).toBe('Armed · ETH $3,811 · −0.3 %')
    expect(triggerWord(trigger({ condition: { ...PAYLOAD.trigger.condition, hits: 1 } }))).toBe(
      'Armed · 1 of 2 checks',
    )
    expect(
      triggerWord(
        trigger({ market: { ...PAYLOAD.trigger.market, priceUsd: null, distancePct: null } }),
      ),
    ).toBe('Armed · waiting for a price')
    expect(triggerWord(trigger({ status: 'triggered' }))).toBe('Triggered · order open')
    expect(triggerWord(trigger({ status: 'paused' }))).toBe('Paused')
    expect(triggerWord(trigger({ status: 'done' }))).toBe('Done')
    expect(triggerWord(PENDING)).toBe('Awaiting approval')
  })

  it('says the live line under the sentence', () => {
    expect(nowText(trigger())).toBe('ETH $3,882 · 2.1 % above the line')
    expect(nowText(BUY)).toBe('ETH $3,882 · 2.1 % above the line')
    expect(nowText(ALERT)).toBe('ETH $3,882 · 28.8 % below the line')
    expect(nowText(trigger({ condition: { ...PAYLOAD.trigger.condition, hits: 1 } }))).toBe(
      'ETH $3,882 · fires after 1 more check',
    )
    expect(nowText(trigger({ market: { ...PAYLOAD.trigger.market, priceUsd: null } }))).toBe(
      'armed, waiting for a price',
    )
    expect(nowText(PENDING)).toBe('ETH $3,882 · awaiting approval')
  })

  it('says how a finished trigger ended, never a stale distance, and what its order moved', () => {
    // A ledger row from before the engine nulled it still carries a distance.
    const done = trigger({
      status: 'done',
      statusReason: 'sold 0.02 ETH for 42.4 USDC at $2,120',
      market: { ...PAYLOAD.trigger.market, priceUsd: 2120, distancePct: -2.1, balance: null },
      action: { ...PAYLOAD.trigger.action, estimatedUsd: 42.4 },
      result: {
        orderId: 'ord_000000000001',
        txHash: null,
        explorerUrl: null,
        amountIn: { raw: '20000000000000000', human: '0.02', usd: 42.4 },
        amountOut: { raw: '42400000', human: '42.4', usd: 42.4 },
        priceUsd: 2120,
        gasUsd: 0.01,
      },
    })
    expect(triggerWord(done)).toBe('Done · sold 0.02 ETH for 42.4 USDC at $2,120')
    expect(nowText(done)).toBe('done · sold 0.02 ETH for 42.4 USDC at $2,120')
    for (const text of [triggerWord(done), nowText(done)]) {
      expect(text).not.toMatch(/%|line/)
    }
    expect(sizeText(done)).toBe('0.02 ETH → 42.4 USDC · ≈ $42.40')
    // A buy moved the quote in and the token out.
    const bought = {
      ...done,
      kind: 'buy' as const,
      action: { ...BUY.action, estimatedUsd: 42.4 },
      result: {
        ...done.result!,
        amountIn: { raw: '42400000', human: '42.4', usd: 42.4 },
        amountOut: { raw: '20000000000000000', human: '0.02', usd: 42.4 },
      },
    }
    expect(sizeText(bought)).toBe('42.4 USDC → 0.02 ETH · ≈ $42.40')
    // Stopped by the user: the state alone, the reason stays on hover.
    const stopped = trigger({ status: 'stopped', statusReason: 'user', market: done.market })
    expect(triggerWord(stopped)).toBe('Stopped')
    expect(nowText(stopped)).toBe('stopped')
    // Without a result the size is still the plan.
    expect(sizeText(trigger({ status: 'done', result: null }))).toBe('50 % · ≈ $194')
  })

  it('is near within 1 % of the line, and only while armed', () => {
    expect(isNear(near())).toBe(true)
    expect(isNear(trigger())).toBe(false)
    expect(isNear(near({ status: 'paused' }))).toBe(false)
    expect(isNear(trigger({ market: { ...PAYLOAD.trigger.market, distancePct: 0 } }))).toBe(true)
  })

  it('lists every live trigger, and this chat’s finished ones for an hour only', () => {
    const list = [
      trigger(),
      trigger({ id: 'other', sessionKey: 'agent:trading:webchat:other' }),
      trigger({ id: 'unfiled', sessionKey: null }),
      trigger({ id: 'old', status: 'done', updatedAt: '2026-10-04T04:30:00Z' }),
      trigger({ id: 'fresh', status: 'stopped', updatedAt: '2026-10-04T05:40:00Z' }),
    ]
    expect(deskTriggers(list, SESSION, NOW).map((tr) => tr.id)).toEqual([
      'trg_1a2b3c4d',
      'other',
      'unfiled',
      'fresh',
    ])
    expect(
      deskTriggers(
        [trigger({ id: 'od', status: 'stopped', sessionKey: 'agent:trading:webchat:other' })],
        SESSION,
        NOW,
      ),
    ).toEqual([])
    const finished = ['a', 'b', 'c'].map((id) => trigger({ id, status: 'expired' }))
    const { rows, more } = triggerRows([trigger(), ...finished])
    expect(rows.map((tr) => tr.id)).toEqual(['trg_1a2b3c4d', 'a', 'b'])
    expect(more).toBe(1)
  })

  it('sums the live triggers in one chip: fired, near, awaiting, else armed', () => {
    const text = (list: Trigger[]) => {
      const chip = triggerChip(list)
      return chip ? triggerChipText(chip) : null
    }
    expect(text([trigger()])).toBe('Trigger · armed')
    expect(text([trigger(), BUY])).toBe('Triggers ×2')
    expect(text([near()])).toBe('Trigger · near')
    expect(text([trigger({ status: 'triggered' })])).toBe('Trigger · fired')
    expect(text([trigger(), PENDING])).toBe('Triggers ×2 · awaiting')
    expect(text([trigger({ status: 'paused' })])).toBe('Trigger · paused')
    expect(text([trigger({ status: 'done' })])).toBeNull()
    expect(triggerChip([near(), trigger({ status: 'triggered' })])?.word).toBe('fired')
  })
})

describe('TriggerCard', () => {
  afterEach(() => useTouchIdPrompt.setState({ key: null }))

  it('says the whole trigger before the one button that arms it', () => {
    const onApprove = vi.fn()
    const onReject = vi.fn()
    renderDesk(
      <TriggerCard
        trigger={PENDING}
        wallets={[WALLET]}
        deciding={false}
        onApprove={onApprove}
        onReject={onReject}
      />,
    )
    const card = screen.getByTestId('trigger-card')
    expect(card).toHaveAttribute('data-status', 'awaiting_approval')
    expect(card).toHaveAttribute('data-action', 'sell')
    expect(card).toHaveTextContent('Stop-loss ETH')
    expect(screen.getByTestId('trigger-hero')).toHaveTextContent(
      'ETHsell 50 % of ETH when under $3,800',
    )
    expect(screen.getByTestId('trigger-now')).toHaveTextContent('ETH $3,882 · awaiting approval')
    const fact = (key: string) => card.querySelector(`[data-fact='${key}'] dd`)?.textContent
    expect(fact('what')).toBe('sell 50 % of ETH')
    expect(fact('when')).toBe('under $3,800')
    expect(fact('size')).toBe('50 % · ≈ $194')
    expect(fact('now')).toBe('$3,882')
    expect(fact('balance')).toBe('0.1 ETH')
    expect(fact('wallet')).toBe('Main · 0x1111…1111')
    expect(fact('chain')).toBe('Base')
    expect(fact('validUntil')).toBe('until stopped')
    expect(fact('approval')).toBe('waits for you · over $100')
    expect(fact('expires')).toMatch(/^Oct (4|5) \d{2}:\d{2}$/)
    // Beside "Valid until · until stopped", the proposal's deadline is named as
    // one: an "Expires" there read as a contradiction.
    const label = (key: string) => card.querySelector(`[data-fact='${key}'] dt`)?.textContent
    expect(label('validUntil')).toBe('Valid until')
    expect(label('expires')).toBe('Decide by')
    expect(within(card).queryByText('Expires')).toBeNull()
    expect(screen.getByTestId('trigger-needs-approval')).toBeInTheDocument()
    expect(screen.getByTestId('trigger-warnings')).toHaveTextContent(
      'a sell of ≈$194 is above the $100 approval threshold and will wait for you when it fires',
    )
    expect(screen.getByTestId('trigger-approve')).toHaveTextContent('Approve & arm')
    fireEvent.click(screen.getByTestId('trigger-approve'))
    expect(onApprove).toHaveBeenCalledWith(PENDING)
    fireEvent.click(screen.getByTestId('trigger-reject'))
    expect(onReject).toHaveBeenCalledWith(PENDING)
  })

  it('warns about a line already crossed, nothing to sell, and no price', () => {
    const { unmount } = renderDesk(
      <TriggerCard
        trigger={trigger({
          status: 'awaiting_approval',
          market: {
            ...PAYLOAD.trigger.market,
            priceUsd: 3700,
            distancePct: 0,
            balance: { raw: '0', human: '0', usd: 0 },
          },
        })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    )
    const warnings = screen.getByTestId('trigger-warnings')
    expect(warnings).toHaveTextContent(
      'ETH is already at $3,700, under $3,800: this fires after the next two checks',
    )
    expect(warnings).toHaveTextContent('Main holds no ETH')
    unmount()
    renderDesk(
      <TriggerCard
        trigger={{
          ...ALERT,
          status: 'awaiting_approval',
          market: { ...ALERT.market, priceUsd: null, distancePct: null },
        }}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    )
    expect(screen.getByTestId('trigger-warnings')).toHaveTextContent(
      'price unknown: the trigger waits until ETH has a price',
    )
    // An alert trades nothing: no size, no balance, no approval line.
    const card = screen.getByTestId('trigger-card')
    expect(card.querySelector(`[data-fact='what'] dd`)).toHaveTextContent('notify')
    expect(card.querySelector(`[data-fact='size']`)).toBeNull()
    expect(card.querySelector(`[data-fact='approval']`)).toBeNull()
    expect(card.querySelector(`[data-fact='balance']`)).toBeNull()
    expect(screen.queryByTestId('trigger-needs-approval')).toBeNull()
  })

  it('says an alert only notifies: no order, never "an ordinary order through your limits"', () => {
    const { unmount } = renderDesk(
      <TriggerCard
        trigger={{ ...ALERT, status: 'awaiting_approval' }}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    )
    const enforced = screen.getByTestId('trigger-enforced')
    expect(enforced).toHaveTextContent('notifies you once')
    expect(enforced).toHaveTextContent('An alert places no order')
    expect(enforced).not.toHaveTextContent('ordinary order')
    unmount()
    // A trigger that trades still says every fire goes through the limits.
    renderDesk(
      <TriggerCard
        trigger={PENDING}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    )
    expect(screen.getByTestId('trigger-enforced')).toHaveTextContent(
      'Every fire is an ordinary order through your limits.',
    )
  })

  it('locks both buttons while the decision is in flight, and says Touch ID while it asks', () => {
    const { unmount } = renderDesk(
      <TriggerCard
        trigger={PENDING}
        wallets={[WALLET]}
        deciding
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    )
    expect(screen.getByTestId('trigger-approve')).toBeDisabled()
    expect(screen.getByTestId('trigger-reject')).toBeDisabled()
    unmount()
    useTouchIdPrompt.setState({ key: `trigger:${PENDING.id}` })
    renderDesk(
      <TriggerCard
        trigger={PENDING}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    )
    expect(screen.getByTestId('trigger-approve')).toHaveTextContent('Touch ID…')
    expect(screen.getByTestId('trigger-approve')).toBeDisabled()
    expect(screen.getByTestId('trigger-reject')).not.toBeDisabled()
  })

  it('docks above the pending mandates and orders, and takes focus on Reject once', () => {
    const onApproveTrigger = vi.fn()
    const onRejectTrigger = vi.fn()
    renderDesk(
      <ApprovalsRegion
        pending={[]}
        settled={[]}
        wallets={[WALLET]}
        deciding={null}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOrderId={null}
        mandates={[{ ...MANDATE, id: 'dca_99', status: 'awaiting_approval' }]}
        triggers={[PENDING]}
        onApproveTrigger={onApproveTrigger}
        onRejectTrigger={onRejectTrigger}
      />,
    )
    const region = screen.getByTestId('approvals-region')
    const cards = [...region.querySelectorAll('article')]
    expect(cards[0]).toHaveAttribute('data-testid', 'trigger-card')
    expect(cards[1]).toHaveAttribute('data-testid', 'mandate-card')
    expect(document.activeElement).toBe(
      within(cards[0] as HTMLElement).getByTestId('trigger-reject'),
    )
    fireEvent.click(within(cards[0] as HTMLElement).getByTestId('trigger-approve'))
    expect(onApproveTrigger).toHaveBeenCalledWith(PENDING)
    fireEvent.click(within(cards[0] as HTMLElement).getByTestId('trigger-reject'))
    expect(onRejectTrigger).toHaveBeenCalledWith(PENDING)
  })

  it('leaves the focus to a pending order, and renders for a pending trigger alone', () => {
    const { unmount } = renderDesk(
      <ApprovalsRegion
        pending={[order({ orderId: 'o1', status: 'awaiting_approval', initiator: 'agent' })]}
        settled={[]}
        wallets={[WALLET]}
        deciding={null}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOrderId={null}
        triggers={[PENDING]}
      />,
    )
    const cards = [...screen.getByTestId('approvals-region').querySelectorAll('article')]
    expect(cards.map((c) => c.getAttribute('data-testid'))).toEqual([
      'trigger-card',
      'approval-card',
    ])
    expect(document.activeElement).not.toBe(screen.getByTestId('trigger-reject'))
    unmount()
    renderDesk(
      <ApprovalsRegion
        pending={[]}
        settled={[]}
        wallets={[WALLET]}
        deciding={null}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOrderId={null}
        triggers={[PENDING]}
      />,
    )
    expect(screen.getByTestId('trigger-card')).toBeInTheDocument()
  })
})

describe('triggers among the missions', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
  })
  afterEach(() => vi.useRealTimers())

  function controls(triggers: Trigger[], extra: Record<string, unknown> = {}) {
    const handlers = {
      onTriggerPause: vi.fn(),
      onTriggerResume: vi.fn(),
      onTriggerFire: vi.fn(),
      onTriggerStop: vi.fn(),
    }
    const view = renderDesk(
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
        triggers={triggers}
        {...handlers}
        {...extra}
      />,
    )
    return { ...handlers, view }
  }

  it('shows an armed trigger with its price, distance and plan, and Pause · Sell now · Stop', () => {
    const h = controls([trigger()])
    const row = screen.getByTestId('trigger-row')
    expect(row).toHaveAttribute('data-state', 'armed')
    expect(row).toHaveAttribute('data-action', 'sell')
    expect(row).not.toHaveAttribute('data-near')
    expect(row).toHaveTextContent('Stop-loss ETH')
    expect(screen.getByTestId('trigger-word')).toHaveTextContent('Armed · ETH $3,882 · −2.1 %')
    expect(screen.getByTestId('trigger-plan')).toHaveTextContent('sell 50 % ETH · under $3,800')
    expect(screen.queryByTestId('trigger-resume')).toBeNull()
    expect(screen.getByTestId('trigger-fire')).toHaveAttribute('title', 'Sell now')
    fireEvent.click(screen.getByTestId('trigger-pause'))
    expect(h.onTriggerPause).toHaveBeenCalledTimes(1)
  })

  it('asks for a second click before Fire now and before Stop, inline', () => {
    const confirm = vi.spyOn(window, 'confirm')
    const h = controls([trigger()])
    fireEvent.click(screen.getByTestId('trigger-fire'))
    expect(h.onTriggerFire).not.toHaveBeenCalled()
    expect(screen.getByTestId('trigger-fire')).toHaveTextContent('Sell now — click again')
    fireEvent.click(screen.getByTestId('trigger-fire'))
    expect(h.onTriggerFire).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('trigger-stop'))
    expect(h.onTriggerStop).not.toHaveBeenCalled()
    expect(screen.getByTestId('trigger-stop')).toHaveTextContent('Stop — click again')
    fireEvent.click(screen.getByTestId('trigger-stop'))
    expect(h.onTriggerStop).toHaveBeenCalledTimes(1)
    expect(confirm).not.toHaveBeenCalled()
  })

  it('arms one two-click control at a time', () => {
    controls([trigger()])
    fireEvent.click(screen.getByTestId('trigger-fire'))
    fireEvent.click(screen.getByTestId('trigger-stop'))
    expect(screen.getByTestId('trigger-stop')).toHaveTextContent('Stop — click again')
    expect(screen.getByTestId('trigger-fire')).not.toHaveTextContent('click again')
  })

  it('offers Resume on a paused one, Stop only on a triggered one, a pointer on a pending one', () => {
    controls([
      trigger({ id: 'p', status: 'paused', statusReason: 'paused: nothing to sell' }),
      trigger({ id: 't', status: 'triggered' }),
      PENDING,
      trigger({
        id: 'd',
        status: 'done',
        statusReason: 'sold 0.05 ETH for 189.4 USDC at $3,788',
        updatedAt: '2026-10-04T05:50:00Z',
      }),
      { ...ALERT, id: 'a', status: 'done', statusReason: 'alerted at $5,012' },
    ])
    const rows = screen.getAllByTestId('trigger-row') as HTMLElement[]
    const byId = (id: string) => rows.find((r) => r.getAttribute('data-trigger') === id)!
    const paused = byId('p')
    expect(within(paused).getByTestId('trigger-word')).toHaveTextContent('Paused')
    expect(within(paused).getByTestId('trigger-resume')).toBeInTheDocument()
    expect(within(paused).getByTestId('trigger-fire')).toBeInTheDocument()
    expect(paused).toHaveAttribute('title', 'paused: nothing to sell')
    const fired = byId('t')
    expect(within(fired).getByTestId('trigger-word')).toHaveTextContent('Triggered · order open')
    expect(within(fired).queryByTestId('trigger-pause')).toBeNull()
    expect(within(fired).queryByTestId('trigger-fire')).toBeNull()
    expect(within(fired).getByTestId('trigger-stop')).toBeInTheDocument()
    const pending = byId('trg_99')
    expect(within(pending).getByTestId('trigger-word')).toHaveTextContent('Awaiting approval')
    expect(pending).toHaveTextContent('Review it above the composer')
    expect(within(pending).queryByRole('button')).toBeNull()
    const done = byId('d')
    expect(within(done).getByTestId('trigger-word')).toHaveTextContent('Done')
    expect(done).toHaveAttribute('title', 'sold 0.05 ETH for 189.4 USDC at $3,788')
    expect(within(done).queryByRole('button')).toBeNull()
    // An alert's Fire now keeps its name.
    expect(byId('a')).toHaveAttribute('data-action', 'alert')
  })

  it('says a bare user pause as “Paused by you”, and marks a trigger near its line', () => {
    controls([trigger({ id: 'p', status: 'paused', statusReason: 'user' }), near({ id: 'n' })])
    const rows = screen.getAllByTestId('trigger-row') as HTMLElement[]
    expect(rows[0]).toHaveAttribute('title', 'Paused by you')
    expect(rows[1]).toHaveAttribute('data-near', 'true')
  })

  it('locks the row of a trigger with a write in flight', () => {
    controls([trigger()], { triggerBusy: 'trg_1a2b3c4d' })
    expect(screen.getByTestId('trigger-pause')).toBeDisabled()
    expect(screen.getByTestId('trigger-fire')).toBeDisabled()
    expect(screen.getByTestId('trigger-stop')).toBeDisabled()
  })

  it('leaves out a pending trigger whose card is already in the approvals region', () => {
    controls([PENDING, trigger()], { askedTriggers: new Set([PENDING.id]) })
    const rows = screen.getAllByTestId('trigger-row')
    expect(rows.map((r) => r.getAttribute('data-trigger'))).toEqual(['trg_1a2b3c4d'])
    expect(screen.queryByText('Review it above the composer')).toBeNull()
  })

  it('obeys the mandates’ caps: live rows, then two finished newest first, then "+N more"', () => {
    const all = [
      trigger(),
      trigger({ id: 'c', status: 'expired', updatedAt: '2026-10-04T05:30:00Z' }),
      trigger({ id: 'b', status: 'stopped', updatedAt: '2026-10-04T05:45:00Z' }),
      trigger({ id: 'a', status: 'done', updatedAt: '2026-10-04T05:40:00Z' }),
      // Finished over an hour ago: gone from the desk.
      trigger({ id: 'old', status: 'done', updatedAt: '2026-10-04T04:30:00Z' }),
    ]
    controls(deskTriggers(all, SESSION, NOW))
    const ids = () =>
      screen.getAllByTestId('trigger-row').map((r) => r.getAttribute('data-trigger'))
    expect(ids()).toEqual(['trg_1a2b3c4d', 'b', 'a'])
    expect(screen.getByTestId('trigger-more')).toHaveTextContent('+1 more')
    fireEvent.click(screen.getByTestId('trigger-more'))
    expect(ids()).toEqual(['trg_1a2b3c4d', 'b', 'a', 'c'])
    expect(screen.getByTestId('trigger-more')).toHaveTextContent('show fewer')
  })

  it('keeps every control of a row in one group, so the group wraps whole and Stop stays reachable', () => {
    controls([trigger(), trigger({ id: 't', status: 'triggered' })])
    const [armed, fired] = screen.getAllByTestId('trigger-row') as HTMLElement[]
    const group = armed!.querySelector('.trd-mctl__ctl')
    expect(group).not.toBeNull()
    for (const id of ['trigger-pause', 'trigger-fire', 'trigger-stop']) {
      expect(group).toContainElement(within(armed!).getByTestId(id))
    }
    // The armed second click is spelled out inside the same group.
    fireEvent.click(within(armed!).getByTestId('trigger-stop'))
    expect(group).toContainElement(within(armed!).getByTestId('trigger-stop'))
    expect(fired!.querySelector('.trd-mctl__ctl')).toContainElement(
      within(fired!).getByTestId('trigger-stop'),
    )
  })

  it('shows two finished rows at most, then "+N more"', () => {
    controls([
      trigger(),
      trigger({ id: 'b', status: 'stopped' }),
      trigger({ id: 'a', status: 'done' }),
      trigger({ id: 'c', status: 'expired' }),
    ])
    expect(screen.getAllByTestId('trigger-row')).toHaveLength(3)
    fireEvent.click(screen.getByTestId('trigger-more'))
    expect(screen.getAllByTestId('trigger-row')).toHaveLength(4)
  })

  it('names a trigger once, in its row; the strip above the composer leaves it out', () => {
    // The dot strip used to repeat every trigger row word for word, a line
    // per trigger taken from the transcript.
    renderDesk(
      <>
        <MissionStrip missions={[]} running={new Set()} pendingApprovals={0} />
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
          triggers={[near()]}
        />
        <StatusStrip mode="trading" onSwitchMode={vi.fn()} triggers={[near(), BUY]} />
      </>,
    )
    expect(screen.queryByTestId('mission-strip')).toBeNull()
    expect(screen.queryByTestId('mission-strip-trigger')).toBeNull()
    expect(screen.getAllByTestId('trigger-row')).toHaveLength(1)
    expect(screen.getByTestId('trigger-word')).toHaveTextContent('Armed · ETH $3,811 · −0.3 %')
    expect(screen.getByTestId('trigger-row')).toHaveAttribute('data-near', 'true')
    const chip = screen.getByTestId('strip-triggers')
    expect(chip).toHaveTextContent(/^Triggers ×2 · near$/)
    expect(chip).toHaveAttribute('data-state', 'near')
  })

  it('gives each chip one of the two mission slots', () => {
    const jobs = [
      { id: 'j1', name: 'Watch ETH', enabled: true },
      { id: 'j2', name: 'Watch BTC', enabled: true },
    ]
    renderDesk(
      <StatusStrip
        mode="trading"
        onSwitchMode={vi.fn()}
        missions={jobs}
        mandates={[MANDATE]}
        triggers={[trigger({ status: 'triggered' })]}
      />,
    )
    expect(screen.getByTestId('strip-triggers')).toHaveTextContent(/^Trigger · fired$/)
    expect(screen.getByTestId('strip-mandates')).toBeInTheDocument()
    const strip = screen.getByTestId('status-strip')
    expect(strip).not.toHaveTextContent('Watch ETH')
    expect(strip).toHaveTextContent('+2')
  })
})

describe('useMissions · triggers', () => {
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

  const other = trigger({ id: 'trg_o', sessionKey: 'agent:trading:webchat:other' })
  const unfiled = trigger({ id: 'trg_u', sessionKey: null })
  const pendingElsewhere = { ...PENDING, sessionKey: 'agent:main:webchat:x' }

  function answer(method: string, params: Record<string, unknown>) {
    if (method === 'cron.list') return []
    if (method === 'trading.dca.list') return { kind: 'mandates', mandates: [] }
    if (method === 'trading.trigger.list') {
      return {
        version: 1,
        kind: 'triggers',
        fetchedAt: PAYLOAD.fetchedAt,
        warnings: [],
        triggers: params.all ? [trigger(), other, unfiled, pendingElsewhere] : [],
        totals: { count: 4, armed: 3, awaiting: 1, triggered: 0 },
      }
    }
    if (method.startsWith('trading.trigger.')) return PAYLOAD
    return {}
  }

  it('lists every live trigger and every pending one, whichever chat filed them', async () => {
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) =>
      answer(method, params),
    )
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.triggers.length).toBeGreaterThan(0))
    expect(rpcCall).toHaveBeenCalledWith('trading.trigger.list', { all: true })
    expect(result.current.triggers.map((tr) => tr.id)).toEqual(['trg_1a2b3c4d', 'trg_o', 'trg_u'])
    expect(result.current.awaitingTriggers.map((tr) => tr.id)).toEqual(['trg_99'])
    expect(rpcCall).not.toHaveBeenCalledWith('trading.trigger.pause', expect.anything())
  })

  it('shows a write’s answer on the row at once, without waiting for the list', async () => {
    let held = false
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'trading.trigger.pause') {
        held = true
        return {
          ...PAYLOAD,
          trigger: { ...PAYLOAD.trigger, status: 'paused', statusReason: 'user' },
        }
      }
      if (held && method === 'trading.trigger.list') return new Promise(() => {})
      return answer(method, params)
    })
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.triggers.length).toBeGreaterThan(0))
    const tr = result.current.triggers[0]!
    expect(tr.status).toBe('armed')
    await act(async () => {
      await result.current.trigger.pause(tr)
    })
    await waitFor(() =>
      expect(result.current.triggers.find((x) => x.id === tr.id)?.status).toBe('paused'),
    )
  })

  it('steers and toasts a trigger through trading.trigger.*', async () => {
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) =>
      answer(method, params),
    )
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.triggers.length).toBeGreaterThan(0))
    const tr = result.current.triggers[0]!
    const id = { id: 'trigger-trg_1a2b3c4d' }
    for (const [action, method, said] of [
      ['approve', 'trading.trigger.approve', 'Trigger approved and armed · Stop-loss ETH'],
      ['reject', 'trading.trigger.reject', 'Trigger rejected · Stop-loss ETH'],
      ['pause', 'trading.trigger.pause', 'Trigger paused · Stop-loss ETH'],
      ['resume', 'trading.trigger.resume', 'Trigger armed again · Stop-loss ETH'],
      ['stop', 'trading.trigger.stop', 'Trigger stopped · Stop-loss ETH'],
    ] as const) {
      await act(async () => {
        await result.current.trigger[action](tr)
      })
      expect(rpcCall).toHaveBeenCalledWith(method, { triggerId: 'trg_1a2b3c4d' })
      expect(toasts.success).toHaveBeenLastCalledWith(said, id)
    }
    await act(async () => {
      await result.current.trigger.stop(tr, 'changed my mind')
    })
    expect(rpcCall).toHaveBeenCalledWith('trading.trigger.stop', {
      triggerId: 'trg_1a2b3c4d',
      reason: 'changed my mind',
    })
  })

  it('says why when the engine refuses, and resolves null', async () => {
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'trading.trigger.pause')
        throw new Error('trading.trigger.bad_state: already stopped')
      return answer(method, params)
    })
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.triggers.length).toBeGreaterThan(0))
    let res: unknown = 'unset'
    await act(async () => {
      res = await result.current.trigger.pause(result.current.triggers[0]!)
    })
    expect(res).toBeNull()
    expect(toasts.error).toHaveBeenCalledWith(
      'Could not update the trigger: trading.trigger.bad_state: already stopped',
      { id: 'trigger-trg_1a2b3c4d' },
    )
  })

  it('says what a Fire now did: alerted, placed, filled, parked, skipped and why, failed and why', async () => {
    const fire = (extra: Partial<TriggerFire>): TriggerFire => ({
      n: 2,
      at: '2026-10-04T06:01:00Z',
      manual: true,
      status: 'pending',
      reasonCode: null,
      reason: null,
      priceUsd: 3790,
      orderId: 'ord_9',
      txHash: null,
      explorerUrl: null,
      ...extra,
    })
    let answerFire: TriggerFire | null = null
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) =>
      method === 'trading.trigger.fire'
        ? { ...PAYLOAD, ...(answerFire ? { fire: answerFire } : {}) }
        : answer(method, params),
    )
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.triggers.length).toBeGreaterThan(0))
    const tr = result.current.triggers[0]!
    const id = { id: 'trigger-trg_1a2b3c4d' }
    const fireNow = async (f: TriggerFire | null) => {
      answerFire = f
      await act(async () => {
        await result.current.trigger.fire(tr)
      })
    }
    await fireNow(fire({ status: 'skipped', reason: 'paused: nothing to sell' }))
    expect(toasts.warning).toHaveBeenLastCalledWith(
      'Fire skipped: paused: nothing to sell · Stop-loss ETH',
      id,
    )
    expect(toasts.success).not.toHaveBeenCalled()
    await fireNow(fire({ status: 'failed', reason: 'no route' }))
    expect(toasts.error).toHaveBeenLastCalledWith('Fire failed: no route · Stop-loss ETH', id)
    await fireNow(fire({ status: 'pending' }))
    expect(toasts.success).toHaveBeenLastCalledWith('Fired; order placed · Stop-loss ETH', id)
    await fireNow(fire({ status: 'filled' }))
    expect(toasts.success).toHaveBeenLastCalledWith('Fired and filled · Stop-loss ETH', id)
    await fireNow(fire({ status: 'alerted', orderId: null }))
    expect(toasts.success).toHaveBeenLastCalledWith('Alert sent · Stop-loss ETH', id)
    await fireNow(fire({ status: 'parked' }))
    expect(toasts.info).toHaveBeenLastCalledWith(
      'Fired; the order waits for your approval · Stop-loss ETH',
      id,
    )
    expect(rpcCall).toHaveBeenCalledWith('trading.trigger.fire', { triggerId: 'trg_1a2b3c4d' })
    // No `fire` in the answer and no manual fire in the history: said, not silent.
    await fireNow(null)
    expect(toasts.info).toHaveBeenLastCalledWith('Nothing fired · Stop-loss ETH', id)
  })
})

// jsdom has no layout, so the geometry the live desk got wrong is pinned in
// desk.css itself. A trigger row once ran 600px wide in a 544px column, its
// Stop under the BOOK where no pointer could reach it; and with a proposal
// docked the transcript shrank to 28px.
describe('missions rows and the transcript floor · CSS contract', () => {
  const deskCss = readFileSync('src/renderer/src/views/trading/desk/desk.css', 'utf8')
  const rules = [...deskCss.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(
    ([, selector = '', body = '']) => ({ selector: selector.trim(), body }),
  )
  const rule = (selector: string) => rules.find((r) => r.selector === selector)?.body ?? ''

  it('never lets a row outgrow its column: it shrinks, wraps, and its texts ellipsize', () => {
    const row = rule('.trd-mctl__row')
    expect(row).toMatch(/min-width: 0;/)
    expect(row).toMatch(/max-width: 100%;/)
    expect(row).toMatch(/flex-wrap: wrap;/)
    // A fixed height cannot hold a second line.
    expect(row).not.toMatch(/(^|[^-])height: \d+px;/m)
    expect(row).toMatch(/min-height: \d+px;/)
    for (const part of ['.trd-mctl__name', '.trd-mctl__word', '.trd-mctl__runs']) {
      expect(rule(part), part).toMatch(/overflow: hidden;/)
      expect(rule(part), part).toMatch(/text-overflow: ellipsis;/)
    }
    // The controls are one group that never shrinks: it drops to the next line whole.
    const ctl = rule('.trd-mctl__ctl')
    expect(ctl).toMatch(/flex-shrink: 0;/)
    expect(ctl).toMatch(/margin-left: auto;/)
    expect(rule('.trd-mctl')).toMatch(/min-width: 0;/)
  })

  it('caps the rows band and lets it scroll, so it cannot eat the transcript', () => {
    const band = rule('.trd-mctl')
    expect(band).toMatch(/max-height: min\(var\(--mctl-cap, [\d.]+rem\), \d+vh\);/)
    expect(band).toMatch(/overflow-y: auto;/)
  })

  // Four DCAs took four lines above the composer, and a tall window would
  // have shown a fifth. Three rows, then the band scrolls.
  it('shows three rows at most, each on a line of its own, then scrolls', () => {
    const band = rule('.trd-mctl')
    const row = Number(/min-height: (\d+)px;/.exec(rule('.trd-mctl__row'))![1])
    const gap = Number(/gap: (\d+)px;/.exec(band)![1])
    const padY = Number(/padding: (\d+)px \d+px;/.exec(band)![1])
    // The fallback before watchRowsBand has measured: three one-line rows.
    const capPx = Number(/--mctl-cap, ([\d.]+)rem/.exec(band)![1]) * 16
    expect(capPx).toBe(3 * row + 2 * gap + 2 * padY)
    // Rows wrapped into a shared line would put more than three in that cap.
    expect(band).toMatch(/flex-direction: column;/)
    expect(band).not.toMatch(/flex-wrap: wrap;/)
    // A row that cannot shrink keeps its second line when the band is full.
    expect(rule('.trd-mctl > *')).toMatch(/flex-shrink: 0;/)
  })

  it('gives the desk transcript a floor and lets the approvals region yield instead', () => {
    // The floor is on the transcript's box (thread + what is laid over it).
    const thread = rule('.chat-desktop[data-desk] .chat-transcript')
    expect(thread).toMatch(/flex: 1 1 0;/)
    const floor = /min-height: clamp\(([\d.]+)rem, (\d+)vh, ([\d.]+)rem\);/.exec(thread)
    expect(floor).toBeTruthy()
    // At a 1200×800 window the floor is well over the 28px the tester saw.
    const px = Math.min(
      Math.max(Number(floor![1]) * 16, (Number(floor![2]) / 100) * 800),
      Number(floor![3]) * 16,
    )
    expect(px).toBeGreaterThanOrEqual(120)
    const asks = rule('.trd-asks')
    expect(asks).toMatch(/flex: 0 1 auto;/)
    expect(asks).not.toMatch(/flex-shrink: 0;/)
    // …and scrolls its cards inside its own cap.
    expect(asks).toMatch(/max-height: min\(\d+vh, [\d.]+rem\);/)
    expect(asks).toMatch(/overflow-y: auto;/)
  })

  // At 1200×800 the region got 238px for a 275px trigger card: its
  // explanation, and a second proposal entirely, sat behind an inner scroll.
  it('folds the rows band while a proposal is docked, and gives the room to the region', () => {
    const band = /max-height: min\((?:var\(--mctl-cap, )?([\d.]+)rem\)?, (\d+)vh\);/
    const capAt800 = (body: string) => {
      const m = band.exec(body)
      expect(m, body).toBeTruthy()
      return Math.min(Number(m![1]) * 16, (Number(m![2]) / 100) * 800)
    }
    const docked = rule('.chat-stage:has(> .trd-asks) .trd-mctl')
    const atRest = capAt800(rule('.trd-mctl'))
    // About two rows (26px each, 6px apart), and always less than at rest.
    expect(capAt800(docked)).toBeGreaterThanOrEqual(58)
    expect(capAt800(docked)).toBeLessThanOrEqual(64)
    expect(capAt800(docked)).toBeLessThan(atRest)
    // The region's own cap is never the limit at 800px: it may take 46vh.
    const asks = /max-height: min\((\d+)vh, ([\d.]+)rem\);/.exec(rule('.trd-asks'))!
    expect(Math.min((Number(asks[1]) / 100) * 800, Number(asks[2]) * 16)).toBeGreaterThanOrEqual(
      0.46 * 800,
    )
  })

  // Sticky to the band's foot, "+1 more" sat on top of the second bracket
  // row while a docked proposal folded the band to two rows. It is a line of
  // its own in the band's flow now, a row's height, laid over nothing.
  it('puts "+N more" on a line of its own after its rows, never over one', () => {
    for (const r of rules.filter((x) => x.selector.includes('.trd-mctl__more'))) {
      expect(r.body, r.selector).not.toMatch(/position: (sticky|absolute|fixed);/)
      expect(r.body, r.selector).not.toMatch(/margin(-top)?: -/)
    }
    const more = rule('.trd-mctl > .trd-mctl__more')
    expect(more).toMatch(/align-self: stretch;/)
    const rowMin = /min-height: (\d+)px;/.exec(rule('.trd-mctl__row'))![1]
    expect(more).toMatch(new RegExp(`min-height: ${rowMin}px;`))
  })
})

// The desk's empty hint once read through a docked proposal ("Tell the desk
// what to do" over "awaiting approval", "Does", "When"), and the renderer's
// own "No messages yet." sat above it.
describe('approvals cards and the empty transcript · CSS contract', () => {
  const deskCss = readFileSync('src/renderer/src/views/trading/desk/desk.css', 'utf8')
  const chatCss = readFileSync('src/renderer/src/views/chat/chat.css', 'utf8')
  const rulesOf = (css: string) =>
    [...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(
      ([, selector = '', body = '']) => ({ selector: selector.trim(), body }),
    )
  const desk = rulesOf(deskCss)
  const chat = rulesOf(chatCss)
  const rule = (rules: typeof desk, selector: string) =>
    rules.find((r) => r.selector === selector)?.body ?? ''

  it('paints every approvals card opaque — trigger, DCA and order alike', () => {
    const card = rule(desk, '.trd-card')
    expect(card).toMatch(/background: var\(--elevated\);/)
    expect(card).not.toMatch(/transparent/)
    // No variant brings the see-through plate back.
    for (const r of desk) {
      if (!/\.trd-card(\.trd-(trigger|mandate)|\[data-kind)/.test(r.selector)) continue
      expect(r.body, r.selector).not.toMatch(/background:[^;]*transparent/)
    }
  })

  it('lays the empty hint over the transcript box, which clips it', () => {
    const box = rule(chat, '.chat-transcript')
    expect(box).toMatch(/position: relative;/)
    expect(box).toMatch(/overflow: hidden;/)
    expect(box).toMatch(/min-height: 0;/)
    const hint = rule(desk, '.trd-chat__empty')
    expect(hint).toMatch(/position: absolute;/)
    expect(hint).toMatch(/inset: 0;/)
    expect(rule(chat, '.chat-history-loading')).toMatch(/inset: 0;/)
  })

  it('says the empty desk once: the renderer’s own empty line is hidden at the desk', () => {
    expect(rule(desk, '.chat-desktop[data-desk] .chat-empty')).toMatch(/display: none;/)
    // The old selector keyed on a wrapper the desk no longer renders.
    expect(desk.some((r) => r.selector === '.trd-chat .chat-empty')).toBe(false)
  })
})
