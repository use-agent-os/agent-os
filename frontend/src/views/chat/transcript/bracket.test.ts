import { readFileSync } from 'node:fs'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'

import { artifactCategory } from './artifacts'
import {
  TRIGGER_ARTIFACT_MIME,
  TRIGGER_CONFIRM_MS,
  TRIGGER_FACT_WIDE_CHARS,
  TRIGGER_RECENT_FIRES,
  bracketActionsFor,
  bracketChangedPayload,
  bracketDownsidePct,
  bracketFires,
  bracketGauge,
  bracketHeroText,
  bracketLegOverText,
  bracketLegStateText,
  bracketLegWord,
  bracketNearest,
  bracketNowText,
  bracketRewardRisk,
  bracketRewardRiskText,
  bracketRowNowText,
  bracketRowPlanText,
  bracketSizeText,
  bracketStatusLabel,
  bracketUpsidePct,
  buildTriggerCard,
  createTriggerMounter,
  formatSignedPct,
  isBracketLegLive,
  normalizeBracket,
  normalizeTriggerPayload,
  recountTotals,
  triggerReadMethod,
  triggerStatusLabel,
  type Bracket,
  type BracketListPayload,
  type BracketOnePayload,
  type TriggerOnePayload,
  type TriggerPayload,
  type TriggerRenderContext,
  type TriggerStatus,
  unbreakable,
} from './trigger'

// docs/brackets.md is the contract. The engine writes these fixtures
// (AGENTOS_REGEN_TRIGGER_FIXTURES=1); the assertions stay structural and
// patch in any figure they need, so a regenerated set still passes.
const FIXTURES = '../tests/fixtures/trigger_cards'
const NAMES = [
  'bracket-armed',
  'bracket-awaiting',
  'bracket-done',
  'bracket-alert',
  'brackets',
  'brackets-empty',
] as const
type Name = (typeof NAMES)[number]
type One = Exclude<Name, 'brackets' | 'brackets-empty'>

type Json = Record<string, unknown>

function fixture(name: Name): Json {
  return JSON.parse(readFileSync(`${FIXTURES}/${name}.json`, 'utf8')) as Json
}

function payload<T extends TriggerPayload>(body: Json): T {
  const normalized = normalizeTriggerPayload(body)
  if (!normalized) throw new Error('fixture did not normalize')
  return normalized as T
}

function one(name: One): BracketOnePayload {
  return payload<BracketOnePayload>(fixture(name))
}

function idOf(name: One): string {
  return (fixture(name).bracket as Json).id as string
}

/** A fixture's raw body with its bracket patched. */
function withBracket(name: One, patch: (bracket: Json) => void): Json {
  const raw = fixture(name)
  patch(raw.bracket as Json)
  return raw
}

/** A fixture's raw body with its bracket in `status` (and its stamp moved on). */
function inStatus(name: One, status: TriggerStatus, extra: Json = {}): Json {
  return withBracket(name, (b) => {
    b.status = status
    b.updatedAt = '2026-12-31T00:00:00Z'
    Object.assign(b, extra)
  })
}

/** The bracket of a fixture with its market / lines / legs overridden. */
function bracketWith(
  name: One,
  market: Json,
  lines: Json = {},
  patch: (b: Json) => void = () => {},
): Bracket {
  const raw = withBracket(name, (b) => {
    b.market = { ...(b.market as Json), ...market }
    b.lines = { ...(b.lines as Json), ...lines }
    patch(b)
  })
  return payload<BracketOnePayload>(raw).bracket
}

function leg(b: Json, which: 'takeProfit' | 'stopLoss'): Json {
  return b[which] as Json
}

const NOW = Date.parse('2026-10-05T09:15:00Z')

function ctx(overrides: Partial<TriggerRenderContext> = {}): TriggerRenderContext {
  return {
    now: () => NOW,
    copyText: vi.fn(),
    setTimer: (fn, ms) => void setTimeout(fn, ms),
    ...overrides,
  }
}

function render(p: TriggerPayload, context = ctx()): HTMLElement {
  const card = buildTriggerCard(p, context)
  document.body.append(card)
  return card
}

function placeholder(src = '/api/v1/artifacts/brk-1'): HTMLElement {
  const node = document.createElement('div')
  node.className = 'msg-artifact-trigger'
  node.dataset.triggerSrc = src
  node.innerHTML = `<div class="msg-artifact-trigger__body"></div>
    <p class="msg-artifact-trigger__status">Loading trigger…</p>`
  document.body.append(node)
  return node
}

async function flush(): Promise<void> {
  for (let i = 0; i < 6; i++) await Promise.resolve()
}

function texts(root: ParentNode, selector: string): string[] {
  return [...root.querySelectorAll(selector)].map((n) => n.textContent ?? '')
}

function ops(root: ParentNode): string[] {
  return [...root.querySelectorAll<HTMLElement>('.trigger-actions [data-trigger-op]')].map(
    (b) => b.dataset.triggerOp ?? '',
  )
}

beforeEach(() => {
  document.body.replaceChildren()
})

afterEach(() => {
  vi.useRealTimers()
})

/* ── category + normalization ──────────────────────────────────────────── */

describe('a bracket artifact', () => {
  it('is a trigger artifact: one mime, one category', () => {
    expect(artifactCategory({ mime: TRIGGER_ARTIFACT_MIME, name: 'bracket-protect.json' })).toBe(
      'trigger',
    )
  })

  it('normalizes every bracket fixture to its kind, re-read through trading.bracket', () => {
    for (const name of NAMES) {
      const raw = fixture(name)
      const p = normalizeTriggerPayload(raw)
      expect(p, name).not.toBeNull()
      expect(p!.kind, name).toBe(raw.kind)
      expect(p!.request?.scope, name).toBe('bracket')
      expect(triggerReadMethod(p!.request!), name).toBe(
        `trading.bracket.${raw.kind === 'bracket' ? 'get' : 'list'}`,
      )
    }
  })

  it('keeps a bracket as the engine sent it, both legs included', () => {
    for (const name of [
      'bracket-armed',
      'bracket-awaiting',
      'bracket-done',
      'bracket-alert',
    ] as const) {
      const raw = fixture(name).bracket as Json
      const { bracket } = one(name)
      expect(bracket.id, name).toBe(raw.id)
      expect(bracket.id, name).toMatch(/^brk_/)
      expect(bracket.name, name).toBe(raw.name)
      expect(bracket.kind, name).toBe(raw.kind)
      expect(bracket.status, name).toBe(raw.status)
      expect(bracket.lines.takeProfitUsd, name).toBe((raw.lines as Json).takeProfitUsd)
      expect(bracket.lines.stopLossUsd, name).toBe((raw.lines as Json).stopLossUsd)
      expect(bracket.market.nearest, name).toBe((raw.market as Json).nearest ?? null)
      expect(bracket.fired, name).toBe(raw.fired ?? null)
      for (const [which, legName] of [
        ['takeProfit', 'tp'],
        ['stopLoss', 'sl'],
      ] as const) {
        const tr = which === 'takeProfit' ? bracket.takeProfit : bracket.stopLoss
        expect(tr?.id, `${name} ${which}`).toBe(leg(raw, which).id)
        expect(tr?.bracket, `${name} ${which}`).toEqual({
          id: bracket.id,
          name: expect.any(String) as unknown,
          leg: legName,
        })
      }
    }
    expect(one('bracket-armed').bracket.status).toBe('armed')
    expect(one('bracket-awaiting').bracket.status).toBe('awaiting_approval')
    expect(one('bracket-done').bracket.status).toBe('done')
    expect(one('bracket-done').bracket.fired).toBe('tp')
    expect(one('bracket-done').bracket.result?.orderId).toBeTruthy()
    expect(one('bracket-alert').bracket.kind).toBe('alert')
  })

  it('reads the list, its totals and the empty list', () => {
    const raw = fixture('brackets')
    const list = payload<BracketListPayload>(raw)
    expect(list.brackets.map((b) => b.id)).toEqual(
      (raw.brackets as Json[]).map((b) => b.id as string),
    )
    expect(list.totals.count).toBe((raw.totals as Json).count)
    const empty = payload<BracketListPayload>(fixture('brackets-empty'))
    expect(empty.brackets).toEqual([])
    expect(empty.totals.count).toBe(0)
  })

  it('reads a line off its leg when the payload leaves it out, a trail’s stop for a trailing leg', () => {
    const b = bracketWith(
      'bracket-armed',
      {},
      { takeProfitUsd: null, stopLossUsd: null, trailPct: null },
      (raw) => {
        const sl = leg(raw, 'stopLoss')
        sl.condition = {
          ...(sl.condition as Json),
          direction: 'trail',
          priceUsd: null,
          trailPct: 10,
          peakPriceUsd: 2100,
          stopPriceUsd: 1890,
        }
      },
    )
    expect(b.lines.takeProfitUsd).toBe(b.takeProfit!.condition.priceUsd)
    expect(b.lines.stopLossUsd).toBe(1890)
    expect(b.lines.trailPct).toBe(10)
  })

  it('never throws on junk and returns null when there is nothing to draw', () => {
    for (const junk of [
      { kind: 'bracket' },
      { kind: 'bracket', bracket: 'x' },
      { kind: 'bracket', bracket: { id: 'brk_x' } },
      { kind: 'brackets', brackets: 'x' },
    ]) {
      expect(() => normalizeTriggerPayload(junk)).not.toThrow()
      expect(normalizeTriggerPayload(junk), JSON.stringify(junk)).toBeNull()
    }
    const bare = normalizeBracket({ id: 'brk_bare', token: { symbol: 'ETH' } })!
    expect(bare.takeProfit).toBeNull()
    expect(bare.status).toBe('unknown')
    expect(bare.name).toContain('ETH')
    // It still draws: no gauge, two leg rows with nothing known.
    const card = render(
      payload({ kind: 'bracket', bracket: { id: 'brk_bare', token: { symbol: 'ETH' } } }),
      ctx({ canWrite: true }),
    )
    expect(card.querySelector('.trigger-gauge')).toBeNull()
    expect(card.querySelectorAll('.bracket-leg')).toHaveLength(2)
    expect(card.querySelector('.trigger-actions')).toBeNull()
  })

  it('maps a read to the right family', () => {
    expect(triggerReadMethod({ kind: 'get', params: { bracketId: 'brk_1' } })).toBe(
      'trading.bracket.get',
    )
    expect(triggerReadMethod({ kind: 'list', params: {}, scope: 'bracket' })).toBe(
      'trading.bracket.list',
    )
    expect(triggerReadMethod({ kind: 'get', params: { triggerId: 'trg_1' } })).toBe(
      'trading.trigger.get',
    )
    expect(triggerReadMethod({ kind: 'list', params: {} })).toBe('trading.trigger.list')
  })
})

