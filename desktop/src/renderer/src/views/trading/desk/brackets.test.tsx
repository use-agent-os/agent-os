import { readFileSync } from 'node:fs'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, renderHook, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { useTouchIdPrompt } from '~/lib/biometric-gate'
import { renderDesk, WALLET } from '../test-utils'
import type { Bracket, BracketPayload, TriggerFire, TriggerPayload } from '../types'
import { ApprovalsRegion } from './ApprovalsRegion'
import { BracketCard } from './BracketCard'
import {
  bracketHeroText,
  bracketNear,
  bracketNotes,
  bracketPlanText,
  bracketRows,
  bracketSizeText,
  bracketWord,
  deskBrackets,
  isSteerableBracket,
  legWord,
  movesText,
  rewardRiskText,
} from './bracket-logic'
import { MissionControls } from './MissionControls'
import { useMissions } from './missions'
import { StatusStrip } from './StatusStrip'
import { triggerChip, triggerChipText } from './trigger-logic'

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

const read = (name: string) =>
  JSON.parse(
    readFileSync(`src/renderer/src/views/trading/desk/__fixtures__/${name}`, 'utf8'),
  ) as unknown
const PAYLOAD = read('bracket/bracket.json') as BracketPayload
const AWAITING = (read('bracket/bracket-awaiting.json') as BracketPayload).bracket
const TRIGGER = (read('trigger/trigger.json') as TriggerPayload).trigger
const SESSION = 'agent:trading:webchat:desk-test'
const NOW = Date.parse('2026-10-04T06:00:00Z')

function bracket(extra: Partial<Bracket> = {}): Bracket {
  return { ...PAYLOAD.bracket, ...extra }
}

/** The fixture, 0.6 % over its stop. */
function near(extra: Partial<Bracket> = {}): Bracket {
  return bracket({
    market: { ...PAYLOAD.bracket.market, priceUsd: 3440, upsidePct: 32.6, downsidePct: -0.6 },
    ...extra,
  })
}

const ALERT = bracket({
  id: 'brk_al',
  name: 'Watch ETH',
  kind: 'alert',
  action: {
    ...PAYLOAD.bracket.action,
    kind: 'alert',
    amountPct: null,
    estimatedUsd: null,
    needsApproval: false,
    label: 'notify',
  },
  market: { ...PAYLOAD.bracket.market, balance: null },
})

describe('bracket logic', () => {
  it('says the sentence, the size, the plan and the moves off the engine figures', () => {
    expect(bracketHeroText(bracket())).toBe(
      'sell 100 % of ETH · take profit over $4,560 · stop under $3,420',
    )
    expect(bracketHeroText(ALERT)).toBe('notify when ETH is over $4,560 or under $3,420')
    expect(bracketSizeText(bracket())).toBe('100 % · ≈ $380')
    const partial = bracket({ action: { ...PAYLOAD.bracket.action, amountPct: 100, tpPct: 50 } })
    expect(bracketSizeText(partial)).toBe('50 % at take-profit, 100 % at stop')
    expect(bracketHeroText(partial)).toBe(
      'sell 50 % of ETH at take-profit, 100 % at stop · take profit over $4,560 · stop under $3,420',
    )
    expect(bracketSizeText(ALERT)).toBe('—')
    expect(bracketPlanText(bracket())).toBe('sell 100 % ETH · $3,420 – $4,560')
    expect(bracketPlanText(ALERT)).toBe('ETH $3,420 – $4,560')
    expect(movesText(bracket())).toBe('+20 % / −10 %')
    expect(movesText(near())).toBe('+32.6 % / −0.6 %')
    const trail = bracket({
      lines: { ...PAYLOAD.bracket.lines, trailPct: 10, stopLossLabel: '' },
    })
    expect(bracketHeroText(trail)).toBe(
      'sell 100 % of ETH · take profit over $4,560 · stop 10 % below peak',
    )
  })

  it('says each state in the contract’s words', () => {
    expect(bracketWord(bracket())).toBe('Armed · ETH $3,800 · +20 % / −10 %')
    expect(bracketWord(AWAITING)).toBe('Awaiting approval')
    const checking = bracket({
      takeProfit: {
        ...PAYLOAD.bracket.takeProfit,
        condition: { ...PAYLOAD.bracket.takeProfit.condition, hits: 1 },
      },
    })
    expect(bracketWord(checking)).toBe('Take-profit · 1 of 2 checks')
    const firing = bracket({
      status: 'triggered',
      stopLoss: { ...PAYLOAD.bracket.stopLoss, status: 'triggered' },
      takeProfit: {
        ...PAYLOAD.bracket.takeProfit,
        status: 'paused',
        statusReason: 'on hold: stop-loss fired',
      },
    })
    expect(bracketWord(firing)).toBe('Triggered · stop-loss order open')
    expect(bracketWord(bracket({ status: 'paused' }))).toBe('Paused')
    expect(bracketWord(bracket({ status: 'done', fired: 'tp' }))).toBe('Done · take-profit')
    expect(bracketWord(bracket({ status: 'stopped' }))).toBe('Stopped')
    expect(bracketWord(bracket({ market: { ...PAYLOAD.bracket.market, priceUsd: null } }))).toBe(
      'Armed · waiting for a price',
    )
  })

  it('is near within 1 % of either line, only while armed; steerable armed or paused', () => {
    expect(bracketNear(bracket())).toBe(false)
    expect(bracketNear(near())).toBe(true)
    expect(bracketNear(bracket({ market: { ...PAYLOAD.bracket.market, upsidePct: 0.8 } }))).toBe(
      true,
    )
    expect(bracketNear(near({ status: 'paused' }))).toBe(false)
    expect(isSteerableBracket(bracket())).toBe(true)
    expect(isSteerableBracket(bracket({ status: 'paused' }))).toBe(true)
    expect(isSteerableBracket(bracket({ status: 'triggered' }))).toBe(false)
    expect(isSteerableBracket(AWAITING)).toBe(false)
  })

  it('lists every live bracket, and this chat’s finished ones for an hour only', () => {
    const list = [
      bracket(),
      bracket({ id: 'other', sessionKey: 'agent:trading:webchat:other' }),
      bracket({ id: 'unfiled', sessionKey: null }),
      bracket({ id: 'old', status: 'done', updatedAt: '2026-10-04T04:30:00Z' }),
      bracket({ id: 'fresh', status: 'stopped', updatedAt: '2026-10-04T05:40:00Z' }),
    ]
    expect(deskBrackets(list, SESSION, NOW).map((b) => b.id)).toEqual([
      'brk_1a2b3c4d',
      'other',
      'unfiled',
      'fresh',
    ])
    expect(
      deskBrackets(
        [bracket({ id: 'od', status: 'stopped', sessionKey: 'agent:trading:webchat:other' })],
        SESSION,
        NOW,
      ),
    ).toEqual([])
    const finished = ['a', 'b', 'c'].map((id) => bracket({ id, status: 'expired' }))
    const { rows, more } = bracketRows([bracket(), ...finished])
    expect(rows.map((b) => b.id)).toEqual(['brk_1a2b3c4d', 'a', 'b'])
    expect(more).toBe(1)
  })

  it('says a warning both legs give once, a one-leg warning with its leg word', () => {
    const crossed = bracket({
      market: { ...PAYLOAD.bracket.market, priceUsd: 4700 },
      takeProfit: {
        ...PAYLOAD.bracket.takeProfit,
        market: { ...PAYLOAD.bracket.takeProfit.market, priceUsd: 4700, distancePct: 0 },
      },
      stopLoss: {
        ...PAYLOAD.bracket.stopLoss,
        market: { ...PAYLOAD.bracket.stopLoss.market, priceUsd: 4700, distancePct: -27.2 },
      },
    })
    expect(bracketNotes(crossed, 'Main')).toEqual([
      'take-profit: ETH is already at $4,700, over $4,560: this fires after the next two checks',
      'a sell of ≈$380 is above the $100 approval threshold and will wait for you when it fires',
    ])
  })

  it('counts a bracket once in the trigger chip: near either line, fired while a leg is open', () => {
    const text = (brackets: Bracket[], triggers = [TRIGGER]) => {
      const chip = triggerChip(triggers, brackets)
      return chip ? triggerChipText(chip) : null
    }
    expect(text([bracket()], [])).toBe('Trigger · armed')
    expect(text([bracket()])).toBe('Triggers ×2')
    expect(text([near()])).toBe('Triggers ×2 · near')
    expect(text([bracket({ status: 'triggered' })], [])).toBe('Trigger · fired')
    expect(text([AWAITING], [])).toBe('Trigger · awaiting')
    expect(text([bracket({ status: 'done' })], [])).toBeNull()
  })

  // The live desk said "Size 1 % · ≈ $0" where the chat card said "≈ $0.00275",
  // and "2 : 1" where it said "2.0 : 1".
  it('never says $0 for a dust-sized estimate, and gives reward : risk one decimal', () => {
    const dust = bracket({
      action: { ...PAYLOAD.bracket.action, amountPct: 1, estimatedUsd: 0.00275 },
    })
    expect(bracketSizeText(dust)).toBe('1 % · ≈ $0.0028')
    expect(
      bracketSizeText(bracket({ action: { ...PAYLOAD.bracket.action, estimatedUsd: 0 } })),
    ).toBe('100 % · ≈ $0')
    const rr = (rewardRisk: number) =>
      rewardRiskText(bracket({ market: { ...PAYLOAD.bracket.market, rewardRisk } }))
    expect(rr(2)).toBe('2.0 : 1')
    expect(rr(2.06)).toBe('2.1 : 1')
  })

  // A range alert sells nothing: the engine names its legs the ceiling and
  // the floor ("ceiling: alerted at $1", "range left over the top").
  it('names a range alert’s legs the ceiling and the floor, never take-profit or stop-loss', () => {
    expect(legWord('tp')).toBe('take-profit')
    expect(legWord('sl', 'sell')).toBe('stop-loss')
    expect(legWord('tp', 'alert')).toBe('ceiling')
    expect(legWord('sl', 'alert')).toBe('floor')
    const checking = {
      ...ALERT,
      takeProfit: {
        ...PAYLOAD.bracket.takeProfit,
        condition: { ...PAYLOAD.bracket.takeProfit.condition, hits: 1 },
      },
    }
    expect(bracketWord(checking)).toBe('Ceiling · 1 of 2 checks')
    expect(bracketWord({ ...ALERT, status: 'done', fired: 'tp' })).toBe('Done · ceiling')
    expect(
      bracketWord({
        ...ALERT,
        status: 'triggered',
        stopLoss: { ...PAYLOAD.bracket.stopLoss, status: 'triggered' },
      }),
    ).toBe('Triggered · floor order open')
    const crossed = {
      ...ALERT,
      takeProfit: {
        ...PAYLOAD.bracket.takeProfit,
        kind: 'alert' as const,
        market: { ...PAYLOAD.bracket.takeProfit.market, priceUsd: 4700, distancePct: 0 },
      },
      stopLoss: {
        ...PAYLOAD.bracket.stopLoss,
        kind: 'alert' as const,
        market: { ...PAYLOAD.bracket.stopLoss.market, priceUsd: 4700, distancePct: -27.2 },
      },
    }
    const notes = bracketNotes(crossed, 'Main')
    expect(notes.some((n) => n.startsWith('ceiling: '))).toBe(true)
    expect(notes.join(' ')).not.toMatch(/take-profit|stop-loss/)
  })

  // After a partial take-profit the engine sends upsidePct null: the dead
  // leg is "—", never a "0 %" or a "+0 %".
  it('says a dead take-profit leg as "—", nothing derived from it', () => {
    const rest = bracket({
      market: { ...PAYLOAD.bracket.market, upsidePct: null, downsidePct: -90, rewardRisk: null },
    })
    expect(movesText(rest)).toBe('— / −90 %')
    expect(bracketWord(rest)).toBe('Armed · ETH $3,800 · — / −90 %')
    expect(bracketWord(rest)).not.toMatch(/\b0 %/)
    expect(rewardRiskText(rest)).toBe('')
    expect(bracketNear(rest)).toBe(false)
  })
})