/* ── text ──────────────────────────────────────────────────────────────── */

describe('bracket text', () => {
  it('says both lines in one sentence', () => {
    const sell = bracketWith(
      'bracket-armed',
      {},
      {
        takeProfitLabel: 'over $4,560',
        stopLossLabel: 'under $3,420',
      },
      (b) => {
        b.action = { ...(b.action as Json), amountPct: 100, amount: null, amountUsd: null }
      },
    )
    expect(bracketHeroText(sell)).toBe(
      `sell 100 % of ${sell.token.symbol} · take profit over $4,560 · stop under $3,420`,
    )
    const alert = bracketWith(
      'bracket-alert',
      {},
      {
        takeProfitLabel: 'over $4,560',
        stopLossLabel: 'under $3,420',
      },
    )
    expect(bracketHeroText(alert)).toBe(
      `notify when ${alert.token.symbol} is over $4,560 or under $3,420`,
    )
    // No labels: derived from the lines.
    const derived = bracketWith(
      'bracket-armed',
      {},
      {
        takeProfitLabel: '',
        stopLossLabel: '',
        takeProfitUsd: 4560,
        stopLossUsd: 3420,
        trailPct: null,
      },
      (b) => {
        ;(leg(b, 'takeProfit').condition as Json).label = ''
        ;(leg(b, 'stopLoss').condition as Json).label = ''
      },
    )
    expect(bracketHeroText(derived)).toMatch(/take profit over \$4,560 · stop under \$3,420$/)
  })

  it('reads price · upside · downside · checked for an armed bracket', () => {
    const b = bracketWith(
      'bracket-armed',
      {
        priceUsd: 3790,
        upsidePct: 20.3,
        downsidePct: -9.8,
        checkedAt: new Date(NOW - 12_000).toISOString(),
      },
      {},
      (raw) => {
        raw.status = 'armed'
        for (const w of ['takeProfit', 'stopLoss'] as const) {
          ;(leg(raw, w).condition as Json).hits = 0
        }
      },
    )
    expect(bracketNowText(b, NOW)).toBe(
      `${b.token.symbol} $3,790 · +20.3 % to take-profit · −9.8 % to stop · checked 12 s ago`,
    )
    expect(bracketNowText({ ...b, market: { ...b.market, priceUsd: null } }, NOW)).toBe(
      'armed, waiting for a price · checked 12 s ago',
    )
    // One leg one check from firing says so instead of the distances.
    const confirming = bracketWith('bracket-armed', { priceUsd: 3790 }, {}, (raw) => {
      raw.status = 'armed'
      const tp = leg(raw, 'takeProfit')
      tp.status = 'armed'
      tp.condition = { ...(tp.condition as Json), hits: 1, confirmTicks: 2 }
      ;(leg(raw, 'stopLoss').condition as Json).hits = 0
    })
    expect(bracketNowText(confirming, NOW)).toContain('take-profit fires after 1 more check')
  })

  it('derives the distances, the nearest leg and reward : risk when the engine sends none', () => {
    const b = bracketWith(
      'bracket-armed',
      {
        priceUsd: 2000,
        upsidePct: null,
        downsidePct: null,
        rewardRisk: null,
        nearest: null,
      },
      { takeProfitUsd: 2400, stopLossUsd: 1800 },
    )
    expect(bracketUpsidePct(b)).toBeCloseTo(20, 5)
    expect(bracketDownsidePct(b)).toBeCloseTo(-10, 5)
    expect(bracketNearest(b)).toBe('sl')
    expect(bracketRewardRisk(b)).toBe(2)
    expect(bracketRewardRiskText(b)).toBe('2.0 : 1')
    const met = { ...b, market: { ...b.market, priceUsd: 2500 } }
    expect(bracketUpsidePct(met)).toBe(0)
    expect(bracketRewardRisk(met)).toBeNull()
    expect(bracketRewardRiskText(met)).toBe('—')
    expect(formatSignedPct(20.34)).toBe('+20.3 %')
    expect(formatSignedPct(-9.8)).toBe('−9.8 %')
    expect(formatSignedPct(null)).toBe('')
  })

  it('words every other status', () => {
    const at = (status: TriggerStatus, extra: Json = {}): string =>
      bracketNowText(
        payload<BracketOnePayload>(inStatus('bracket-armed', status, extra)).bracket,
        NOW,
      )
    expect(at('awaiting_approval', { expiresAt: null })).toBe('awaiting approval')
    expect(
      at('awaiting_approval', { expiresAt: new Date(NOW + 90 * 60_000).toISOString() }),
    ).toMatch(/^awaiting approval · proposal expires in /)
    expect(at('paused')).toBe('paused · not watching the price')
    expect(at('stopped')).toBe('stopped · will not fire')
    expect(at('rejected')).toBe('rejected · never armed')
    expect(at('expired')).toBe('expired · will not fire')
    const triggered = payload<BracketOnePayload>(
      withBracket('bracket-armed', (b) => {
        b.status = 'triggered'
        leg(b, 'stopLoss').status = 'triggered'
        leg(b, 'takeProfit').status = 'paused'
      }),
    ).bracket
    expect(bracketNowText(triggered, NOW)).toBe('on hold · stop-loss order open')
    expect(bracketStatusLabel('armed')).toBe(triggerStatusLabel('armed'))
  })

  it('ends a done bracket at the leg that filled', () => {
    const { bracket } = one('bracket-done')
    expect(bracket.fired).not.toBeNull()
    const word = bracket.fired === 'tp' ? 'take-profit' : 'stop-loss'
    expect(bracketNowText(bracket, NOW)).toMatch(
      new RegExp(`^done · ${word}: sold [\\d.,]+ \\S+ at \\$[\\d,.]+$`),
    )
  })

  it('words the size, a partial take-profit and what a fill moved', () => {
    const b = bracketWith('bracket-armed', {}, {}, (raw) => {
      raw.action = {
        ...(raw.action as Json),
        amountPct: 100,
        amount: null,
        amountUsd: null,
        tpPct: null,
        estimatedUsd: 189,
      }
    })
    expect(bracketSizeText(b)).toBe('100 % · ≈ $189')
    expect(bracketSizeText({ ...b, action: { ...b.action, tpPct: 50 } })).toBe(
      '50 % at take-profit, 100 % at stop',
    )
    expect(bracketSizeText(one('bracket-alert').bracket)).toBe('notify only')
    expect(bracketSizeText(one('bracket-done').bracket)).toContain('→')
  })
})