describe('BracketCard', () => {
  afterEach(() => useTouchIdPrompt.setState({ key: null }))

  it('says both lines, the size, the wallet and the deadline before Approve & arm', () => {
    const onApprove = vi.fn()
    const onReject = vi.fn()
    renderDesk(
      <BracketCard
        bracket={AWAITING}
        wallets={[WALLET]}
        deciding={false}
        onApprove={onApprove}
        onReject={onReject}
      />,
    )
    const card = screen.getByTestId('bracket-card')
    expect(card).toHaveAttribute('data-status', 'awaiting_approval')
    expect(card).toHaveAttribute('data-action', 'sell')
    expect(card).toHaveAttribute('data-kind', 'bracket')
    expect(card).toHaveTextContent('Protect ETH')
    expect(screen.getByTestId('bracket-hero')).toHaveTextContent(
      'ETHsell 100 % of ETH · take profit over $4,560 · stop under $3,420',
    )
    expect(screen.getByTestId('bracket-now')).toHaveTextContent(
      'ETH $3,800 · +20 % / −10 % · awaiting approval',
    )
    const fact = (key: string) => card.querySelector(`[data-fact='${key}'] dd`)?.textContent
    // The sentence says what it does and both lines, the live line the price:
    // the table never repeats them.
    for (const key of ['what', 'takeProfit', 'stopLoss', 'now']) expect(fact(key)).toBeUndefined()
    expect(fact('size')).toBe('100 % · ≈ $380')
    expect(fact('rewardRisk')).toBe('2.0 : 1')
    expect(fact('balance')).toBe('0.1 ETH')
    expect(fact('wallet')).toBe('Main · 0x1111…1111')
    expect(fact('validUntil')).toBe('until stopped')
    expect(fact('approval')).toBe('waits for you · over $100')
    expect(fact('expires')).toMatch(/^Oct (4|5) \d{2}:\d{2}$/)
    expect(screen.getByTestId('bracket-both-legs')).toHaveTextContent('One decision for both legs')
    // The engine's warning and the card's own say the same once.
    expect(screen.getByTestId('bracket-warnings').querySelectorAll('li')).toHaveLength(1)
    // One line on the card; the whole sentence is its tooltip.
    const enforced = screen.getByTestId('bracket-enforced')
    expect(enforced).toHaveTextContent('When one leg fills, it stops the other.')
    expect(enforced).toHaveAttribute('title', enforced.textContent)
    expect(screen.getByTestId('bracket-approve')).toHaveTextContent('Approve & arm')
    fireEvent.click(screen.getByTestId('bracket-approve'))
    expect(onApprove).toHaveBeenCalledWith(AWAITING)
    fireEvent.click(screen.getByTestId('bracket-reject'))
    expect(onReject).toHaveBeenCalledWith(AWAITING)
  })

  it('says a range alert moves nothing: no size, no approval line', () => {
    renderDesk(
      <BracketCard
        bracket={{ ...ALERT, status: 'awaiting_approval' }}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    )
    const card = screen.getByTestId('bracket-card')
    expect(card).toHaveAttribute('data-action', 'alert')
    expect(screen.getByTestId('bracket-hero')).toHaveTextContent(
      'notify when ETH is over $4,560 or under $3,420',
    )
    expect(card.querySelector(`[data-fact='size']`)).toBeNull()
    expect(card.querySelector(`[data-fact='approval']`)).toBeNull()
    expect(screen.getByTestId('bracket-enforced')).toHaveTextContent(
      'An alert places no order and moves no funds.',
    )
  })

  it('locks both buttons while deciding, and says Touch ID while it asks', () => {
    const { unmount } = renderDesk(
      <BracketCard
        bracket={AWAITING}
        wallets={[WALLET]}
        deciding
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    )
    expect(screen.getByTestId('bracket-approve')).toBeDisabled()
    expect(screen.getByTestId('bracket-reject')).toBeDisabled()
    unmount()
    useTouchIdPrompt.setState({ key: `bracket:${AWAITING.id}` })
    renderDesk(
      <BracketCard
        bracket={AWAITING}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    )
    expect(screen.getByTestId('bracket-approve')).toHaveTextContent('Touch ID…')
    expect(screen.getByTestId('bracket-approve')).toBeDisabled()
    expect(screen.getByTestId('bracket-reject')).not.toBeDisabled()
  })

  it('docks above the pending triggers, and takes focus on its Reject once', () => {
    const onApproveBracket = vi.fn()
    const onRejectBracket = vi.fn()
    renderDesk(
      <ApprovalsRegion
        pending={[]}
        settled={[]}
        wallets={[WALLET]}
        deciding={null}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOrderId={null}
        triggers={[{ ...TRIGGER, id: 'trg_99', status: 'awaiting_approval' }]}
        brackets={[AWAITING]}
        onApproveBracket={onApproveBracket}
        onRejectBracket={onRejectBracket}
      />,
    )
    const cards = [...screen.getByTestId('approvals-region').querySelectorAll('article')]
    expect(cards.map((c) => c.getAttribute('data-testid'))).toEqual([
      'bracket-card',
      'trigger-card',
    ])
    expect(document.activeElement).toBe(
      within(cards[0] as HTMLElement).getByTestId('bracket-reject'),
    )
    fireEvent.click(within(cards[0] as HTMLElement).getByTestId('bracket-approve'))
    expect(onApproveBracket).toHaveBeenCalledWith(AWAITING)
    fireEvent.click(within(cards[0] as HTMLElement).getByTestId('bracket-reject'))
    expect(onRejectBracket).toHaveBeenCalledWith(AWAITING)
  })
})