/* ── the range gauge ───────────────────────────────────────────────────── */

describe('bracketGauge', () => {
  const lines = { takeProfitUsd: 2400, stopLossUsd: 1800 }

  it('puts the stop left, the take-profit right and the price between them', () => {
    const g = bracketGauge(bracketWith('bracket-armed', { priceUsd: 2000 }, lines))!
    expect(g.lo).toBeLessThan(1800)
    expect(g.hi).toBeGreaterThan(2400)
    expect(g.sl).toBeGreaterThan(0)
    expect(g.tp).toBeLessThan(1)
    expect(g.sl).toBeLessThan(g.current!)
    expect(g.current!).toBeLessThan(g.tp)
    // A third of the way from the stop to the take-profit.
    expect((g.current! - g.sl) / (g.tp - g.sl)).toBeCloseTo(1 / 3, 5)
    expect(g.zone).toEqual([g.sl, g.tp])
    expect(g.clamped).toBeNull()
  })

  it('clamps a price far outside the lines into the rail', () => {
    const low = bracketGauge(bracketWith('bracket-armed', { priceUsd: 100 }, lines))!
    expect(low.current).toBe(0)
    expect(low.clamped).toBe('below')
    const high = bracketGauge(bracketWith('bracket-armed', { priceUsd: 99_000 }, lines))!
    expect(high.current).toBe(1)
    expect(high.clamped).toBe('above')
    // Just past a line but inside the padding: past the tick, not clamped.
    const past = bracketGauge(bracketWith('bracket-armed', { priceUsd: 2450 }, lines))!
    expect(past.current!).toBeGreaterThan(past.tp)
    expect(past.current!).toBeLessThan(1)
    expect(past.clamped).toBeNull()
  })

  it('draws a trailing stop at its stop line now', () => {
    const b = bracketWith(
      'bracket-armed',
      { priceUsd: 2000 },
      { stopLossUsd: null, takeProfitUsd: 2400 },
      (raw) => {
        const sl = leg(raw, 'stopLoss')
        sl.condition = {
          ...(sl.condition as Json),
          direction: 'trail',
          priceUsd: null,
          trailPct: 10,
          peakPriceUsd: 2100,
          stopPriceUsd: 1890,
        }
      },
    )
    const g = bracketGauge(b)!
    expect(g.lo + (g.hi - g.lo) * g.sl).toBeCloseTo(1890, 5)
  })

  it('has nothing to draw without both lines, or with them the wrong way round', () => {
    expect(
      bracketGauge(
        bracketWith('bracket-armed', {}, { takeProfitUsd: null }, (raw) => {
          ;(leg(raw, 'takeProfit').condition as Json).priceUsd = null
        }),
      ),
    ).toBeNull()
    expect(
      bracketGauge(bracketWith('bracket-armed', {}, { takeProfitUsd: 1000, stopLossUsd: 1800 })),
    ).toBeNull()
  })

  it('renders the exact hooks: rail, two ticks, the zone, the dot, three labels', () => {
    const p = payload<BracketOnePayload>(
      withBracket('bracket-armed', (b) => {
        b.status = 'armed'
        b.lines = { ...(b.lines as Json), ...lines }
        b.market = {
          ...(b.market as Json),
          priceUsd: 2000,
          upsidePct: 20,
          downsidePct: -10,
          nearest: 'sl',
        }
      }),
    )
    const card = render(p)
    expect(card.dataset.triggerNearest).toBe('sl')
    const gauge = card.querySelector<HTMLElement>('.trigger-gauge[data-trigger-gauge="range"]')!
    expect(gauge).not.toBeNull()
    expect(gauge.dataset.triggerDist).toBe('-10')
    expect(gauge.getAttribute('role')).toBe('img')
    expect(gauge.querySelector('svg.trigger-gauge__svg')).not.toBeNull()
    expect(gauge.querySelector('line.trigger-gauge__line')?.getAttribute('x2')).toBe('100%')
    expect(
      [...gauge.querySelectorAll('.trigger-gauge__tick')].map((n) => n.getAttribute('data-leg')),
    ).toEqual(['sl', 'tp'])
    expect(gauge.querySelector('.trigger-gauge__zone')?.getAttribute('data-zone')).toBe('bracket')
    expect(gauge.querySelector('circle.trigger-gauge__dot')?.getAttribute('cx')).toMatch(/%$/)
    expect(
      [...gauge.querySelectorAll<HTMLElement>('.trigger-gauge__label')].map((n) => n.dataset.leg),
    ).toEqual(['sl', 'tp', 'now'])
    expect(texts(gauge, '.trigger-gauge__label')).toEqual([
      'stop $1,800',
      'take profit $2,400',
      'now $2,000',
    ])
    const x = (sel: string): number =>
      parseFloat(gauge.querySelector(sel)!.getAttribute(sel.includes('dot') ? 'cx' : 'x1')!)
    expect(x('.trigger-gauge__tick[data-leg="sl"]')).toBeLessThan(x('.trigger-gauge__dot'))
    expect(x('.trigger-gauge__dot')).toBeLessThan(x('.trigger-gauge__tick[data-leg="tp"]'))
    // A finished bracket has no distance left to tint.
    const done = render(one('bracket-done')).querySelector<HTMLElement>('.trigger-gauge')
    if (done) expect(done.dataset.triggerDist).toBeUndefined()
  })
})

/* ── the card ──────────────────────────────────────────────────────────── */

describe('buildTriggerCard — bracket', () => {
  it('reads like an instrument: the contract root, head, hero, gauge, legs, facts, footer', () => {
    const p = one('bracket-armed')
    const card = render(p)
    expect(card.tagName).toBe('ARTICLE')
    expect(card).toHaveClass('trigger-card')
    expect(card.dataset.triggerKind).toBe('bracket')
    expect(card.dataset.triggerAction).toBe('sell')
    expect(card.dataset.triggerStatus).toBe('armed')
    expect(card.dataset.triggerChain).toBe(p.bracket.chain!.key)
    expect(card.dataset.triggerLayout).toBe('narrow')
    expect(card.dataset.triggerNearest).toMatch(/^(tp|sl)$/)
    expect(card.querySelector('.trigger-card__head .trigger-card__name')).toHaveTextContent(
      p.bracket.name,
    )
    expect(card.querySelector('.trigger-pill')?.getAttribute('data-status')).toBe('armed')
    expect(card.querySelector('.trigger-card__glyph')?.textContent).toBe('')
    expect(card.querySelector('.trigger-card__hero-line')?.textContent).toBe(
      bracketHeroText(p.bracket),
    )
    expect(card.querySelector('.trigger-card__now')?.textContent).toBe(
      bracketNowText(p.bracket, NOW),
    )
    expect(card.querySelector('.trigger-gauge')).not.toBeNull()
    expect(card.querySelector('.bracket-legs')).not.toBeNull()
    const meta = card.querySelector('.trigger-card__foot-meta')!.textContent!
    expect(meta.startsWith(`${p.bracket.id} · `)).toBe(true)
    expect(meta).toMatch(/\(0x[0-9a-fA-F]{4}…[0-9a-fA-F]{4}\)/)
    expect(card.lastElementChild).toHaveClass('trigger-card__foot')
    // The order of the sections.
    expect([...card.children].map((n) => n.className.split(' ')[0])).toEqual(
      expect.arrayContaining(['trigger-card__head', 'trigger-card__hero', 'trigger-gauge']),
    )
    const order = [...card.children].map((n) => n.className.split(' ')[0])
    expect(order.indexOf('trigger-gauge')).toBeLessThan(order.indexOf('bracket-legs'))
    expect(order.indexOf('bracket-legs')).toBeLessThan(order.indexOf('trigger-card__facts'))
  })

  it('shows both legs as two rows: word, line, state', () => {
    const p = payload<BracketOnePayload>(
      withBracket('bracket-armed', (b) => {
        const tp = leg(b, 'takeProfit')
        tp.status = 'armed'
        tp.condition = { ...(tp.condition as Json), hits: 1, confirmTicks: 2 }
        const sl = leg(b, 'stopLoss')
        sl.status = 'armed'
        sl.condition = { ...(sl.condition as Json), hits: 0, direction: 'below' }
        b.lines = { ...(b.lines as Json), trailPct: null }
      }),
    )
    const card = render(p)
    const rows = [...card.querySelectorAll<HTMLElement>('.bracket-legs .bracket-leg')]
    expect(rows.map((r) => [r.dataset.leg, r.dataset.status])).toEqual([
      ['tp', 'armed'],
      ['sl', 'armed'],
    ])
    expect(texts(card, '.bracket-leg__word')).toEqual(['Take-profit', 'Stop-loss'])
    expect(rows[0]!.querySelector('.bracket-leg__line')?.textContent).toBe(
      (p.bracket.lines.takeProfitLabel || p.bracket.takeProfit!.condition.label) as string,
    )
    expect(rows[0]!.querySelector('.bracket-leg__state')).toHaveTextContent('armed · 1 of 2 checks')
    expect(rows[1]!.querySelector('.bracket-leg__state')?.textContent).toMatch(/^armed/)
  })

  it('says a leg is on hold, stopped by its twin, or done', () => {
    const held = payload<BracketOnePayload>(
      withBracket('bracket-armed', (b) => {
        b.status = 'triggered'
        leg(b, 'takeProfit').status = 'triggered'
        Object.assign(leg(b, 'stopLoss'), {
          status: 'paused',
          statusReason: 'on hold: take-profit fired',
        })
      }),
    )
    const card = render(held)
    const sl = card.querySelector<HTMLElement>('.bracket-leg[data-leg="sl"]')!
    expect(sl.dataset.status).toBe('paused')
    expect(sl.dataset.hold).toBe('true')
    expect(sl.querySelector('.bracket-leg__state')).toHaveTextContent('on hold')
    expect(bracketLegStateText(held.bracket.takeProfit)).toBe('fired · order open')

    const done = render(one('bracket-done'))
    const fired = one('bracket-done').bracket.fired!
    const other = fired === 'tp' ? 'sl' : 'tp'
    const firedRow = done.querySelector<HTMLElement>(`.bracket-leg[data-leg="${fired}"]`)!
    expect(firedRow.dataset.fired).toBe('true')
    expect(firedRow.querySelector('.bracket-leg__state')?.textContent).toMatch(/^done/)
    const otherRow = done.querySelector<HTMLElement>(`.bracket-leg[data-leg="${other}"]`)!
    expect(otherRow.dataset.status).toBe('stopped')
    expect(otherRow.querySelector('.bracket-leg__state')?.textContent).toMatch(/^stopped · /)
    expect(bracketLegStateText(null)).toBe('—')
  })

  it('names a trailing stop leg', () => {
    const card = render(
      payload(
        withBracket('bracket-armed', (b) => {
          b.lines = { ...(b.lines as Json), trailPct: 10, stopLossLabel: '10 % below peak' }
        }),
      ),
    )
    expect(card.querySelector('.bracket-leg[data-leg="sl"] .bracket-leg__word')).toHaveTextContent(
      'Trailing stop',
    )
    expect(card.querySelector('.bracket-leg[data-leg="sl"] .bracket-leg__line')).toHaveTextContent(
      '10 % below peak',
    )
  })

  it('merges both legs’ fires, newest first, each named by its leg', () => {
    const fire = (n: number, minute: number, status = 'failed'): Json => ({
      n,
      at: `2026-10-05T09:${String(minute).padStart(2, '0')}:00Z`,
      status,
      manual: false,
      reason: 'no route',
    })
    const raw = withBracket('bracket-armed', (b) => {
      leg(b, 'takeProfit').fires = [fire(3, 10), fire(2, 6), fire(1, 2)]
      leg(b, 'stopLoss').fires = [fire(3, 12), fire(2, 8), fire(1, 4)]
    })
    const p = payload<BracketOnePayload>(raw)
    const rows = bracketFires(p.bracket)
    expect(rows).toHaveLength(TRIGGER_RECENT_FIRES)
    expect(rows.map((r) => `${r.leg}${r.fire.n}`)).toEqual(['sl3', 'tp3', 'sl2', 'tp2', 'sl1'])
    const card = render(p)
    const lis = [...card.querySelectorAll<HTMLElement>('.trigger-fires .trigger-fire')]
    expect(lis.map((li) => li.dataset.leg)).toEqual(['sl', 'tp', 'sl', 'tp', 'sl'])
    expect(texts(card, '.trigger-fire__n')).toEqual([
      'stop-loss #3',
      'take-profit #3',
      'stop-loss #2',
      'take-profit #2',
      'stop-loss #1',
    ])
    expect(lis[0]!.querySelector('.trigger-fire__detail')).toHaveTextContent('no route')
  })

  it('lays the facts out per kind and status', () => {
    const facts = (p: TriggerPayload): string[] =>
      [...render(p).querySelectorAll<HTMLElement>('.trigger-card__facts .trigger-fact')].map(
        (n) => n.dataset.triggerFact ?? '',
      )
    const armed = payload<BracketOnePayload>(
      withBracket('bracket-armed', (b) => {
        b.validUntil = null
        b.market = { ...(b.market as Json), rewardRisk: 2.1 }
      }),
    )
    expect(facts(armed)).toEqual(['size', 'balance', 'rr', 'approval'])
    const card = render(armed)
    const rr = card.querySelector('[data-trigger-fact="rr"] .trigger-fact__value')!
    expect(rr).toHaveClass('trigger-fact__rr')
    expect(rr).toHaveTextContent('2.1 : 1')
    expect(card.querySelector('[data-trigger-fact="rr"] .trigger-fact__label')).toHaveTextContent(
      'reward : risk',
    )
    expect(
      facts(payload(inStatus('bracket-armed', 'armed', { validUntil: '2027-03-05T12:00:00Z' }))),
    ).toEqual(['size', 'balance', 'rr', 'approval', 'valid'])
    expect(facts(payload(inStatus('bracket-alert', 'armed', { validUntil: null })))).toEqual([
      'size',
      'rr',
    ])
    expect(facts(payload(inStatus('bracket-done', 'done', { validUntil: null })))).toEqual([
      'size',
      'approval',
    ])
  })

  it('draws each status with exactly its controls on the desk', () => {
    const expected: Record<string, string[]> = {
      awaiting_approval: ['approve', 'reject'],
      armed: ['pause', 'fire', 'stop'],
      paused: ['resume', 'fire', 'stop'],
      triggered: ['stop'],
      done: [],
      stopped: [],
      rejected: [],
      expired: [],
    }
    for (const [status, controls] of Object.entries(expected)) {
      const card = render(
        payload(inStatus('bracket-armed', status as TriggerStatus)),
        ctx({ canWrite: true }),
      )
      expect(card.dataset.triggerStatus, status).toBe(status)
      expect(card.querySelector('.trigger-pill')?.getAttribute('data-status'), status).toBe(status)
      expect(ops(card), status).toEqual(controls)
      expect(bracketActionsFor(status as TriggerStatus), status).toEqual(controls)
      expect(card.querySelector('.trigger-card__now')?.textContent, status).not.toBe('')
      expect(card.querySelectorAll('.bracket-leg'), status).toHaveLength(2)
    }
  })

  it('renders no controls at all without canWrite', () => {
    for (const name of ['bracket-armed', 'bracket-awaiting', 'bracket-alert'] as const) {
      expect(render(one(name)).querySelector('.trigger-actions'), name).toBeNull()
    }
    expect(render(payload(fixture('brackets'))).querySelector('.trigger-actions')).toBeNull()
  })

  it('keys its buttons by bracket id and words fire for the kind', () => {
    const awaiting = one('bracket-awaiting')
    const card = render(awaiting, ctx({ canWrite: true }))
    const buttons = [...card.querySelectorAll<HTMLButtonElement>('.trigger-actions button')]
    expect(
      buttons.map((b) => [b.type, b.dataset.triggerOp, b.dataset.bracketId, b.textContent]),
    ).toEqual([
      ['button', 'approve', awaiting.bracket.id, 'Approve & arm'],
      ['button', 'reject', awaiting.bracket.id, 'Reject'],
    ])
    expect(buttons.every((b) => b.dataset.triggerId === undefined)).toBe(true)
    expect(card.querySelector<HTMLElement>('.trigger-actions')!.dataset.bracketId).toBe(
      awaiting.bracket.id,
    )
    expect(
      texts(
        render(payload(inStatus('bracket-armed', 'armed')), ctx({ canWrite: true })),
        '[data-trigger-op]',
      ),
    ).toEqual(['Pause', 'Sell now', 'Stop'])
    expect(
      texts(
        render(payload(inStatus('bracket-alert', 'paused')), ctx({ canWrite: true })),
        '[data-trigger-op]',
      ),
    ).toEqual(['Resume', 'Notify now', 'Stop'])
  })

  it('keeps attacker-chosen strings as text', () => {
    const evil = '<img src=x onerror=alert(1)>'
    const raw = withBracket('bracket-done', (b) => {
      b.name = evil
      b.token = { ...(b.token as Json), symbol: evil }
      b.statusReason = evil
      b.lines = { ...(b.lines as Json), takeProfitLabel: evil, stopLossLabel: evil }
    })
    raw.warnings = [evil]
    const card = render(payload(raw), ctx({ canWrite: true, canRefresh: true }))
    expect(card.querySelector('img')).toBeNull()
    expect(card.querySelector('.trigger-card__name')?.textContent).toBe(evil)
    expect(card.querySelector('.bracket-leg__line')?.textContent).toBe(evil)
  })
})