describe('brackets among the missions', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
  })
  afterEach(() => vi.useRealTimers())

  function controls(brackets: Bracket[], extra: Record<string, unknown> = {}) {
    const handlers = {
      onBracketPause: vi.fn(),
      onBracketResume: vi.fn(),
      onBracketFire: vi.fn(),
      onBracketStop: vi.fn(),
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
        brackets={brackets}
        {...handlers}
        {...extra}
      />,
    )
    return handlers
  }

  it('is one row — never a row per leg — with its state, range, Pause · Sell now · Stop', () => {
    const h = controls([bracket()])
    expect(screen.getAllByTestId('bracket-row')).toHaveLength(1)
    expect(screen.queryByTestId('trigger-row')).toBeNull()
    const row = screen.getByTestId('bracket-row')
    expect(row).toHaveAttribute('data-state', 'armed')
    expect(row).not.toHaveAttribute('data-near')
    expect(row).toHaveTextContent('Protect ETH')
    expect(screen.getByTestId('bracket-word')).toHaveTextContent(
      'Armed · ETH $3,800 · +20 % / −10 %',
    )
    expect(screen.getByTestId('bracket-plan')).toHaveTextContent('sell 100 % ETH · $3,420 – $4,560')
    expect(screen.getByTestId('bracket-fire')).toHaveAttribute('title', 'Sell now')
    expect(screen.queryByTestId('bracket-resume')).toBeNull()
    fireEvent.click(screen.getByTestId('bracket-pause'))
    expect(h.onBracketPause).toHaveBeenCalledTimes(1)
  })

  it('asks for a second click before Sell now and before Stop, one armed at a time', () => {
    const h = controls([bracket()])
    fireEvent.click(screen.getByTestId('bracket-fire'))
    expect(h.onBracketFire).not.toHaveBeenCalled()
    expect(screen.getByTestId('bracket-fire')).toHaveTextContent('Sell now — click again')
    fireEvent.click(screen.getByTestId('bracket-stop'))
    expect(screen.getByTestId('bracket-fire')).not.toHaveTextContent('click again')
    expect(screen.getByTestId('bracket-stop')).toHaveTextContent('Stop — click again')
    fireEvent.click(screen.getByTestId('bracket-stop'))
    expect(h.onBracketStop).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('bracket-fire'))
    fireEvent.click(screen.getByTestId('bracket-fire'))
    expect(h.onBracketFire).toHaveBeenCalledTimes(1)
  })

  it('offers Resume on a paused one, Stop only on a triggered one, nothing on a finished one', () => {
    const h = controls([
      bracket({ id: 'p', status: 'paused', statusReason: 'stop-loss paused: nothing to sell' }),
      bracket({ id: 't', status: 'triggered' }),
      bracket({ id: 'd', status: 'done', fired: 'tp', updatedAt: '2026-10-04T05:50:00Z' }),
      { ...ALERT, id: 'a' },
    ])
    const rows = screen.getAllByTestId('bracket-row') as HTMLElement[]
    const byId = (id: string) => rows.find((r) => r.getAttribute('data-bracket') === id)!
    const paused = byId('p')
    expect(paused).toHaveAttribute('title', 'stop-loss paused: nothing to sell')
    fireEvent.click(within(paused).getByTestId('bracket-resume'))
    expect(h.onBracketResume).toHaveBeenCalledTimes(1)
    const fired = byId('t')
    expect(within(fired).queryByTestId('bracket-pause')).toBeNull()
    expect(within(fired).queryByTestId('bracket-fire')).toBeNull()
    expect(within(fired).getByTestId('bracket-stop')).toBeInTheDocument()
    const done = byId('d')
    expect(within(done).getByTestId('bracket-word')).toHaveTextContent('Done · take-profit')
    expect(within(done).queryByRole('button')).toBeNull()
    expect(within(byId('a')).getByTestId('bracket-fire')).toHaveAttribute('title', 'Notify now')
  })

  it('marks a bracket near a line, locks a busy row, leaves out one asked above', () => {
    controls([near(), AWAITING], {
      bracketBusy: 'brk_1a2b3c4d',
      askedBrackets: new Set([AWAITING.id]),
    })
    const rows = screen.getAllByTestId('bracket-row')
    expect(rows.map((r) => r.getAttribute('data-bracket'))).toEqual(['brk_1a2b3c4d'])
    expect(rows[0]).toHaveAttribute('data-near', 'true')
    expect(screen.getByTestId('bracket-pause')).toBeDisabled()
    expect(screen.getByTestId('bracket-fire')).toBeDisabled()
    expect(screen.getByTestId('bracket-stop')).toBeDisabled()
  })

  // Live: "Stop — click again" came back on a row whose Stop had already
  // gone through, the row still reading "Armed" until the list caught up.
  it('drops an armed confirm when the row’s write lands or its state moves', () => {
    const props = (b: Bracket, busy: string | null = null) => (
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
        brackets={[b]}
        bracketBusy={busy}
        onBracketStop={vi.fn()}
        onBracketFire={vi.fn()}
      />
    )
    const { rerender } = renderDesk(props(bracket()))
    fireEvent.click(screen.getByTestId('bracket-stop'))
    expect(screen.getByTestId('bracket-stop')).toHaveTextContent('Stop — click again')
    rerender(props(bracket(), 'brk_1a2b3c4d'))
    expect(screen.getByTestId('bracket-stop')).not.toHaveTextContent('click again')
    rerender(props(bracket()))
    fireEvent.click(screen.getByTestId('bracket-fire'))
    expect(screen.getByTestId('bracket-fire')).toHaveTextContent('click again')
    rerender(props(bracket({ status: 'paused' })))
    expect(screen.getByTestId('bracket-fire')).not.toHaveTextContent('click again')
  })

  // The toggle is its own line after the rows it folds (desk.css pins the
  // geometry): never between two rows of the same kind.
  it('puts "+N more" after the bracket rows it folds', () => {
    const finished = ['a', 'b', 'c'].map((id) =>
      bracket({ id, status: 'stopped', updatedAt: '2026-10-04T05:50:00Z' }),
    )
    controls([bracket(), ...finished])
    const band = screen.getByTestId('mission-controls')
    const kids = [...band.children]
    const more = screen.getByTestId('bracket-more')
    expect(more).toHaveTextContent('+1 more')
    expect(more).toHaveClass('trd-mctl__more')
    const rows = screen.getAllByTestId('bracket-row')
    expect(rows).toHaveLength(3)
    expect(kids.indexOf(more)).toBe(kids.indexOf(rows[rows.length - 1]!) + 1)
  })

  it('counts a bracket into the status strip’s trigger chip', () => {
    renderDesk(
      <StatusStrip mode="trading" onSwitchMode={vi.fn()} triggers={[]} brackets={[near()]} />,
    )
    const chip = screen.getByTestId('strip-triggers')
    expect(chip).toHaveTextContent(/^Trigger · near$/)
    expect(chip).toHaveAttribute('data-state', 'near')
  })
})