/* ── a leg's own card ──────────────────────────────────────────────────── */

describe('a leg’s own trigger card', () => {
  it('names its bracket in the header and offers no controls', () => {
    const b = fixture('bracket-armed').bracket as Json
    for (const [which, word] of [
      ['takeProfit', 'take-profit'],
      ['stopLoss', 'stop-loss'],
    ] as const) {
      const p = payload<TriggerOnePayload>({ kind: 'trigger', trigger: leg(b, which) })
      expect(p.trigger.bracket?.id).toBe(b.id)
      const card = render(p, ctx({ canWrite: true }))
      const group = card.querySelector('.trigger-card__head .trigger-card__group')
      expect(group).toHaveTextContent(`${word} leg of ${b.name as string} · ${b.id as string}`)
      expect(card.querySelector('.trigger-actions')).toBeNull()
    }
    // A plain trigger has neither the line nor the field.
    const plain = payload<TriggerOnePayload>(
      JSON.parse(readFileSync(`${FIXTURES}/trigger-armed.json`, 'utf8')) as Json,
    )
    expect(plain.trigger.bracket).toBeNull()
    const card = render(plain, ctx({ canWrite: true }))
    expect(card.querySelector('.trigger-card__group')).toBeNull()
    expect(card.querySelector('.trigger-actions')).not.toBeNull()
  })
})

/* ── the list ──────────────────────────────────────────────────────────── */

describe('buildTriggerCard — brackets', () => {
  it('lists every bracket: plan, live price, compact controls', () => {
    const p = payload<BracketListPayload>(fixture('brackets'))
    const card = render(p, ctx({ canWrite: true }))
    expect(card.dataset.triggerKind).toBe('brackets')
    expect(card.querySelector('.trigger-card__list-title')).toHaveTextContent('Brackets')
    expect(card.querySelector('.trigger-card__count')).toHaveTextContent(String(p.totals.count))
    const rows = [...card.querySelectorAll<HTMLElement>('.trigger-rows .trigger-row')]
    expect(rows.map((r) => r.dataset.bracketId)).toEqual(p.brackets.map((b) => b.id))
    rows.forEach((row, i) => {
      const b = p.brackets[i]!
      expect(row.dataset.triggerStatus).toBe(b.status)
      expect(row.querySelector('.trigger-row__plan')?.textContent).toBe(bracketRowPlanText(b))
      expect(row.querySelector('.trigger-row__plan')?.textContent).toMatch(
        /^(sell|alert) .+ · \$[\d,.]+ – \$[\d,.]+$/,
      )
      expect(ops(row)).toEqual(bracketActionsFor(b.status))
      row.querySelectorAll<HTMLElement>('[data-trigger-op]').forEach((btn) => {
        expect(btn.dataset.triggerCompact).toBe('true')
        expect(btn.dataset.bracketId).toBe(b.id)
      })
    })
  })

  it('writes the row price line', () => {
    const b = bracketWith(
      'bracket-armed',
      { priceUsd: 3790, upsidePct: 20.3, downsidePct: -9.8 },
      {},
      (raw) => {
        raw.status = 'armed'
        for (const w of ['takeProfit', 'stopLoss'] as const) {
          ;(leg(raw, w).condition as Json).hits = 0
        }
      },
    )
    expect(bracketRowNowText(b)).toBe(`${b.token.symbol} $3,790 · +20.3 % / −9.8 %`)
  })

  it('says so when there are none', () => {
    const card = render(payload(fixture('brackets-empty')))
    expect(card.querySelector('.trigger-card__empty')).toHaveTextContent('No brackets yet.')
    expect(card.querySelector('.trigger-rows')).toBeNull()
  })
})

/* ── controls → RPC → payload swap ─────────────────────────────────────── */

function mountWith(
  body: Json,
  call: (method: string, params: Record<string, unknown>) => Promise<unknown>,
  extra: {
    onOrder?: (id: string) => void
    read?: (method: string, params: Record<string, unknown>) => Promise<unknown>
  } = {},
) {
  const host = placeholder()
  const mounter = createTriggerMounter({
    fetchPayload: () => Promise.resolve(body),
    call: extra.read,
    actions: { call, onOrder: extra.onOrder },
    now: () => NOW,
  })
  mounter.mountTrigger(document.body)
  return { host, mounter }
}

function button(host: HTMLElement, op: string): HTMLButtonElement {
  return host.querySelector<HTMLButtonElement>(`.trigger-actions [data-trigger-op="${op}"]`)!
}

describe('bracket control → RPC → payload swap', () => {
  it('pauses through trading.bracket.pause: busy while in flight, then the answer', async () => {
    let resolve: (v: unknown) => void = () => {}
    const call = vi.fn(() => new Promise((r) => (resolve = r)))
    const body = inStatus('bracket-armed', 'armed')
    const { host, mounter } = mountWith(body, call)
    await flush()
    button(host, 'pause').click()
    expect(call).toHaveBeenCalledWith('trading.bracket.pause', {
      bracketId: idOf('bracket-armed'),
    })
    const row = host.querySelector<HTMLElement>('.trigger-actions')!
    expect(row.dataset.triggerBusy).toBe('pause')
    expect(host.querySelector('.trigger-card')).toHaveAttribute('data-trigger-busy', 'true')
    expect([...row.querySelectorAll('button')].every((b) => b.disabled)).toBe(true)
    resolve(inStatus('bracket-armed', 'paused', { updatedAt: '2027-01-01T00:00:00Z' }))
    await flush()
    const card = host.querySelector<HTMLElement>('.trigger-card')!
    expect(card.dataset.triggerStatus).toBe('paused')
    expect(card).not.toHaveAttribute('data-trigger-busy')
    expect(texts(host, '.trigger-actions [data-trigger-op]')).toEqual([
      'Resume',
      'Sell now',
      'Stop',
    ])
    mounter.destroyAll()
  })

  it('approves both legs at once', async () => {
    const call = vi.fn(() => Promise.resolve(inStatus('bracket-awaiting', 'armed')))
    const { host, mounter } = mountWith(fixture('bracket-awaiting'), call)
    await flush()
    button(host, 'approve').click()
    await flush()
    expect(call).toHaveBeenCalledWith('trading.bracket.approve', {
      bracketId: idOf('bracket-awaiting'),
    })
    expect(host.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'armed')
    mounter.destroyAll()
  })

  it('asks for a second click before Sell now, then hands the order to the desk', async () => {
    vi.useFakeTimers()
    const onOrder = vi.fn()
    const call = vi.fn(() => {
      const raw = inStatus('bracket-armed', 'triggered')
      raw.fire = {
        n: 1,
        at: '2026-10-05T09:15:01Z',
        status: 'pending',
        orderId: 'ord_brk',
        manual: true,
      }
      return Promise.resolve(raw)
    })
    const { host, mounter } = mountWith(inStatus('bracket-armed', 'armed'), call, { onOrder })
    await flush()
    const fire = button(host, 'fire')
    fire.click()
    expect(call).not.toHaveBeenCalled()
    expect(fire.dataset.triggerConfirm).toBe('true')
    expect(fire).toHaveTextContent('Sell now — click again')
    vi.advanceTimersByTime(TRIGGER_CONFIRM_MS)
    expect(fire.dataset.triggerConfirm).toBeUndefined()
    fire.click()
    fire.click()
    await flush()
    expect(call).toHaveBeenCalledWith('trading.bracket.fire', { bracketId: idOf('bracket-armed') })
    expect(onOrder).toHaveBeenCalledWith('ord_brk')
    expect(texts(host, '.trigger-actions [data-trigger-op]')).toEqual(['Stop'])
    mounter.destroyAll()
  })

  it('asks for a second click before stopping both legs', async () => {
    vi.useFakeTimers()
    const call = vi.fn(() => Promise.resolve(inStatus('bracket-armed', 'stopped')))
    const { host, mounter } = mountWith(inStatus('bracket-armed', 'armed'), call)
    await flush()
    const stop = button(host, 'stop')
    stop.click()
    expect(call).not.toHaveBeenCalled()
    expect(stop.title).toBe('Click again to stop both legs for good')
    stop.click()
    await flush()
    expect(call).toHaveBeenCalledWith('trading.bracket.stop', { bracketId: idOf('bracket-armed') })
    expect(host.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'stopped')
    expect(host.querySelector('.trigger-actions')).toBeNull()
    mounter.destroyAll()
  })

  it('shows a refusal inline; an out-of-date one re-reads through trading.bracket.get', async () => {
    const codes = ['wallet.locked', 'trading.bracket.bad_state']
    const act = vi.fn(() =>
      Promise.reject(Object.assign(new Error('nope'), { code: codes.shift() })),
    )
    const read = vi.fn(() => Promise.resolve(inStatus('bracket-armed', 'armed')))
    const { host, mounter } = mountWith(inStatus('bracket-armed', 'armed'), act, { read })
    await flush()
    expect(read).toHaveBeenCalledWith('trading.bracket.get', { bracketId: idOf('bracket-armed') })
    read.mockClear()
    button(host, 'pause').click()
    await flush()
    expect(host.querySelector('.trigger-actions__error')).toHaveTextContent('nope')
    expect(button(host, 'pause').disabled).toBe(false)
    expect(read).not.toHaveBeenCalled()
    read.mockImplementationOnce(() =>
      Promise.resolve(inStatus('bracket-armed', 'done', { updatedAt: '2027-02-01T00:00:00Z' })),
    )
    button(host, 'pause').click()
    await flush()
    expect(read).toHaveBeenCalledWith('trading.bracket.get', { bracketId: idOf('bracket-armed') })
    expect(host.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'done')
    mounter.destroyAll()
  })

  it('treats an answer that is not a bracket payload as an error', async () => {
    const call = vi.fn(() =>
      Promise.resolve(
        JSON.parse(readFileSync(`${FIXTURES}/trigger-armed.json`, 'utf8')) as unknown,
      ),
    )
    const { host, mounter } = mountWith(inStatus('bracket-armed', 'armed'), call)
    await flush()
    button(host, 'pause').click()
    await flush()
    expect(host.querySelector('.trigger-actions__error')).toHaveTextContent(
      'Trigger data could not be read.',
    )
    mounter.destroyAll()
  })

  it('swaps a row of a brackets list in place', async () => {
    const list = payload<BracketListPayload>(fixture('brackets'))
    const target = list.brackets[0]!
    const actionFor = bracketActionsFor(target.status)[0]!
    const call = vi.fn(() =>
      Promise.resolve({
        kind: 'bracket',
        fetchedAt: '2026-10-05T09:16:00Z',
        bracket: {
          ...(fixture('brackets').brackets as Json[]).find((b) => b.id === target.id),
          status: 'stopped',
          updatedAt: '2027-01-01T00:00:00Z',
        },
      }),
    )
    const { host, mounter } = mountWith(fixture('brackets'), call)
    await flush()
    host
      .querySelector<HTMLButtonElement>(
        `.trigger-row[data-bracket-id="${target.id}"] [data-trigger-op="${actionFor}"]`,
      )!
      .click()
    if (actionFor === 'fire' || actionFor === 'stop') {
      host
        .querySelector<HTMLButtonElement>(
          `.trigger-row[data-bracket-id="${target.id}"] [data-trigger-op="${actionFor}"]`,
        )!
        .click()
    }
    await flush()
    expect(call).toHaveBeenCalledWith(`trading.bracket.${actionFor}`, { bracketId: target.id })
    expect(host.querySelector(`.trigger-row[data-bracket-id="${target.id}"]`)).toHaveAttribute(
      'data-trigger-status',
      'stopped',
    )
    expect(host.querySelector('.trigger-card')).toHaveAttribute('data-trigger-kind', 'brackets')
    // The header is recounted from the rows, never left at the snapshot's.
    const after = recountTotals(
      list.totals,
      list.brackets.map((b) => (b.id === target.id ? { status: 'stopped' as const } : b)),
    )
    expect(host.querySelector('.trigger-totals')?.textContent ?? '').toBe(
      [
        after.armed ? `${after.armed} armed` : '',
        after.awaiting ? `${after.awaiting} awaiting` : '',
        after.triggered ? `${after.triggered} triggered` : '',
      ]
        .filter(Boolean)
        .join(' · '),
    )
    mounter.destroyAll()
  })

  it('recounts the list header after a row action: "2 armed" becomes "1 armed"', async () => {
    const list = payload<BracketListPayload>(fixture('brackets'))
    const armed = list.brackets.filter((b) => b.status === 'armed')
    const awaiting = list.brackets.filter((b) => b.status === 'awaiting_approval').length
    expect(armed.length).toBeGreaterThan(0)
    const target = armed[0]!
    const call = vi.fn(() =>
      Promise.resolve({
        kind: 'bracket',
        fetchedAt: '2026-10-05T09:16:00Z',
        bracket: {
          ...(fixture('brackets').brackets as Json[]).find((b) => b.id === target.id),
          status: 'paused',
          updatedAt: '2027-01-01T00:00:00Z',
        },
      }),
    )
    const { host, mounter } = mountWith(fixture('brackets'), call)
    await flush()
    const totals = (): string => host.querySelector('.trigger-totals')?.textContent ?? ''
    expect(totals()).toContain(`${armed.length} armed`)
    host
      .querySelector<HTMLButtonElement>(
        `.trigger-row[data-bracket-id="${target.id}"] [data-trigger-op="pause"]`,
      )!
      .click()
    await flush()
    expect(call).toHaveBeenCalledWith('trading.bracket.pause', { bracketId: target.id })
    const want = [
      armed.length > 1 ? `${armed.length - 1} armed` : '',
      awaiting ? `${awaiting} awaiting` : '',
    ]
      .filter(Boolean)
      .join(' · ')
    expect(totals()).toBe(want)
    expect(host.querySelector('.trigger-card__count')).toHaveTextContent(String(list.totals.count))
    // An event moves it the same way.
    mounter.bracketChanged({
      bracket: {
        ...(fixture('brackets').brackets as Json[]).find((b) => b.id === target.id),
        status: 'armed',
        updatedAt: '2027-01-02T00:00:00Z',
      },
    })
    expect(totals()).toContain(`${armed.length} armed`)
    mounter.destroyAll()
  })

  it('recounts totals by status and keeps the engine count', () => {
    const totals = { count: 3, armed: 2, awaiting: 1, triggered: 0 }
    expect(
      recountTotals(totals, [
        { status: 'armed' },
        { status: 'stopped' },
        { status: 'triggered' },
      ] as Array<{ status: TriggerStatus }>),
    ).toEqual({ count: 3, armed: 1, awaiting: 0, triggered: 1 })
  })
})

/* ── live-tester fixes (2026-10-05) ────────────────────────────────────── */

/** brk_41996d3c after its partial take-profit: the take-profit leg done, the bracket armed. */
function partialTp(market: Json = {}, patch: (raw: Json) => void = () => {}): Bracket {
  return bracketWith(
    'bracket-armed',
    {
      priceUsd: 1.000017,
      upsidePct: null,
      downsidePct: -90.0001699971,
      rewardRisk: null,
      nearest: 'sl',
      checkedAt: new Date(NOW - 12_000).toISOString(),
      ...market,
    },
    {
      takeProfitUsd: 0.5,
      stopLossUsd: 0.1,
      takeProfitLabel: 'over $0.50',
      stopLossLabel: 'under $0.10',
      fromPriceUsd: null,
    },
    (raw) => {
      raw.status = 'armed'
      raw.fired = 'tp'
      raw.statusReason = 'take-profit filled · stop-loss guards the rest'
      const tp = leg(raw, 'takeProfit')
      tp.status = 'done'
      tp.statusReason = 'sold 0.000298 USDC at $1'
      ;(tp.condition as Json).hits = 0
      ;(leg(raw, 'stopLoss').condition as Json).hits = 0
      patch(raw)
    },
  )
}