describe('useMissions · brackets', () => {
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

  const unfiled = bracket({ id: 'brk_u', sessionKey: null })
  const other = bracket({ id: 'brk_o', sessionKey: 'agent:trading:webchat:other' })
  const pendingElsewhere = { ...AWAITING, sessionKey: 'agent:main:webchat:x' }

  function answer(method: string, params: Record<string, unknown>) {
    if (method === 'cron.list') return []
    if (method === 'trading.dca.list') return { kind: 'mandates', mandates: [] }
    if (method === 'trading.trigger.list') return { kind: 'triggers', triggers: [] }
    if (method === 'trading.bracket.list') {
      return {
        version: 1,
        kind: 'brackets',
        fetchedAt: PAYLOAD.fetchedAt,
        warnings: [],
        brackets: params.all ? [bracket(), other, unfiled, pendingElsewhere] : [],
        totals: { count: 4, armed: 3, awaiting: 1, triggered: 0 },
      }
    }
    if (method.startsWith('trading.bracket.')) return PAYLOAD
    return {}
  }

  it('lists every live bracket and every pending one, whichever chat filed them', async () => {
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) =>
      answer(method, params),
    )
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.brackets.length).toBeGreaterThan(0))
    expect(rpcCall).toHaveBeenCalledWith('trading.bracket.list', { all: true })
    expect(result.current.brackets.map((b) => b.id)).toEqual(['brk_1a2b3c4d', 'brk_o', 'brk_u'])
    expect(result.current.awaitingBrackets.map((b) => b.id)).toEqual(['brk_99'])
    expect(rpcCall).not.toHaveBeenCalledWith('trading.bracket.pause', expect.anything())
  })

  it('steers both legs at once through trading.bracket.*, and toasts each outcome', async () => {
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) =>
      answer(method, params),
    )
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.brackets.length).toBeGreaterThan(0))
    const b = result.current.brackets[0]!
    const id = { id: 'bracket-brk_1a2b3c4d' }
    for (const [action, method, said] of [
      ['approve', 'trading.bracket.approve', 'Bracket approved and armed · Protect ETH'],
      ['reject', 'trading.bracket.reject', 'Bracket rejected · Protect ETH'],
      ['pause', 'trading.bracket.pause', 'Bracket paused · Protect ETH'],
      ['resume', 'trading.bracket.resume', 'Bracket armed again · Protect ETH'],
      ['stop', 'trading.bracket.stop', 'Bracket stopped · Protect ETH'],
    ] as const) {
      await act(async () => {
        await result.current.bracket[action](b)
      })
      expect(rpcCall).toHaveBeenCalledWith(method, { bracketId: 'brk_1a2b3c4d' })
      expect(toasts.success).toHaveBeenLastCalledWith(said, id)
    }
  })

  // Live: a stopped bracket read "Armed" for 5–8 s, a resumed one "Paused"
  // for 3 s, while the list was read again. The write's answer is the
  // refreshed bracket, and the row shows it at once.
  it('shows a write’s answer on the row at once, without waiting for the list', async () => {
    let held = false
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'trading.bracket.pause') {
        held = true
        return {
          ...PAYLOAD,
          bracket: { ...PAYLOAD.bracket, status: 'paused', statusReason: 'user' },
        }
      }
      // Every list read after the write hangs: only the answer can move the row.
      if (held && method === 'trading.bracket.list') return new Promise(() => {})
      return answer(method, params)
    })
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.brackets.length).toBeGreaterThan(0))
    const b = result.current.brackets[0]!
    expect(b.status).toBe('armed')
    await act(async () => {
      await result.current.bracket.pause(b)
    })
    await waitFor(() =>
      expect(result.current.brackets.find((x) => x.id === b.id)?.status).toBe('paused'),
    )
    // …and the sweep still asked for the list again.
    expect(rpcCall.mock.calls.filter(([m]) => m === 'trading.bracket.list').length).toBeGreaterThan(
      1,
    )
  })

  it('fires the nearest leg or the one given, and says what the fire did', async () => {
    const fire: TriggerFire = {
      n: 1,
      at: '2026-10-04T06:01:00Z',
      manual: true,
      status: 'pending',
      reasonCode: null,
      reason: null,
      priceUsd: 3800,
      orderId: 'ord_9',
      txHash: null,
      explorerUrl: null,
    }
    let answerFire: TriggerFire | null = fire
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) =>
      method === 'trading.bracket.fire'
        ? { ...PAYLOAD, ...(answerFire ? { fire: answerFire } : {}) }
        : answer(method, params),
    )
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.brackets.length).toBeGreaterThan(0))
    const b = result.current.brackets[0]!
    const id = { id: 'bracket-brk_1a2b3c4d' }
    let res: BracketPayload | null = null
    await act(async () => {
      res = await result.current.bracket.fire(b)
    })
    expect(rpcCall).toHaveBeenCalledWith('trading.bracket.fire', { bracketId: 'brk_1a2b3c4d' })
    expect(res!.fire?.orderId).toBe('ord_9')
    expect(toasts.success).toHaveBeenLastCalledWith('Fired; order placed · Protect ETH', id)
    answerFire = { ...fire, status: 'skipped', orderId: null, reason: 'paused: nothing to sell' }
    await act(async () => {
      await result.current.bracket.fire(b, 'tp')
    })
    expect(rpcCall).toHaveBeenCalledWith('trading.bracket.fire', {
      bracketId: 'brk_1a2b3c4d',
      leg: 'tp',
    })
    expect(toasts.warning).toHaveBeenLastCalledWith(
      'Fire skipped: paused: nothing to sell · Protect ETH',
      id,
    )
    answerFire = null
    await act(async () => {
      await result.current.bracket.fire(b)
    })
    expect(toasts.info).toHaveBeenLastCalledWith('Nothing fired · Protect ETH', id)
  })

  it('says why when the engine refuses, and resolves null', async () => {
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'trading.bracket.pause')
        throw new Error('trading.bracket.bad_state: a leg is triggered')
      return answer(method, params)
    })
    const { result } = renderHook(() => useMissions(SESSION), { wrapper })
    await waitFor(() => expect(result.current.brackets.length).toBeGreaterThan(0))
    let res: unknown = 'unset'
    await act(async () => {
      res = await result.current.bracket.pause(result.current.brackets[0]!)
    })
    expect(res).toBeNull()
    expect(toasts.error).toHaveBeenCalledWith(
      'Could not update the bracket: trading.bracket.bad_state: a leg is triggered',
      { id: 'bracket-brk_1a2b3c4d' },
    )
  })
})