describe('a partial take-profit', () => {
  it('derives no distance, no nearest and no reward : risk for the leg that is over', () => {
    const b = partialTp()
    expect(isBracketLegLive(b, 'tp')).toBe(false)
    expect(isBracketLegLive(b, 'sl')).toBe(true)
    // The price is over the filled take-profit line: still not "0 % to take-profit".
    expect(bracketUpsidePct(b)).toBeNull()
    expect(bracketUpsidePct(partialTp({ upsidePct: 0 }))).toBeNull()
    expect(bracketDownsidePct(b)).toBeCloseTo(-90, 3)
    expect(bracketNearest(b)).toBe('sl')
    // An engine that still names the dead leg, or none, cannot point at it.
    expect(bracketNearest(partialTp({ nearest: 'tp' }))).toBe('sl')
    expect(bracketNearest(partialTp({ nearest: null }))).toBe('sl')
    expect(bracketRewardRisk(b)).toBeNull()
    expect(bracketRewardRisk(partialTp({ rewardRisk: 2 }))).toBeNull()
    expect(bracketRewardRiskText(b)).toBe('—')
    // Neither leg live: no nearest at all.
    const over = partialTp({}, (raw) => {
      leg(raw, 'stopLoss').status = 'stopped'
    })
    expect(bracketNearest(over)).toBeNull()
    expect(bracketDownsidePct(over)).toBeNull()
  })

  it('reads "— / −90.0 %" on the list row and says what happened on the live line', () => {
    const b = partialTp()
    expect(bracketRowNowText(b)).toBe(`${b.token.symbol} $1.00 · — / −90.0 %`)
    expect(bracketLegOverText(b, 'tp')).toBe('take-profit filled')
    expect(bracketLegOverText(b, 'sl')).toBe('')
    expect(bracketNowText(b, NOW)).toBe(
      `${b.token.symbol} $1.00 · take-profit filled · −90.0 % to stop · checked 12 s ago`,
    )
    expect(bracketNowText(b, NOW)).not.toMatch(/at the take-profit|to take-profit/)
    const stopped = partialTp({}, (raw) => {
      leg(raw, 'takeProfit').status = 'stopped'
    })
    expect(bracketLegOverText(stopped, 'tp')).toBe('take-profit stopped')
  })

  it('stamps the nearest only for the live leg, on the card and on its row', () => {
    const b = partialTp({ nearest: 'tp' })
    const p: BracketOnePayload = { ...one('bracket-armed'), bracket: b }
    const card = render(p)
    expect(card.dataset.triggerNearest).toBe('sl')
    expect(card.querySelector('[data-trigger-fact="rr"] .trigger-fact__value')).toHaveTextContent(
      '—',
    )
    expect(Number(card.querySelector<HTMLElement>('.trigger-gauge')!.dataset.triggerDist)).toBe(-90)
    const list: BracketListPayload = {
      ...payload<BracketListPayload>(fixture('brackets')),
      brackets: [b],
    }
    const row = render(list).querySelector<HTMLElement>('.trigger-row')!
    expect(row.dataset.triggerNearest).toBe('sl')
    expect(row.querySelector('.trigger-row__now')).toHaveTextContent('— / −90.0 %')
  })
})

describe('a range alert bracket', () => {
  it('names its legs the ceiling and the floor, never a take-profit or a stop', () => {
    const p = one('bracket-alert')
    const card = render(p)
    expect(texts(card, '.bracket-leg__word')).toEqual(['Ceiling', 'Floor'])
    const { lines } = p.bracket
    const price = (n: number | null): string => `$${n!.toLocaleString('en-US')}`
    expect(card.querySelector('.trigger-gauge__label[data-leg="sl"]')).toHaveTextContent(
      `floor ${price(lines.stopLossUsd)}`,
    )
    expect(card.querySelector('.trigger-gauge__label[data-leg="tp"]')).toHaveTextContent(
      `ceiling ${price(lines.takeProfitUsd)}`,
    )
    expect(card.querySelector('.trigger-gauge')!.getAttribute('aria-label')).toMatch(
      /; floor \$[\d,.]+; ceiling \$[\d,.]+$/,
    )
    expect(card.querySelector('.trigger-card__now')?.textContent).toMatch(
      /^\S+ \$[\d,.]+ · [+−][\d.]+ % to ceiling · [+−][\d.]+ % to floor/,
    )
    expect(card.textContent).not.toMatch(/take[- ]profit|stop-loss|\bstop\b/i)
    expect(bracketLegWord('tp', 'alert')).toBe('ceiling')
    expect(bracketLegWord('sl', 'alert')).toBe('floor')
    expect(bracketLegWord('tp')).toBe('take-profit')
  })

  it('ends at the leg that alerted, its fires prefixed ceiling / floor', () => {
    const done = payload<BracketOnePayload>(
      withBracket('bracket-alert', (b) => {
        b.status = 'done'
        b.fired = 'tp'
        b.statusReason = 'ceiling: alerted at $1'
        b.updatedAt = '2026-12-31T00:00:00Z'
        const tp = leg(b, 'takeProfit')
        tp.status = 'done'
        tp.fires = [
          { n: 1, at: '2026-10-05T09:00:00Z', status: 'alerted', manual: false, priceUsd: 1 },
        ]
        const sl = leg(b, 'stopLoss')
        sl.status = 'stopped'
        sl.statusReason = 'range left over the top'
      }),
    )
    expect(bracketNowText(done.bracket, NOW)).toBe('done · ceiling: alerted at $1.00')
    const card = render(done)
    expect(texts(card, '.trigger-fire__n')).toEqual(['ceiling #1'])
    expect(card.textContent).not.toMatch(/take[- ]profit|stop-loss/i)
    // Confirming and firing speak the same words.
    const confirming = bracketWith('bracket-alert', {}, {}, (raw) => {
      const tp = leg(raw, 'takeProfit')
      tp.condition = { ...(tp.condition as Json), hits: 1, confirmTicks: 2 }
      ;(leg(raw, 'stopLoss').condition as Json).hits = 0
    })
    expect(bracketNowText(confirming, NOW)).toContain('ceiling fires after 1 more check')
    const firing = payload<BracketOnePayload>(
      withBracket('bracket-alert', (b) => {
        b.status = 'triggered'
        leg(b, 'stopLoss').status = 'triggered'
      }),
    ).bracket
    expect(bracketNowText(firing, NOW)).toBe('on hold · floor firing')
  })
})

describe('the facts never overlap', () => {
  const sizeCell = (card: HTMLElement): HTMLElement =>
    card.querySelector<HTMLElement>('[data-trigger-fact="size"]')!

  it('asks for two columns when a value is long, one when it is short', () => {
    const split = payload<BracketOnePayload>(
      withBracket('bracket-armed', (b) => {
        b.action = { ...(b.action as Json), amountPct: 1, tpPct: 0.5, amount: null }
      }),
    )
    const card = render(split)
    expect(sizeCell(card).dataset.triggerFactSpan).toBe('2')
    expect(sizeCell(card).querySelector('.trigger-fact__value')).toHaveTextContent(
      '0.5 % at take-profit, 1 % at stop',
    )
    const plain = payload<BracketOnePayload>(
      withBracket('bracket-armed', (b) => {
        b.action = { ...(b.action as Json), amountPct: 100, tpPct: null, estimatedUsd: 189 }
      }),
    )
    const short = sizeCell(render(plain))
    expect(short.dataset.triggerFactSpan).toBeUndefined()
    // The "≈ $…" is its own line; the value still reads as one phrase.
    const value = short.querySelector<HTMLElement>('.trigger-fact__value')!
    expect(value.textContent).toBe('100 % · ≈ $189')
    expect(value.querySelector('.trigger-fact__sub')).toHaveTextContent('≈ $189')
    expect(value.querySelector('.trigger-sep')).toHaveAttribute('aria-hidden', 'true')
    expect(value.firstChild?.textContent).toBe('100 %')
  })

  it('splits a fill into what moved and its dollars', () => {
    const done = payload<BracketOnePayload>(
      withBracket('bracket-done', (b) => {
        b.result = {
          ...(b.result as Json),
          amountIn: { raw: '105', human: '0.00000105', usd: 0.00286 },
          amountOut: { raw: '2851', human: '0.002851', usd: 0.00285 },
        }
      }),
    )
    const cell = sizeCell(render(done))
    const value = cell.querySelector<HTMLElement>('.trigger-fact__value')!
    const main = value.firstChild?.textContent ?? ''
    expect(main).toMatch(/ → 0\.002851 \S+$/)
    expect([...main].length).toBeGreaterThan(TRIGGER_FACT_WIDE_CHARS)
    expect(cell.dataset.triggerFactSpan).toBe('2')
    expect(value.querySelector('.trigger-fact__sub')).toHaveTextContent('≈ $0.00286')
    expect(value.textContent).toBe(bracketSizeText(done.bracket))
  })

  it('says what a fire does under "on fire", not "approval"', () => {
    const waits = render(
      payload<BracketOnePayload>(
        withBracket('bracket-armed', (b) => {
          b.action = { ...(b.action as Json), needsApproval: true, approvalThresholdUsd: 100 }
        }),
      ),
    )
    const cell = waits.querySelector<HTMLElement>('[data-trigger-fact="approval"]')!
    expect(cell.querySelector('.trigger-fact__label')).toHaveTextContent('on fire')
    expect(cell.querySelector('.trigger-fact__value')).toHaveTextContent(
      'waits for you · over $100',
    )
    expect(cell.dataset.triggerWaits).toBe('true')
    expect(cell.dataset.triggerFactSpan).toBe('2')
    const auto = render(
      payload<BracketOnePayload>(
        withBracket('bracket-armed', (b) => {
          b.action = { ...(b.action as Json), needsApproval: false }
        }),
      ),
    )
    const autoCell = auto.querySelector<HTMLElement>('[data-trigger-fact="approval"]')!
    expect(autoCell.querySelector('.trigger-fact__value')).toHaveTextContent('trades at once')
    expect(autoCell.dataset.triggerFactSpan).toBeUndefined()
    expect(auto.textContent).not.toMatch(/automatic/)
    // An alert places no order: no on-fire fact (its size already says "notify only").
    const alert = render(one('bracket-alert'))
    expect(alert.querySelector('[data-trigger-fact="approval"]')).toBeNull()
    expect(alert.querySelector('.trigger-card__facts')?.textContent).not.toMatch(/on fire/)
  })
})

/* ── events ────────────────────────────────────────────────────────────── */

describe('bracket events', () => {
  it('swaps a changed bracket into every card that draws it', async () => {
    const armedId = idOf('bracket-armed')
    const listed = (fixture('brackets').brackets as Json[]).some((b) => b.id === armedId)
    const card = placeholder('/a')
    const listHost = placeholder('/b')
    const other = placeholder('/c')
    const mounter = createTriggerMounter({
      fetchPayload: (url) =>
        Promise.resolve(
          fixture(url === '/a' ? 'bracket-armed' : url === '/b' ? 'brackets' : 'bracket-done'),
        ),
    })
    mounter.mountTrigger(document.body)
    await flush()
    // The engine's event body: the full Bracket under `bracket`.
    const paused = inStatus('bracket-armed', 'paused').bracket as Json
    mounter.bracketChanged({ bracket: paused })
    expect(card.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'paused')
    if (listed) {
      expect(listHost.querySelector(`.trigger-row[data-bracket-id="${armedId}"]`)).toHaveAttribute(
        'data-trigger-status',
        'paused',
      )
    }
    expect(other.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'done')
    // The full payload works too; junk is ignored.
    mounter.bracketChanged(inStatus('bracket-armed', 'stopped'))
    expect(card.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'stopped')
    mounter.bracketChanged({ nothing: true })
    mounter.bracketChanged(null)
    // A trigger event never touches a bracket card.
    mounter.triggerChanged({ trigger: (fixture('bracket-armed').bracket as Json).takeProfit })
    expect(card.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'stopped')
    mounter.destroyAll()
  })

  it('reads the three event body shapes', () => {
    const full = inStatus('bracket-armed', 'paused')
    expect(bracketChangedPayload(full)?.payload?.bracket.status).toBe('paused')
    expect(bracketChangedPayload({ bracket: full })?.payload?.request?.scope).toBe('bracket')
    const bare = bracketChangedPayload({ bracket: full.bracket })
    expect(bare?.bracket.id).toBe(idOf('bracket-armed'))
    expect(bare?.payload).toBeNull()
    expect(bracketChangedPayload({ bracket: 'x' })).toBeNull()
  })

  it('re-reads a bracket card when its leg’s order settles', async () => {
    const read = vi.fn(() => Promise.resolve(fixture('bracket-armed')))
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('bracket-armed')),
      call: read,
    })
    mounter.mountTrigger(document.body)
    await flush()
    expect(read).toHaveBeenCalledTimes(1)
    read.mockClear()
    mounter.orderFinished('ord_unrelated', 'trg_unrelated')
    expect(read).not.toHaveBeenCalled()
    // The order names the stop-loss leg; the card maps it to its bracket.
    const slId = ((fixture('bracket-armed').bracket as Json).stopLoss as Json).id as string
    mounter.orderFinished('ord_new', slId)
    await flush()
    expect(read).toHaveBeenCalledWith('trading.bracket.get', { bracketId: idOf('bracket-armed') })
    expect(host.querySelector('.trigger-card')).not.toBeNull()
    mounter.destroyAll()
  })

  it('draws the snapshot with its controls off until the live read lands', async () => {
    let resolve: (v: unknown) => void = () => {}
    const read = vi.fn(() => new Promise((r) => (resolve = r)))
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('bracket-awaiting')),
      call: read,
      actions: { call: vi.fn() },
      now: () => NOW,
    })
    mounter.mountTrigger(document.body)
    await flush()
    expect(read).toHaveBeenCalledWith('trading.bracket.get', {
      bracketId: idOf('bracket-awaiting'),
    })
    const snapshot = host.querySelector<HTMLElement>('.trigger-card')!
    expect(snapshot.dataset.triggerStale).toBe('checking')
    expect(button(host, 'approve').disabled).toBe(true)
    resolve(inStatus('bracket-awaiting', 'rejected'))
    await flush()
    const card = host.querySelector<HTMLElement>('.trigger-card')!
    expect(card.dataset.triggerStatus).toBe('rejected')
    expect(card).not.toHaveAttribute('data-trigger-stale')
    expect(card.querySelector('.trigger-actions')).toBeNull()

    // ↻ re-runs trading.bracket.get.
    read.mockImplementation(() =>
      Promise.resolve(
        inStatus('bracket-awaiting', 'expired', { updatedAt: '2027-03-01T00:00:00Z' }),
      ),
    )
    host.querySelector<HTMLButtonElement>('[data-trigger-foot="refresh"]')!.click()
    await flush()
    expect(read).toHaveBeenLastCalledWith('trading.bracket.get', {
      bracketId: idOf('bracket-awaiting'),
    })
    expect(host.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'expired')
    mounter.destroyAll()
  })

  it('ticks the live line of a bracket card', async () => {
    vi.useFakeTimers()
    let clock = NOW
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () =>
        Promise.resolve(
          withBracket('bracket-armed', (b) => {
            b.status = 'armed'
            b.market = {
              ...(b.market as Json),
              checkedAt: new Date(NOW - 12_000).toISOString(),
              priceUsd: 2000,
            }
            for (const w of ['takeProfit', 'stopLoss'] as const) {
              ;(leg(b, w).condition as Json).hits = 0
            }
          }),
        ),
      now: () => clock,
    })
    mounter.mountTrigger(document.body)
    await flush()
    const line = (): string => host.querySelector('.trigger-card__now')!.textContent ?? ''
    expect(line()).toMatch(/checked 12 s ago$/)
    clock += 1_000
    vi.advanceTimersByTime(1_000)
    expect(line()).toMatch(/checked 13 s ago$/)
    mounter.destroyAll()
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('unbreakable', () => {
  it('joins a countdown with non-breaking spaces so "m" never wraps alone', () => {
    expect(unbreakable('23 h 53 m')).toBe('23\u00a0h\u00a053\u00a0m')
  })
})
