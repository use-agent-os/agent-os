import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  DCA_ARTIFACT_MIME,
  DCA_CHART_MIN_PAD,
  DCA_CONFIRM_MS,
  DCA_COPIED_MS,
  DCA_MINUTE_MS,
  DCA_SECOND_MS,
  DCA_WIDE_MIN_PX,
  buildDcaCard,
  buildDcaChartModel,
  createDcaMounter,
  dcaActionsFor,
  dcaAxisLabels,
  dcaChangedPayload,
  dcaErrorText,
  dcaLayoutFor,
  dcaProgress,
  dcaReadMethod,
  dcaTickDelay,
  dcaYAxisLabels,
  everyLabel,
  formatCountdown,
  formatDcaPrice,
  formatDcaUsd,
  formatSignedUsd,
  formatVsPct,
  isDcaArtifact,
  layoutDcaCard,
  nextBuyText,
  normalizeDcaPayload,
  normalizeDcaRequest,
  statusReasonText,
  type DcaChartModel,
  type DcaMandate,
  type DcaMandatePayload,
  type DcaMandatesPayload,
  type DcaPayload,
  type DcaRenderContext,
  type DcaRun,
  type DcaStatus,
} from './dca'
import { LP_ARTIFACT_MIME } from './lp'

const FIXTURES = 'src/views/chat/transcript/__fixtures__/dca'
// The engine writes its own payloads here (tests/fixtures/dca_cards); when they
// exist the renderer must read them too, so the two sides cannot drift.
const ENGINE_FIXTURES = '../tests/fixtures/dca_cards'

type Json = Record<string, unknown>

function fixture(name: string): Json {
  return JSON.parse(readFileSync(`${FIXTURES}/${name}.json`, 'utf8')) as Json
}

function payload<T extends DcaPayload>(name: string): T {
  const normalized = normalizeDcaPayload(fixture(name))
  if (!normalized) throw new Error(`fixture ${name} did not normalize`)
  return normalized as T
}

/** The active fixture's raw body with its mandate patched. */
function activeWith(patch: (mandate: Json) => void): Json {
  const raw = fixture('mandate-active')
  patch(raw.mandate as Json)
  return raw
}

function mandatePayloadFor(status: DcaStatus, extra: Json = {}): Json {
  return activeWith((m) => {
    m.status = status
    Object.assign(m, extra)
  })
}

// fetchedAt of every fixture; the active mandate's next buy is 09:00 that day.
const FETCHED_AT = Date.parse('2026-09-28T05:48:00Z')
const NEXT_RUN = Date.parse('2026-09-28T09:00:00Z')

function ctx(overrides: Partial<DcaRenderContext> = {}): DcaRenderContext {
  return {
    now: () => FETCHED_AT + 2 * 60_000,
    copyText: vi.fn(),
    setTimer: (fn, ms) => void setTimeout(fn, ms),
    ...overrides,
  }
}

function render(p: DcaPayload, context = ctx()): HTMLElement {
  const card = buildDcaCard(p, context)
  document.body.append(card)
  return card
}

function placeholder(src = '/api/v1/artifacts/dca-1'): HTMLElement {
  const node = document.createElement('div')
  node.className = 'msg-artifact-dca'
  node.dataset.dcaSrc = src
  node.innerHTML = `<div class="msg-artifact-dca__body"></div>
    <p class="msg-artifact-dca__status">Loading DCA mandate…</p>`
  document.body.append(node)
  return node
}

async function flush(): Promise<void> {
  for (let i = 0; i < 6; i++) await Promise.resolve()
}

function deferred<T>(): {
  promise: Promise<T>
  resolve: (v: T) => void
  reject: (e: unknown) => void
} {
  let resolve: (v: T) => void = () => {}
  let reject: (e: unknown) => void = () => {}
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function texts(root: ParentNode, selector: string): string[] {
  return [...root.querySelectorAll(selector)].map((n) => n.textContent ?? '')
}

beforeEach(() => {
  document.body.replaceChildren()
})

afterEach(() => {
  vi.useRealTimers()
})

/* ── mime + normalization ──────────────────────────────────────────────── */

describe('isDcaArtifact', () => {
  it('matches the dca mime, with or without parameters, in any case', () => {
    expect(isDcaArtifact({ mime: DCA_ARTIFACT_MIME })).toBe(true)
    expect(isDcaArtifact({ mime: `${DCA_ARTIFACT_MIME}; charset=utf-8` })).toBe(true)
    expect(isDcaArtifact({ mime: 'APPLICATION/VND.AGENTOS.DCA+JSON' })).toBe(true)
  })

  it('rejects everything else', () => {
    expect(isDcaArtifact({ mime: LP_ARTIFACT_MIME })).toBe(false)
    expect(isDcaArtifact({ mime: 'application/json' })).toBe(false)
    expect(isDcaArtifact({})).toBe(false)
    expect(isDcaArtifact(null)).toBe(false)
  })
})

describe('normalizeDcaPayload', () => {
  it('reads every fixture', () => {
    for (const name of [
      'mandate-active',
      'mandate-awaiting',
      'mandate-completed',
      'mandates',
      'mandates-empty',
    ]) {
      expect(normalizeDcaPayload(fixture(name)), name).not.toBeNull()
    }
  })

  it('keeps the active mandate figures as the engine sent them', () => {
    const { mandate, request, warnings } = payload<DcaMandatePayload>('mandate-active')
    expect(mandate.id).toBe('dca_1a2b3c4d')
    expect(mandate.status).toBe('active')
    expect(mandate.token.symbol).toBe('ETH')
    expect(mandate.quote.symbol).toBe('USDC')
    expect(mandate.chain).toMatchObject({ id: 8453, key: 'base', explorer: 'https://basescan.org' })
    expect(mandate.budget).toEqual({
      usdPerRun: 10,
      capUsd: 300,
      spentUsd: 120,
      reservedUsd: 10,
      remainingUsd: 170,
      progress: 0.4,
    })
    expect(mandate.runs).toEqual({ done: 12, max: 30, skipped: 1, failed: 0, attempts: 14 })
    expect(mandate.history).toHaveLength(14)
    expect(mandate.history[0]).toMatchObject({ n: 14, status: 'parked', manual: true })
    expect(request).toEqual({ kind: 'get', params: { mandateId: 'dca_1a2b3c4d' } })
    expect(warnings).toEqual(['USDC balance covers 17 more buys'])
  })

  it('reads numbers sent as strings', () => {
    const raw = activeWith((m) => {
      const budget = m.budget as Json
      budget.usdPerRun = '10'
      budget.capUsd = '300.00'
      budget.spentUsd = '120.4'
      ;(m.runs as Json).done = '12'
      ;(m.acquired as Json).avgPriceUsd = '2860'
    })
    const p = normalizeDcaPayload(raw) as DcaMandatePayload
    expect(p.mandate.budget.usdPerRun).toBe(10)
    expect(p.mandate.budget.capUsd).toBe(300)
    expect(p.mandate.budget.spentUsd).toBe(120.4)
    expect(p.mandate.runs.done).toBe(12)
    expect(p.mandate.acquired.avgPriceUsd).toBe(2860)
  })

  it('treats a null USD as unknown, never as zero', () => {
    const raw = activeWith((m) => {
      const acquired = m.acquired as Json
      acquired.avgPriceUsd = null
      acquired.unrealizedUsd = null
      acquired.vsAvgPct = null
      ;(acquired.amount as Json).usd = null
    })
    const p = normalizeDcaPayload(raw) as DcaMandatePayload
    expect(p.mandate.acquired.avgPriceUsd).toBeNull()
    expect(p.mandate.acquired.unrealizedUsd).toBeNull()
    expect(p.mandate.acquired.amount.usd).toBeNull()
    const card = render(p)
    const unrealized = card.querySelector('[data-dca-stat="unrealized"] .dca-stat__value')!
    expect(unrealized).toHaveTextContent('—')
    expect(unrealized).toHaveAttribute('data-dca-no-price', 'true')
    expect(card.querySelector('[data-dca-stat="avg"] .dca-stat__value')).toHaveTextContent('—')
  })

  it('tolerates a mandate with only its core fields', () => {
    const p = normalizeDcaPayload({
      version: 1,
      kind: 'mandate',
      mandate: {
        id: 'dca_00000001',
        status: 'active',
        token: { symbol: 'ETH' },
        budget: { usdPerRun: 5 },
        runs: { max: 10 },
      },
    }) as DcaMandatePayload
    expect(p).not.toBeNull()
    expect(p.mandate.name).toBe('DCA ETH')
    // The cap defaults to usdPerRun × runsMax, as the engine does.
    expect(p.mandate.budget.capUsd).toBe(50)
    expect(p.mandate.history).toEqual([])
    expect(p.mandate.schedule.startNow).toBe(true)
    expect(p.request).toBeNull()
    expect(p.run).toBeNull()
    // …and still draws: the chart becomes a hint, the footer has no ↻.
    const card = render(p, ctx({ canRefresh: true }))
    expect(card.querySelector('.dca-chart--empty')).not.toBeNull()
    expect(card.querySelector('[data-dca-foot="refresh"]')).toBeNull()
  })

  it('returns null when there is nothing to draw', () => {
    expect(normalizeDcaPayload(null)).toBeNull()
    expect(normalizeDcaPayload({ kind: 'nope' })).toBeNull()
    expect(normalizeDcaPayload({ kind: 'mandate', mandate: { token: { symbol: 'X' } } })).toBeNull()
    expect(normalizeDcaPayload({ kind: 'mandate', mandate: { id: 'dca_1' } })).toBeNull()
    expect(normalizeDcaPayload({ kind: 'mandates' })).toBeNull()
  })

  it('maps an unknown status to "unknown" and drops unusable rows', () => {
    const raw = activeWith((m) => {
      m.status = 'exploded'
      ;(m.history as unknown[]).push('garbage', null)
    })
    const p = normalizeDcaPayload(raw) as DcaMandatePayload
    expect(p.mandate.status).toBe('unknown')
    expect(p.mandate.history).toHaveLength(14)
  })

  it('never keeps a non-http explorer link', () => {
    const raw = activeWith((m) => {
      const run = (m.history as Json[])[1]!
      run.txHash = 'not-a-hash'
      run.explorerUrl = 'javascript:alert(1)'
      const other = (m.history as Json[])[2]!
      other.txHash = null
      other.explorerUrl = 'https://basescan.org/tx/0xabc'
    })
    const p = normalizeDcaPayload(raw) as DcaMandatePayload
    expect(p.mandate.history[1]!.explorerUrl).toBe('')
    expect(p.mandate.history[2]!.explorerUrl).toBe('https://basescan.org/tx/0xabc')
    // A good hash builds the link from the chain's explorer.
    expect(p.mandate.history[3]!.explorerUrl).toMatch(
      /^https:\/\/basescan\.org\/tx\/0x[0-9a-f]{64}$/,
    )
  })

  it('only accepts get and list as the request to re-run', () => {
    expect(normalizeDcaRequest({ kind: 'get', params: { mandateId: 'x' } })).toEqual({
      kind: 'get',
      params: { mandateId: 'x' },
    })
    expect(normalizeDcaRequest({ kind: 'list' })).toEqual({ kind: 'list', params: {} })
    expect(normalizeDcaRequest({ kind: 'stop', params: {} })).toBeNull()
    expect(dcaReadMethod({ kind: 'list', params: {} })).toBe('trading.dca.list')
  })

  it('reads the run a "Buy now" answered with', () => {
    const raw = fixture('mandate-active')
    raw.run = { n: 15, at: '2026-09-28T05:50:00Z', status: 'parked', orderId: 'ord_new', usd: 10 }
    const p = normalizeDcaPayload(raw) as DcaMandatePayload
    expect(p.run).toMatchObject({ n: 15, status: 'parked', orderId: 'ord_new' })
  })
})

/* ── formatting ────────────────────────────────────────────────────────── */

describe('formatting', () => {
  it('prints budget USD without cents when whole', () => {
    expect(formatDcaUsd(10)).toBe('$10')
    expect(formatDcaUsd(300)).toBe('$300')
    expect(formatDcaUsd(120.4)).toBe('$120.40')
    expect(formatDcaUsd(0.05)).toBe('$0.05')
    expect(formatDcaUsd(0)).toBe('$0')
    expect(formatDcaUsd(null)).toBe('—')
  })

  it('prints prices the way a trader reads them', () => {
    expect(formatDcaPrice(2860.4)).toBe('$2,860')
    expect(formatDcaPrice(1.2345)).toBe('$1.23')
    expect(formatDcaPrice(0.00000123)).toBe('$0.0₅123')
    expect(formatDcaPrice(null)).toBe('—')
  })

  it('signs deltas and percentages', () => {
    expect(formatSignedUsd(10.8)).toBe('+$10.80')
    expect(formatSignedUsd(-4.2)).toBe('−$4.20')
    expect(formatSignedUsd(null)).toBe('—')
    expect(formatVsPct(4.21)).toBe('▲ 4.2 %')
    expect(formatVsPct(-1.1)).toBe('▼ 1.1 %')
  })

  it('names the schedule from its interval', () => {
    expect(everyLabel(86_400)).toBe('every day')
    expect(everyLabel(21_600)).toBe('every 6 hours')
    expect(everyLabel(3600)).toBe('every hour')
    expect(everyLabel(1800)).toBe('every 30 minutes')
    expect(everyLabel(604_800)).toBe('every week')
    expect(everyLabel(172_800)).toBe('every 2 days')
    expect(everyLabel(90)).toBe('every 90 s')
  })
})

/* ── countdown ─────────────────────────────────────────────────────────── */

describe('countdown', () => {
  it('formats a span at the right grain', () => {
    expect(formatCountdown(3 * 3_600_000 + 12 * 60_000 + 30_000)).toBe('3 h 12 m')
    expect(formatCountdown(12 * 60_000 + 5_000)).toBe('12 m 5 s')
    expect(formatCountdown(42_000)).toBe('42 s')
    expect(formatCountdown(28 * 3_600_000)).toBe('1 d 4 h')
    // Trimmed: a whole span drops its zero second part.
    expect(formatCountdown(3_600_000)).toBe('1 h 0 m')
    expect(formatCountdown(3_600_000, true)).toBe('1 h')
    expect(formatCountdown(120_000, true)).toBe('2 m')
    expect(formatCountdown(86_400_000, true)).toBe('1 d')
    expect(formatCountdown(90_000, true)).toBe('1 m 30 s')
  })

  it('says when the next buy is, or why there is none', () => {
    const active = payload<DcaMandatePayload>('mandate-active').mandate
    expect(nextBuyText(active, FETCHED_AT)).toBe('next buy in 3 h 12 m')
    expect(nextBuyText(active, NEXT_RUN + 1000)).toBe('buy now due')
    const awaiting = payload<DcaMandatePayload>('mandate-awaiting').mandate
    expect(nextBuyText(awaiting, FETCHED_AT)).toBe(
      'first buy on approval · proposal expires in 23 h 43 m',
    )
    expect(
      nextBuyText({ ...awaiting, schedule: { ...awaiting.schedule, startNow: false } }, FETCHED_AT),
    ).toBe('first buy after 6 h · proposal expires in 23 h 43 m')
    expect(
      nextBuyText(
        { ...awaiting, schedule: { ...awaiting.schedule, startNow: false, everySeconds: 120 } },
        FETCHED_AT,
      ),
    ).toMatch(/^first buy after 2 m · /)
    const completed = payload<DcaMandatePayload>('mandate-completed').mandate
    expect(nextBuyText(completed, FETCHED_AT)).toBe('completed · no more buys')
    expect(nextBuyText({ ...active, status: 'paused' }, FETCHED_AT)).toBe(
      'paused · no buys until resumed',
    )
  })

  it('ticks every second under an hour and every minute above', () => {
    expect(dcaTickDelay(30 * 60_000)).toBe(DCA_SECOND_MS)
    expect(dcaTickDelay(3 * 3_600_000)).toBe(DCA_MINUTE_MS)
    // Wakes in time to switch to seconds when the hour mark is close.
    expect(dcaTickDelay(3_600_000 + 20_000)).toBe(20_000)
    expect(dcaTickDelay(null)).toBe(DCA_MINUTE_MS)
    expect(dcaTickDelay(-5)).toBe(DCA_MINUTE_MS)
  })

  it('counts down live in a mounted card and stops on destroyAll', async () => {
    vi.useFakeTimers()
    let now = NEXT_RUN - (2 * 60_000 + 3_000)
    const host = placeholder()
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-active')),
      now: () => now,
    })
    mounter.mountDca(document.body)
    await flush()
    const next = host.querySelector('.dca-card__next')!
    expect(next).toHaveTextContent('next buy in 2 m 3 s')
    expect(next).toHaveAttribute('data-dca-next-at', '2026-09-28T09:00:00Z')
    expect(vi.getTimerCount()).toBe(1)

    now += 1000
    vi.advanceTimersByTime(DCA_SECOND_MS)
    expect(next).toHaveTextContent('next buy in 2 m 2 s')

    now += 3 * 60_000
    vi.advanceTimersByTime(DCA_SECOND_MS)
    expect(next).toHaveTextContent('buy now due')
    expect(next).toHaveAttribute('data-dca-due', 'true')

    mounter.destroyAll()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('refreshes only once a minute while the next buy is hours away', async () => {
    vi.useFakeTimers()
    let now = FETCHED_AT
    const host = placeholder()
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-active')),
      now: () => now,
    })
    mounter.mountDca(document.body)
    await flush()
    const next = host.querySelector('.dca-card__next')!
    expect(next).toHaveTextContent('next buy in 3 h 12 m')
    now += 30_000
    vi.advanceTimersByTime(DCA_SECOND_MS * 30)
    expect(next).toHaveTextContent('next buy in 3 h 12 m')
    now += 60_000
    vi.advanceTimersByTime(DCA_MINUTE_MS)
    expect(next).toHaveTextContent('next buy in 3 h 10 m')
    expect(host.querySelector('.dca-card__ago')).toHaveTextContent('1m ago')
    mounter.destroyAll()
  })

  it('stops the clock once every card has left the document', async () => {
    vi.useFakeTimers()
    const host = placeholder()
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-completed')),
    })
    mounter.mountDca(document.body)
    await flush()
    expect(vi.getTimerCount()).toBe(1)
    host.remove()
    vi.advanceTimersByTime(DCA_MINUTE_MS)
    expect(vi.getTimerCount()).toBe(0)
  })
})

/* ── progress ──────────────────────────────────────────────────────────── */

describe('dcaProgress', () => {
  it('draws spent and the reserved slice against the cap', () => {
    const { mandate } = payload<DcaMandatePayload>('mandate-active')
    const model = dcaProgress(mandate)
    expect(model.spentPct).toBeCloseTo(40)
    expect(model.reservedPct).toBeCloseTo(10 / 3)
    expect(model.amountText).toBe('$120 of $300 · 40 %')
    expect(model.runsText).toBe('12 of 30 buys')
  })

  it('counts buys without a maximum, singular and plural', () => {
    const { mandate } = payload<DcaMandatePayload>('mandate-completed')
    expect(dcaProgress(mandate).runsText).toBe('5 buys')
    expect(dcaProgress({ ...mandate, runs: { ...mandate.runs, done: 1 } }).runsText).toBe('1 buy')
    expect(dcaProgress(mandate).amountText).toBe('$100 of $100 · 100 %')
  })

  it('never lets spent + reserved run past the end of the bar', () => {
    const { mandate } = payload<DcaMandatePayload>('mandate-active')
    const m: DcaMandate = {
      ...mandate,
      budget: { ...mandate.budget, spentUsd: 295, reservedUsd: 10 },
    }
    const model = dcaProgress(m)
    expect(model.spentPct + model.reservedPct).toBeCloseTo(100)
  })

  it('renders the bar with an accessible value and a hatched slice', () => {
    const card = render(payload('mandate-active'))
    const bar = card.querySelector<HTMLElement>('.dca-card__progress .dca-progress')!
    expect(bar).toHaveAttribute('role', 'progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '40')
    expect(bar.querySelector<HTMLElement>('.dca-progress__spent')!.style.width).toBe('40%')
    expect(bar.querySelector('.dca-progress__reserved')).not.toBeNull()
    expect(card.querySelector('.dca-progress__amount')).toHaveTextContent('$120 of $300 · 40 %')
    expect(card.querySelector('.dca-progress__runs')).toHaveTextContent('12 of 30 buys · 1 skipped')
    expect(card.querySelector('.dca-progress__reserved-note')).toHaveTextContent(
      '$10 reserved for a buy in flight',
    )
  })
})

/* ── layouts per status ────────────────────────────────────────────────── */

describe('buildDcaCard — mandate', () => {
  it('reads like an instrument: pair, pill, hero, next buy, stats', () => {
    const card = render(payload('mandate-active'))
    expect(card.tagName).toBe('ARTICLE')
    expect(card.dataset).toMatchObject({
      dcaKind: 'mandate',
      dcaStatus: 'active',
      dcaChain: 'base',
      dcaLayout: 'narrow',
      dcaId: 'dca_1a2b3c4d',
    })
    expect(card.querySelector('.dca-card__pair')).toHaveTextContent('ETH ← USDC')
    expect(card.querySelector('.dca-chain')).toHaveTextContent('Base')
    const pill = card.querySelector<HTMLElement>('.dca-pill')!
    expect(pill.dataset.status).toBe('active')
    expect(pill).toHaveTextContent('Active')
    expect(card.querySelector('.dca-card__name')).toHaveTextContent('DCA ETH')
    expect(card.querySelector('.dca-card__hero-line')).toHaveTextContent('$10 every day')
    expect(card.querySelector('.dca-card__next')).toHaveTextContent('next buy in 3 h 10 m')
    expect(card.querySelector('.dca-card__guards')).toHaveTextContent(
      'buys only at or under $3,100 · 1 % max slippage',
    )

    const stat = (key: string): HTMLElement =>
      card.querySelector<HTMLElement>(`[data-dca-stat="${key}"]`)!
    expect(stat('acquired').querySelector('.dca-stat__value')).toHaveTextContent('0.04159 ETH')
    expect(stat('acquired').querySelector('.dca-stat__sub')).toHaveTextContent('$123.94')
    expect(stat('avg').querySelector('.dca-stat__value')).toHaveTextContent('$2,885')
    const vs = stat('avg').querySelector<HTMLElement>('.dca-stat__sub')!
    expect(vs).toHaveTextContent('vs now ▲ 3.3 %')
    expect(vs.dataset.dcaTone).toBe('up')
    const unrealized = stat('unrealized').querySelector<HTMLElement>('.dca-stat__value')!
    expect(unrealized).toHaveTextContent('+$3.94')
    expect(unrealized.dataset.dcaTone).toBe('up')
    expect(stat('gas').querySelector('.dca-stat__value')).toHaveTextContent('$0.072')
    expect(stat('gas').querySelector('.dca-stat__sub')).toHaveTextContent('$0.006 / buy')

    expect(texts(card, '.dca-card__warning')).toEqual(['USDC balance covers 17 more buys'])
  })

  it('tints a loss down', () => {
    const raw = activeWith((m) => {
      Object.assign(m.acquired as Json, { vsAvgPct: -1.1, unrealizedUsd: -1.32 })
    })
    const card = render(normalizeDcaPayload(raw)!)
    const unrealized = card.querySelector<HTMLElement>(
      '[data-dca-stat="unrealized"] .dca-stat__value',
    )!
    expect(unrealized).toHaveTextContent('−$1.32')
    expect(unrealized.dataset.dcaTone).toBe('down')
    expect(card.querySelector('[data-dca-stat="avg"] .dca-stat__sub')).toHaveTextContent(
      'vs now ▼ 1.1 %',
    )
  })

  it('draws a pending proposal with its first-buy line and no chart yet', () => {
    const card = render(payload('mandate-awaiting'))
    expect(card.dataset.dcaStatus).toBe('awaiting_approval')
    expect(card.querySelector('.dca-pill')).toHaveTextContent('Awaiting approval')
    expect(card.querySelector('.dca-card__hero-line')).toHaveTextContent('$25 every 6 hours')
    expect(card.querySelector('.dca-card__next')).toHaveTextContent(
      /^first buy on approval · proposal expires in/,
    )
    expect(card.querySelector('.dca-card__next')).not.toHaveAttribute('data-dca-next-at')
    expect(card.querySelector('.dca-chart--empty')).toHaveTextContent(
      'The buys chart appears after the second buy.',
    )
    expect(card.querySelector('.dca-runs')).toBeNull()
    expect(card.querySelector('.dca-progress__runs')).toHaveTextContent('0 buys')
  })

  it('says "by you" for a reason the owner gave', () => {
    expect(statusReasonText('user')).toBe('by you')
    expect(statusReasonText('user: too volatile')).toBe('by you: too volatile')
    expect(statusReasonText('cap reached')).toBe('cap reached')
    expect(statusReasonText('username taken')).toBe('username taken')
    for (const status of ['stopped', 'rejected', 'paused'] as const) {
      const card = render(normalizeDcaPayload(mandatePayloadFor(status, { statusReason: 'user' }))!)
      expect(card.querySelector('.dca-card__reason')).toHaveTextContent(/^by you$/)
    }
    const noted = render(
      normalizeDcaPayload(mandatePayloadFor('stopped', { statusReason: 'user: enough ETH' }))!,
    )
    expect(noted.querySelector('.dca-card__reason')).toHaveTextContent(/^by you: enough ETH$/)
  })

  it('draws a completed mandate with its reason and no controls', () => {
    const card = render(payload('mandate-completed'), ctx({ canWrite: true }))
    expect(card.dataset.dcaStatus).toBe('completed')
    expect(card.querySelector('.dca-pill')).toHaveTextContent('Completed')
    expect(card.querySelector('.dca-card__reason')).toHaveTextContent('cap reached')
    expect(card.querySelector('.dca-card__next')).toHaveTextContent('completed · no more buys')
    expect(card.querySelector('.dca-card__pair')).toHaveTextContent('cbBTC ← USDC')
    expect(card.querySelector('.dca-actions')).toBeNull()
  })

  it.each<[DcaStatus, string]>([
    ['paused', 'Paused'],
    ['stopped', 'Stopped'],
    ['rejected', 'Rejected'],
    ['expired', 'Expired'],
  ])('labels a %s mandate', (status, word) => {
    const card = render(normalizeDcaPayload(mandatePayloadFor(status))!)
    expect(card.dataset.dcaStatus).toBe(status)
    expect(card.querySelector(`.dca-pill[data-status="${status}"]`)).toHaveTextContent(word)
  })

  it('lists the last five runs newest first with explorer links', () => {
    const card = render(payload('mandate-active'))
    const rows = [...card.querySelectorAll<HTMLElement>('.dca-run')]
    expect(rows).toHaveLength(5)
    expect(rows.map((r) => r.querySelector('.dca-run__n')!.textContent)).toEqual([
      '#14',
      '#13',
      '#12',
      '#11',
      '#10',
    ])
    expect(rows[0]!.dataset.dcaRunStatus).toBe('parked')
    expect(rows[0]!.querySelector('.dca-run__status')).toHaveTextContent(
      'awaiting approval · #ord_7f3a91…',
    )
    expect(rows[0]!.querySelector('.dca-run__manual')).toHaveTextContent('buy now')
    expect(rows[0]!.querySelector('.dca-run__link')).toBeNull()
    expect(rows[1]!.querySelector('.dca-run__status')).toHaveTextContent('filled')
    expect(rows[1]!.querySelector('.dca-run__detail')).toHaveTextContent(
      /^\$10 → 0\.003408 ETH @ \$2,935$/,
    )
    const link = rows[1]!.querySelector<HTMLAnchorElement>('.dca-run__link')!
    expect(link.href).toMatch(/^https:\/\/basescan\.org\/tx\/0x[0-9a-f]{64}$/)
    expect(link.target).toBe('_blank')
    expect(link.rel).toBe('noopener noreferrer')
    expect(rows[1]!.querySelector('.dca-run__ago')).toHaveTextContent('1d ago')
  })

  it('heads the list "Recent runs" when a listed run bought nothing, "Recent buys" otherwise', () => {
    const card = render(payload('mandate-active'))
    // #14 is parked and #13 skipped: not all of these are buys.
    expect(card.querySelector('.dca-runs__title')).toHaveTextContent(/^Recent runs$/)
    const raw = activeWith((m) => {
      m.history = (m.history as Json[]).filter((r) => r.status === 'filled')
    })
    const filled = render(normalizeDcaPayload(raw)!)
    expect(filled.querySelector('.dca-runs__title')).toHaveTextContent(/^Recent buys$/)
  })

  it('draws "—" for acquired, avg, vs now and unrealised before the first filled buy', () => {
    const raw = activeWith((m) => {
      ;(m.runs as Json).done = 0
      ;(m.budget as Json).spentUsd = 0
      Object.assign(m.acquired as Json, {
        avgPriceUsd: 0,
        vsAvgPct: 0,
        unrealizedUsd: 0,
        amount: { raw: '0', human: '0', usd: 0 },
        gasUsd: 0,
      })
      m.history = (m.history as Json[])
        .filter((r) => r.status === 'skipped')
        .map((r) => ({ ...r, n: 1 }))
    })
    const card = render(normalizeDcaPayload(raw)!)
    const value = (key: string) =>
      card.querySelector<HTMLElement>(`[data-dca-stat="${key}"] .dca-stat__value`)!
    // Not "0 ETH / $0.00".
    expect(value('acquired')).toHaveTextContent(/^—$/)
    expect(value('acquired')).toHaveAttribute('data-dca-no-price', 'true')
    expect(card.querySelector('[data-dca-stat="acquired"] .dca-stat__sub')).toBeNull()
    expect(value('avg')).toHaveTextContent(/^—$/)
    expect(value('avg')).toHaveAttribute('data-dca-no-price', 'true')
    expect(value('unrealized')).toHaveTextContent(/^—$/)
    expect(value('unrealized')).toHaveAttribute('data-dca-no-price', 'true')
    expect(value('unrealized').dataset.dcaTone).toBeUndefined()
    // Not "$0.00" gas either.
    expect(value('gas')).toHaveTextContent(/^—$/)
    expect(value('gas')).toHaveAttribute('data-dca-no-price', 'true')
    expect(card.querySelector('[data-dca-stat="gas"] .dca-stat__sub')).toBeNull()
    // No "vs now 0 %", no "on $0 spent".
    expect(card.querySelector('[data-dca-stat="avg"] .dca-stat__sub')).toBeNull()
    expect(card.querySelector('[data-dca-stat="unrealized"] .dca-stat__sub')).toBeNull()
    expect(card.querySelector('.dca-card__stats')).not.toHaveTextContent('on $0 spent')
    expect(card.querySelector('.dca-runs__title')).toHaveTextContent(/^Recent runs$/)
  })

  it('names a skipped run by its reason', () => {
    const raw = activeWith((m) => {
      m.history = (m.history as Json[]).slice(5)
    })
    const card = render(normalizeDcaPayload(raw)!)
    const skipped = card.querySelector<HTMLElement>('.dca-run[data-dca-run-status="skipped"]')!
    expect(skipped.querySelector('.dca-run__detail')).toHaveTextContent(
      'ETH at $3,148 above $3,100',
    )
  })

  it('writes the footer: id, wallet, as of', () => {
    const card = render(payload('mandate-active'))
    expect(card.querySelector('.dca-card__foot-meta')).toHaveTextContent(
      'dca_1a2b3c4d · Key main (0x89e0…da97) · as of 2m ago',
    )
    expect(card.querySelector('[data-dca-foot="copy"]')).toHaveTextContent('copy id')
    // ↻ only when the context can re-run the read.
    expect(card.querySelector('[data-dca-foot="refresh"]')).toBeNull()
    const again = render(payload('mandate-active'), ctx({ canRefresh: true }))
    expect(again.querySelector('[data-dca-foot="refresh"]')).toHaveTextContent('refresh')
  })

  it('copies the mandate id and says so briefly', async () => {
    vi.useFakeTimers()
    const copyText = vi.fn()
    const card = render(payload('mandate-active'), ctx({ copyText }))
    const button = card.querySelector<HTMLButtonElement>('[data-dca-foot="copy"]')!
    button.click()
    await flush()
    expect(copyText).toHaveBeenCalledWith('dca_1a2b3c4d')
    expect(button.dataset.dcaCopied).toBe('true')
    expect(button).toHaveTextContent('copied')
    vi.advanceTimersByTime(DCA_COPIED_MS)
    expect(button.dataset.dcaCopied).toBeUndefined()
    expect(button).toHaveTextContent('copy id')
  })

  it('keeps attacker-chosen symbols as text', () => {
    const raw = activeWith((m) => {
      ;(m.token as Json).symbol = '<img src=x onerror=alert(1)>'
      m.name = '<b>pwn</b>'
    })
    const card = render(normalizeDcaPayload(raw)!)
    expect(card.querySelector('img')).toBeNull()
    expect(card.querySelector('b')).toBeNull()
    expect(card.querySelector('.dca-pair__token')!.textContent).toBe('<img src=x onerror=alert(1)>')
  })
})

/* ── chart ─────────────────────────────────────────────────────────────── */

function withHistory(runs: Json[]): DcaMandatePayload {
  return normalizeDcaPayload(
    activeWith((m) => {
      m.history = runs
    }),
  ) as DcaMandatePayload
}

function filledRun(n: number, price: number): Json {
  return {
    n,
    at: `2026-09-${String(10 + n).padStart(2, '0')}T09:00:00Z`,
    status: 'filled',
    usd: 10,
    amount: { raw: '1', human: String(10 / price), usd: 10 },
    priceUsd: price,
    orderId: `ord_${n}`,
    txHash: `0x${String(n).padStart(64, '0')}`,
  }
}

describe('buys chart', () => {
  it('shows a hint instead of a chart with no runs or one', () => {
    for (const runs of [[], [filledRun(1, 2800)]]) {
      const p = withHistory(runs)
      expect(buildDcaChartModel(p.mandate)).toBeNull()
      const card = render(p)
      expect(card.querySelector('svg.dca-chart__svg')).toBeNull()
      expect(card.querySelector('.dca-chart--empty')).not.toBeNull()
    }
  })

  it('lays out every attempt in time order on a padded price axis', () => {
    const { mandate } = payload<DcaMandatePayload>('mandate-active')
    const model = buildDcaChartModel(mandate)!
    expect(model.columns).toHaveLength(14)
    expect(model.columns.map((c) => c.run.n)).toEqual([
      1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
    ])
    expect(model.columns.filter((c) => c.kind === 'filled')).toHaveLength(12)
    expect(model.columns[8]!.kind).toBe('skipped')
    expect(model.columns[13]!.kind).toBe('parked')
    expect(model.filled).toBe(12)
    expect(model.minPrice).toBeCloseTo(2701.8)
    expect(model.maxPrice).toBeCloseTo(3092.9)
    // Not from zero: the lowest buy still has a visible bar, the highest nearly fills.
    expect(model.lo).toBeGreaterThan(2000)
    const low = model.columns[2]!
    const high = model.columns[9]!
    expect(100 - low.y!).toBeGreaterThan(10)
    expect(high.y!).toBeLessThan(low.y!)
    // The current price sits above the average.
    expect(model.nowY!).toBeLessThan(model.avgY!)
    // Equal slots.
    expect(model.columns[1]!.x).toBeCloseTo(100 / 14)
  })

  it('caps the chart at the 50 newest attempts', () => {
    const runs = Array.from({ length: 60 }, (_, i) => filledRun(i + 1, 2700 + i)).reverse()
    const model = buildDcaChartModel(withHistory(runs).mandate)!
    expect(model.columns).toHaveLength(50)
    expect(model.columns[0]!.run.n).toBe(11)
  })

  it('draws bars, the average and current-price lines, and marks skipped and failed runs', () => {
    const raw = activeWith((m) => {
      ;(m.history as Json[]).splice(3, 0, {
        n: 11,
        at: '2026-09-24T09:00:00Z',
        status: 'failed',
        reason: 'quote.no_route',
        usd: 10,
      })
    })
    const card = render(normalizeDcaPayload(raw)!)
    const chart = card.querySelector<HTMLElement>('.dca-chart')!
    expect(chart.dataset.dcaChart).toBe('buys')
    const svgEl = chart.querySelector('svg.dca-chart__svg')!
    expect(svgEl.getAttribute('aria-label')).toBe(
      'Buys chart: 12 buys between $2,702 and $3,093, average $2,885, current price $2,980',
    )
    expect(chart.querySelectorAll('.dca-chart__col')).toHaveLength(15)
    expect(chart.querySelectorAll('rect.dca-chart__bar')).toHaveLength(12)
    expect(chart.querySelectorAll('rect.dca-chart__skip')).toHaveLength(1)
    expect(chart.querySelectorAll('rect.dca-chart__parked')).toHaveLength(1)
    expect(chart.querySelectorAll('.dca-chart__fail')).toHaveLength(1)
    expect(chart.querySelector('line.dca-chart__avg-line')).not.toBeNull()
    const nowLine = chart.querySelector('line.dca-chart__now-line')!
    expect(nowLine.getAttribute('data-dca-tone')).toBe('up')
    expect(chart.querySelector('.dca-chart__avg')).toHaveTextContent('avg $2,885')
    expect(chart.querySelector('.dca-chart__now')).toHaveTextContent('now $2,980')
    expect(texts(chart, '.dca-chart__key')).toEqual([
      'buy price',
      'average',
      'current price',
      'awaiting approval',
      'skipped',
      'failed',
    ])
  })

  it('explains a column on hover and focus, and moves with the keyboard', () => {
    const card = render(payload('mandate-active'))
    const tooltip = card.querySelector<HTMLElement>('.dca-chart__tooltip')!
    const cols = [...card.querySelectorAll<SVGGElement>('.dca-chart__col')]
    expect(tooltip.hidden).toBe(true)
    expect(cols[12]!.getAttribute('tabindex')).toBe('0')
    expect(cols[12]!.getAttribute('aria-label')).toMatch(
      /^#13 · .* · filled · \$10 → 0\.003408 ETH @ \$2,935$/,
    )

    cols[12]!.dispatchEvent(new FocusEvent('focus'))
    expect(tooltip.hidden).toBe(false)
    expect(texts(tooltip, '.dca-chart__tooltip-line').slice(1)).toEqual([
      '$10 → 0.003408 ETH @ $2,935',
      'filled',
    ])
    const tx = tooltip.querySelector<HTMLAnchorElement>('a.dca-chart__tx')!
    expect(tx.href).toMatch(/^https:\/\/basescan\.org\/tx\//)
    expect(tx).toHaveTextContent(/^tx 0x[0-9a-f]{4}…[0-9a-f]{4} ↗$/)
    expect(cols[12]!.getAttribute('data-hover')).toBe('true')

    cols[8]!.dispatchEvent(new MouseEvent('mouseenter'))
    expect(tooltip.querySelector('[data-dca-line="detail"]')).toHaveTextContent(
      'ETH at $3,148 above $3,100',
    )
    expect(tooltip.querySelector('[data-dca-line="status"]')).toHaveTextContent('skipped')
    expect(tooltip.querySelector('a.dca-chart__tx')).toBeNull()

    cols[13]!.dispatchEvent(new FocusEvent('focus'))
    expect(tooltip.querySelector('[data-dca-line="status"]')).toHaveTextContent(
      'awaiting approval · buy now',
    )

    const focus = vi.fn()
    ;(cols[12] as unknown as HTMLElement).focus = focus
    cols[13]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', cancelable: true }))
    expect(focus).toHaveBeenCalled()

    cols[13]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(tooltip.hidden).toBe(true)
  })

  it('hides the tooltip a beat after the pointer leaves, unless it moves onto it', () => {
    vi.useFakeTimers()
    const card = render(payload('mandate-active'))
    const tooltip = card.querySelector<HTMLElement>('.dca-chart__tooltip')!
    const col = card.querySelectorAll<SVGGElement>('.dca-chart__col')[3]!
    col.dispatchEvent(new MouseEvent('mouseenter'))
    col.dispatchEvent(new MouseEvent('mouseleave'))
    tooltip.dispatchEvent(new MouseEvent('mouseenter'))
    vi.advanceTimersByTime(500)
    expect(tooltip.hidden).toBe(false)
    tooltip.dispatchEvent(new MouseEvent('mouseleave'))
    vi.advanceTimersByTime(500)
    expect(tooltip.hidden).toBe(true)
  })
})

describe('buys chart axes', () => {
  /** Two fills 0.02 % apart, both on 28 Sep (UTC noon, so one local day anywhere). */
  function nearFlat(): DcaMandatePayload {
    const p = withHistory([
      { ...filledRun(2, 2500.5), at: '2026-09-28T12:10:00Z' },
      { ...filledRun(1, 2500), at: '2026-09-28T12:00:00Z' },
    ])
    p.mandate.acquired.avgPriceUsd = 2500.25
    p.mandate.acquired.currentPriceUsd = 2500.4
    return p
  }

  it('pads a near-flat range so near-equal prices draw near-equal bars', () => {
    const model = buildDcaChartModel(nearFlat().mandate)!
    const [a, b] = model.columns.map((c) => 100 - c.y!)
    expect(a).toBeGreaterThan(20)
    expect(a! / b!).toBeGreaterThan(0.9)
    // At least ±0.5 % of the average past the data.
    expect(model.lo).toBeLessThanOrEqual(2500 - 2500.25 * DCA_CHART_MIN_PAD + 1e-9)
    expect(model.hi).toBeGreaterThanOrEqual(2500.5 + 2500.25 * DCA_CHART_MIN_PAD - 1e-9)
    // A real spread pads by its own range, well past the ±0.5 % floor.
    const wide = buildDcaChartModel(payload<DcaMandatePayload>('mandate-active').mandate)!
    expect(2701.8 - wide.lo).toBeGreaterThan((3092.9 - 2701.8) * 0.35)
  })

  it('labels the top and bottom of the price axis', () => {
    const p = nearFlat()
    const model = buildDcaChartModel(p.mandate)!
    const card = render(p)
    const y = card.querySelector<HTMLElement>('.dca-chart__y')!
    expect(y).toHaveAttribute('aria-hidden', 'true')
    const labels = [...y.querySelectorAll<HTMLElement>('.dca-chart__ylabel')]
    expect(labels.map((l) => l.dataset.edge)).toEqual(['top', 'bottom'])
    expect(labels.map((l) => l.textContent)).toEqual(dcaYAxisLabels(model))
    expect(labels.map((l) => l.textContent)).toEqual(['$2,513', '$2,487'])
    expect(labels[0]!.style.top).toContain('var(--dca-plot-pad)')
  })

  it('gives both y labels one precision, widened until they differ', () => {
    const axis = (avg: number | null, hi: number, lo: number) =>
      dcaYAxisLabels({ avg, hi, lo } as DcaChartModel)
    // The average's precision already resolves the band: both at 2 decimals.
    expect(axis(40, 40.62, 39.38)).toEqual(['$40.62', '$39.38'])
    // A ±0.5 % band at 2 decimals reads "$1.01" / "$1.00": widen both to 3.
    expect(axis(1, 1.0051, 0.9951)).toEqual(['$1.005', '$0.995'])
    expect(axis(1, 1.004, 1.001)).toEqual(['$1.0040', '$1.0010'])
    // Whole dollars above $1,000.
    expect(axis(2500, 2513.2, 2487.4)).toEqual(['$2,513', '$2,487'])
    expect(axis(2500, 2500.4, 2500.1)).toEqual(['$2,500.40', '$2,500.10'])
    // Sub-dollar prices start at the average's significant digits.
    expect(axis(0.0123, 0.01241, 0.01219)).toEqual(['$0.01241', '$0.01219'])
    // Stops at 6 decimals even when they still match.
    expect(axis(1, 1.0000001, 1.0000002)).toEqual(['$1.000000', '$1.000000'])
    // Subscript-zero prices widen by significant digits.
    expect(axis(1.2e-6, 1.2345e-6, 1.2341e-6)).toEqual(['$0.0₅12345', '$0.0₅12341'])
  })

  it('shows times, not the same date twice, when every run fell on one day', () => {
    const sameDay = buildDcaChartModel(nearFlat().mandate)!
    const [first, last] = dcaAxisLabels(sameDay)
    expect(first).toMatch(/^\d{2}:\d{2}$/)
    expect(last).toMatch(/^\d{2}:\d{2}$/)
    expect(first).not.toBe(last)
    const card = render(nearFlat())
    expect(texts(card, '.dca-chart__date')).toEqual([first, last])
    // Runs on different days keep their dates.
    const days = dcaAxisLabels(
      buildDcaChartModel(payload<DcaMandatePayload>('mandate-active').mandate)!,
    )
    expect(days[0]).toMatch(/^Sep \d+$/)
    expect(days[1]).toMatch(/^Sep \d+$/)
  })

  /** Give the plot, its SVG and the tooltip a layout jsdom does not compute. */
  function layout(card: HTMLElement, tip = { w: 160, h: 60 }): HTMLElement {
    const rect = (top: number, width: number, height: number) => () =>
      ({ top, left: 0, width, height, right: width, bottom: top + height, x: 0, y: top }) as DOMRect
    const plot = card.querySelector<HTMLElement>('.dca-chart__plot')!
    plot.getBoundingClientRect = rect(100, 600, 132)
    card.querySelector<SVGElement>('.dca-chart__svg')!.getBoundingClientRect = rect(112, 600, 120)
    const tooltip = card.querySelector<HTMLElement>('.dca-chart__tooltip')!
    Object.defineProperty(tooltip, 'offsetWidth', { configurable: true, value: tip.w })
    Object.defineProperty(tooltip, 'offsetHeight', { configurable: true, value: tip.h })
    return tooltip
  }

  it('opens the tooltip inside the plot, on the hovered column, clamped to its edges', () => {
    const p = payload<DcaMandatePayload>('mandate-active')
    const model = buildDcaChartModel(p.mandate)!
    const card = render(p)
    const tooltip = layout(card)
    const cols = [...card.querySelectorAll<SVGGElement>('.dca-chart__col')]
    const hover = (i: number): void => {
      cols[i]!.dispatchEvent(new MouseEvent('mouseenter'))
    }
    const markTop = (i: number): number => 12 + (120 * model.columns[i]!.y!) / 100

    // A bar whose top leaves room: a few px above it, bottom edge 4px clear.
    const low = model.columns.findIndex((c, i) => c.kind === 'filled' && markTop(i) >= 70)
    expect(low).toBeGreaterThanOrEqual(0)
    hover(low)
    expect(tooltip.dataset.dcaPlace).toBe('above')
    expect(tooltip.style.top).toBe(`${Math.round(markTop(low) - 4 - 60)}px`)
    expect(tooltip.style.bottom).toBe('auto')
    const c = model.columns[low]!
    expect(tooltip.style.left).toBe(`${c.x + c.width / 2}%`)

    // A bar near the plot's top: just below its top instead, never past the plot.
    const high = model.columns.findIndex((c, i) => c.kind === 'filled' && markTop(i) < 64)
    expect(high).toBeGreaterThanOrEqual(0)
    hover(high)
    expect(tooltip.dataset.dcaPlace).toBe('below')
    expect(tooltip.style.top).toBe(`${Math.round(Math.min(markTop(high) + 4, 132 - 60))}px`)

    // Too tall to fit above the low bar: below it, pulled up off the plot's bottom.
    layout(card, { w: 160, h: 120 })
    hover(low)
    expect(tooltip.dataset.dcaPlace).toBe('below')
    expect(tooltip.style.top).toBe('12px')

    // Clamped at the side edges.
    layout(card)
    hover(0)
    expect(tooltip.dataset.align).toBe('start')
    hover(cols.length - 1)
    expect(tooltip.dataset.align).toBe('end')
    hover(Math.floor(cols.length / 2))
    expect(tooltip.dataset.align).toBe('center')
  })

  it('never leaves the old under-the-plot offset behind', () => {
    const card = render(payload('mandate-active'))
    const tooltip = card.querySelector<HTMLElement>('.dca-chart__tooltip')!
    card.querySelectorAll<SVGGElement>('.dca-chart__col')[0]!.dispatchEvent(new FocusEvent('focus'))
    // No layout in jsdom: the same anchor in the plot's CSS lengths.
    expect(tooltip.style.top).not.toContain('100%')
    expect(`${tooltip.style.top} ${tooltip.style.bottom}`).toContain('--dca-plot-')
    expect(['above', 'below']).toContain(tooltip.dataset.dcaPlace)
    expect(tooltip.dataset.align).toBe('start')
  })
})

/* ── list ──────────────────────────────────────────────────────────────── */

describe('buildDcaCard — mandates', () => {
  it('lists every mandate with a mini bar and its next line', () => {
    const p = payload<DcaMandatesPayload>('mandates')
    const names = new Map(p.mandates.map((m) => [m.id, m.name]))
    expect(new Set(names.values()).size).toBe(3)
    const card = render(p)
    expect(card.dataset.dcaKind).toBe('mandates')
    expect(card.querySelector('.dca-card__title')).toHaveTextContent('DCA · 3 mandates')
    expect(card.querySelector('.dca-card__name')).toHaveTextContent('1 active')
    expect(card.querySelector('.dca-totals')).toHaveTextContent('$130 of $900 · $134.21 acquired')
    const rows = [...card.querySelectorAll<HTMLElement>('.dca-row')]
    expect(rows.map((r) => r.dataset.dcaStatus)).toEqual(['active', 'awaiting_approval', 'paused'])
    expect(rows[0]!.dataset.dcaId).toBe('dca_1a2b3c4d')
    // The name leads the row; pair · plan · state is its second line.
    const main = rows[0]!.querySelector('.dca-row__main')!
    expect(main.firstElementChild).toHaveClass('dca-row__name')
    expect(main.firstElementChild!.nextElementSibling).toHaveClass('dca-row__top')
    expect(texts(card, '.dca-row__name')).toEqual(rows.map((r) => names.get(r.dataset.dcaId!)))
    expect(rows[0]!.querySelector('.dca-row__pair')).toHaveTextContent('ETH ← USDC')
    expect(rows[0]!.querySelector('.dca-row__plan')).toHaveTextContent('every day · $10')
    expect(rows[0]!.querySelector('.dca-row__state')).toHaveTextContent('Active')
    expect(rows[0]!.querySelector('.dca-progress--mini')).not.toBeNull()
    expect(rows[0]!.querySelector('.dca-row__spent')).toHaveTextContent('$120 / $300')
    expect(rows[0]!.querySelector('.dca-row__next')).toHaveTextContent('next buy in 3 h 10 m')
    expect(rows[2]!.querySelector('.dca-row__next')).toHaveTextContent('paused')
    expect(card.querySelector('.dca-actions')).toBeNull()
  })

  it('offers compact controls per row on the desk', () => {
    const card = render(payload('mandates'), ctx({ canWrite: true }))
    const actions = [...card.querySelectorAll<HTMLElement>('.dca-row .dca-actions--compact')]
    // The pending proposal (dca_9e8f7a6b) has none: its decision lives in the
    // desk's approvals region, never a second time in the chat.
    expect(actions.map((a) => a.dataset.dcaId)).toEqual(['dca_1a2b3c4d', 'dca_3b4c5d6e'])
    expect(card.querySelector('.dca-row[data-dca-id="dca_9e8f7a6b"] .dca-actions')).toBeNull()
  })

  it('says so when there are none', () => {
    const card = render(payload('mandates-empty'))
    expect(card.querySelector('.dca-card__title')).toHaveTextContent('DCA · 0 mandates')
    expect(card.querySelector('.dca-card__empty')).toHaveTextContent('No DCA mandates yet.')
    expect(card.querySelector('.dca-rows')).toBeNull()
  })
})

/* ── controls ──────────────────────────────────────────────────────────── */

describe('controls', () => {
  it('offers the controls each status allows', () => {
    expect(dcaActionsFor('awaiting_approval')).toEqual([])
    expect(dcaActionsFor('active')).toEqual(['pause', 'run', 'stop'])
    expect(dcaActionsFor('paused')).toEqual(['resume', 'run', 'stop'])
    for (const s of ['completed', 'stopped', 'rejected', 'expired', 'unknown'] as const) {
      expect(dcaActionsFor(s)).toEqual([])
    }
  })

  it('renders no controls at all without canWrite', () => {
    for (const name of ['mandate-active', 'mandate-awaiting']) {
      expect(render(payload(name)).querySelector('.dca-actions'), name).toBeNull()
    }
  })

  it('renders real buttons keyed by action and mandate id', () => {
    const active = render(payload('mandate-active'), ctx({ canWrite: true }))
    const buttons = [
      ...active.querySelectorAll<HTMLButtonElement>('.dca-actions [data-dca-action]'),
    ]
    expect(
      buttons.map((b) => [b.tagName, b.type, b.dataset.dcaAction, b.dataset.dcaId, b.textContent]),
    ).toEqual([
      ['BUTTON', 'button', 'pause', 'dca_1a2b3c4d', 'Pause'],
      ['BUTTON', 'button', 'run', 'dca_1a2b3c4d', 'Buy now'],
      ['BUTTON', 'button', 'stop', 'dca_1a2b3c4d', 'Stop'],
    ])
    expect(buttons[2]!.dataset.dcaTone).toBe('danger')
    const error = active.querySelector<HTMLElement>('.dca-actions__error')!
    expect(error.hidden).toBe(true)
    expect(error).toHaveAttribute('role', 'alert')
  })

  it('maps an operator refusal to plain words, else shows the RPC message', () => {
    expect(dcaErrorText({ code: 'trading.operator_required', message: 'x' })).toBe(
      'Only the desktop app can do this.',
    )
    expect(dcaErrorText({ code: 'trading.dca.bad_state', message: 'mandate is stopped' })).toBe(
      'mandate is stopped',
    )
    expect(dcaErrorText(new Error('boom'))).toBe('boom')
  })
})

function mountWith(
  body: Json,
  call: (method: string, params: Record<string, unknown>) => Promise<unknown>,
  extra: { onOrder?: (id: string) => void; read?: typeof call } = {},
) {
  const host = placeholder()
  const mounter = createDcaMounter({
    fetchPayload: () => Promise.resolve(body),
    call: extra.read,
    actions: { call, onOrder: extra.onOrder },
    now: () => FETCHED_AT,
  })
  mounter.mountDca(document.body)
  return { host, mounter }
}

function button(host: HTMLElement, action: string): HTMLButtonElement {
  return host.querySelector<HTMLButtonElement>(`.dca-actions [data-dca-action="${action}"]`)!
}

describe('control → RPC → payload swap', () => {
  it('pauses: disables the row while in flight, then swaps in the answer', async () => {
    const answer = deferred<unknown>()
    const call = vi.fn(() => answer.promise)
    const { host, mounter } = mountWith(fixture('mandate-active'), call)
    await flush()
    button(host, 'pause').click()
    expect(call).toHaveBeenCalledWith('trading.dca.pause', { mandateId: 'dca_1a2b3c4d' })
    const row = host.querySelector<HTMLElement>('.dca-actions')!
    expect(row.dataset.dcaBusy).toBe('pause')
    expect(host.querySelector('.dca-card')).toHaveAttribute('data-dca-busy', 'true')
    expect(
      [...row.querySelectorAll('button')].every((b) => (b as HTMLButtonElement).disabled),
    ).toBe(true)
    // A second click while in flight does nothing.
    button(host, 'run').click()
    expect(call).toHaveBeenCalledTimes(1)

    answer.resolve(mandatePayloadFor('paused'))
    await flush()
    const card = host.querySelector<HTMLElement>('.dca-card')!
    expect(card.dataset.dcaStatus).toBe('paused')
    expect(card).not.toHaveAttribute('data-dca-busy')
    expect(texts(host, '.dca-actions [data-dca-action]')).toEqual(['Resume', 'Buy now', 'Stop'])
    expect(button(host, 'resume').disabled).toBe(false)
    mounter.destroyAll()
  })

  it('offers no Approve on a pending proposal; it points at the approvals region', async () => {
    const call = vi.fn()
    const { host, mounter } = mountWith(fixture('mandate-awaiting'), call)
    await flush()
    const card = host.querySelector<HTMLElement>('.dca-card')!
    expect(card.dataset.dcaStatus).toBe('awaiting_approval')
    expect(card.querySelector('.dca-actions')).toBeNull()
    expect(card.querySelector('.dca-card__approve-hint')).toHaveTextContent(
      'Approve or reject it in the approvals above the message box.',
    )
    expect(call).not.toHaveBeenCalled()
    mounter.destroyAll()
  })

  it('shows no approval hint outside the desk', () => {
    const card = render(payload('mandate-awaiting'))
    expect(card.querySelector('.dca-card__approve-hint')).toBeNull()
  })

  it('hands a "Buy now" order to the desk', async () => {
    const onOrder = vi.fn()
    const call = vi.fn(() => {
      const raw = fixture('mandate-active')
      raw.run = {
        n: 15,
        at: '2026-09-28T05:48:30Z',
        status: 'parked',
        orderId: 'ord_buy_now',
        manual: true,
      }
      return Promise.resolve(raw)
    })
    const { host, mounter } = mountWith(fixture('mandate-active'), call, { onOrder })
    await flush()
    button(host, 'run').click()
    await flush()
    expect(call).toHaveBeenCalledWith('trading.dca.run', { mandateId: 'dca_1a2b3c4d' })
    expect(onOrder).toHaveBeenCalledWith('ord_buy_now')
    mounter.destroyAll()
  })

  it('asks for a second click before stopping', async () => {
    vi.useFakeTimers()
    const call = vi.fn(() => Promise.resolve(mandatePayloadFor('stopped')))
    const { host, mounter } = mountWith(fixture('mandate-active'), call)
    await flush()
    const stop = button(host, 'stop')
    stop.click()
    expect(call).not.toHaveBeenCalled()
    expect(stop.dataset.dcaConfirm).toBe('true')
    expect(stop).toHaveTextContent('Stop — click again')
    // Left alone, it disarms.
    vi.advanceTimersByTime(DCA_CONFIRM_MS)
    expect(stop.dataset.dcaConfirm).toBeUndefined()
    expect(stop).toHaveTextContent('Stop')

    stop.click()
    stop.click()
    await flush()
    expect(call).toHaveBeenCalledWith('trading.dca.stop', { mandateId: 'dca_1a2b3c4d' })
    const card = host.querySelector<HTMLElement>('.dca-card')!
    expect(card.dataset.dcaStatus).toBe('stopped')
    expect(card.querySelector('.dca-actions')).toBeNull()
    mounter.destroyAll()
  })

  it('shows a refusal inline and frees the buttons', async () => {
    // Not a state refusal (those re-read the card, below): the card stays as it is.
    const call = vi.fn(() =>
      Promise.reject(Object.assign(new Error('mandate is not active'), { code: 'trading.failed' })),
    )
    const { host, mounter } = mountWith(fixture('mandate-active'), call)
    await flush()
    button(host, 'pause').click()
    await flush()
    const error = host.querySelector<HTMLElement>('.dca-actions__error')!
    expect(error.hidden).toBe(false)
    expect(error).toHaveTextContent('mandate is not active')
    expect(host.querySelector('.dca-actions')).not.toHaveAttribute('data-dca-busy')
    expect(button(host, 'pause').disabled).toBe(false)
    expect(host.querySelector('.dca-card')).toHaveAttribute('data-dca-status', 'active')
    mounter.destroyAll()
  })

  it('treats an answer that is not a mandate payload as an error', async () => {
    const call = vi.fn(() => Promise.resolve({ ok: true }))
    const { host, mounter } = mountWith(fixture('mandate-active'), call)
    await flush()
    button(host, 'pause').click()
    await flush()
    expect(host.querySelector('.dca-actions__error')).toHaveTextContent(
      'DCA data could not be read.',
    )
    mounter.destroyAll()
  })

  it('swaps a row of a list card in place', async () => {
    const call = vi.fn(() => Promise.resolve(mandatePayloadFor('paused')))
    const { host, mounter } = mountWith(fixture('mandates'), call)
    await flush()
    host
      .querySelector<HTMLButtonElement>(
        '.dca-row[data-dca-id="dca_1a2b3c4d"] [data-dca-action="pause"]',
      )!
      .click()
    await flush()
    const card = host.querySelector<HTMLElement>('.dca-card')!
    expect(card.dataset.dcaKind).toBe('mandates')
    expect(
      [...card.querySelectorAll<HTMLElement>('.dca-row')].map((r) => r.dataset.dcaStatus),
    ).toEqual(['paused', 'awaiting_approval', 'paused'])
    mounter.destroyAll()
  })

  it('reads the actions getter at click time', async () => {
    let actions: { call: typeof call } | null = null
    const call = vi.fn(() => Promise.resolve(mandatePayloadFor('paused')))
    const host = placeholder()
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-active')),
      actions: () => actions,
    })
    mounter.mountDca(document.body)
    await flush()
    expect(host.querySelector('.dca-actions')).toBeNull()
    actions = { call }
    mounter.mandateChanged(fixture('mandate-active'))
    button(host, 'pause').click()
    await flush()
    expect(call).toHaveBeenCalledTimes(1)
    mounter.destroyAll()
  })
})

/* ── refresh + events ──────────────────────────────────────────────────── */

describe('refresh and events', () => {
  it('↻ re-runs the echoed read and redraws', async () => {
    const read = vi.fn(() => Promise.resolve(mandatePayloadFor('paused')))
    const host = placeholder()
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-active')),
      call: read,
      now: () => FETCHED_AT,
    })
    mounter.mountDca(document.body)
    await flush()
    host.querySelector<HTMLButtonElement>('[data-dca-foot="refresh"]')!.click()
    expect(host.querySelector('.dca-card')).toHaveAttribute('data-dca-refreshing', 'true')
    await flush()
    expect(read).toHaveBeenCalledWith('trading.dca.get', { mandateId: 'dca_1a2b3c4d' })
    expect(host.querySelector('.dca-card')).toHaveAttribute('data-dca-status', 'paused')
    expect(host.querySelector('.dca-card')).not.toHaveAttribute('data-dca-refreshing')
    mounter.destroyAll()
  })

  it('says why a ↻ failed, under the footer', async () => {
    const read = vi.fn(() => Promise.reject(new Error('gateway down')))
    const host = placeholder()
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandates')),
      call: read,
    })
    mounter.mountDca(document.body)
    await flush()
    host.querySelector<HTMLButtonElement>('[data-dca-foot="refresh"]')!.click()
    await flush()
    expect(read).toHaveBeenCalledWith('trading.dca.list', {})
    expect(host.querySelector('.dca-card__refresh-error')).toHaveTextContent(
      'refresh failed: gateway down',
    )
    expect(host.querySelector<HTMLButtonElement>('[data-dca-foot="refresh"]')!.disabled).toBe(false)
    mounter.destroyAll()
  })

  it('swaps a changed mandate into every card that draws it', async () => {
    const one = placeholder('/a')
    const list = placeholder('/b')
    const other = placeholder('/c')
    const mounter = createDcaMounter({
      fetchPayload: (url) =>
        Promise.resolve(
          fixture(
            url === '/a' ? 'mandate-active' : url === '/b' ? 'mandates' : 'mandate-completed',
          ),
        ),
    })
    mounter.mountDca(document.body)
    await flush()
    // The event body carries the full payload under `mandate`.
    mounter.mandateChanged({ mandate: mandatePayloadFor('paused') })
    expect(one.querySelector('.dca-card')).toHaveAttribute('data-dca-status', 'paused')
    expect(list.querySelector('.dca-row[data-dca-id="dca_1a2b3c4d"]')).toHaveAttribute(
      'data-dca-status',
      'paused',
    )
    expect(other.querySelector('.dca-card')).toHaveAttribute('data-dca-status', 'completed')
    // A bare mandate works too, and keeps the card's own ↻ request.
    const bare = mandatePayloadFor('stopped').mandate as Json
    mounter.mandateChanged({ mandate: bare })
    expect(one.querySelector('.dca-card')).toHaveAttribute('data-dca-status', 'stopped')
    mounter.mandateChanged({ nothing: true })
    mounter.mandateChanged(null)
    expect(one.querySelector('.dca-card')).toHaveAttribute('data-dca-status', 'stopped')
    mounter.destroyAll()
  })

  it('reads the three event body shapes', () => {
    const full = mandatePayloadFor('paused')
    expect(dcaChangedPayload(full)?.payload?.mandate.status).toBe('paused')
    expect(dcaChangedPayload({ mandate: full })?.payload?.request?.kind).toBe('get')
    const bare = dcaChangedPayload({ mandate: full.mandate })
    expect(bare?.mandate.id).toBe('dca_1a2b3c4d')
    expect(bare?.payload).toBeNull()
    expect(dcaChangedPayload({ mandate: 'x' })).toBeNull()
  })

  it('re-reads a card when one of its orders settles', async () => {
    const read = vi.fn(() => Promise.resolve(fixture('mandate-active')))
    const host = placeholder()
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-active')),
      call: read,
    })
    mounter.mountDca(document.body)
    await flush()
    // The mount's own live read.
    expect(read).toHaveBeenCalledTimes(1)
    read.mockClear()
    mounter.orderFinished('ord_unrelated')
    expect(read).not.toHaveBeenCalled()
    // The parked "buy now" (#14) settles.
    mounter.orderFinished('ord_7f3a91c2d4')
    await flush()
    expect(read).toHaveBeenCalledWith('trading.dca.get', { mandateId: 'dca_1a2b3c4d' })
    expect(host.querySelector('.dca-card')).not.toBeNull()
    mounter.destroyAll()
  })
})

describe('live state over the artifact snapshot', () => {
  function completedFor(name: string): Json {
    const raw = fixture(name)
    ;(raw.mandate as Json).status = 'completed'
    return raw
  }

  function activeFor(name: string): Json {
    const raw = fixture(name)
    ;(raw.mandate as Json).status = 'active'
    return raw
  }

  it('re-reads on mount; the snapshot is a placeholder with its controls off', async () => {
    const answer = deferred<unknown>()
    const read = vi.fn(() => answer.promise)
    const host = placeholder()
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-active')),
      call: read,
      actions: { call: vi.fn() },
      now: () => FETCHED_AT,
    })
    mounter.mountDca(document.body)
    await flush()
    expect(read).toHaveBeenCalledWith('trading.dca.get', { mandateId: 'dca_1a2b3c4d' })
    const snapshot = host.querySelector<HTMLElement>('.dca-card')!
    expect(snapshot.dataset.dcaStatus).toBe('active')
    expect(snapshot.dataset.dcaStale).toBe('checking')
    // The mount's read is quiet: no dimming, no hint yet.
    expect(snapshot).not.toHaveAttribute('data-dca-refreshing')
    expect(snapshot.querySelector('.dca-card__stale')).toBeNull()
    expect(button(host, 'pause').disabled).toBe(true)
    expect(button(host, 'stop').disabled).toBe(true)

    answer.resolve(completedFor('mandate-active'))
    await flush()
    const card = host.querySelector<HTMLElement>('.dca-card')!
    expect(card.dataset.dcaStatus).toBe('completed')
    expect(card).not.toHaveAttribute('data-dca-stale')
    expect(card.querySelector('.dca-actions')).toBeNull()
    mounter.destroyAll()
  })

  it('keeps the snapshot with its controls off and says so when the read fails', async () => {
    const read = vi.fn((): Promise<unknown> => Promise.reject(new Error('gateway down')))
    const host = placeholder()
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-active')),
      call: read,
      actions: { call: vi.fn() },
      now: () => FETCHED_AT,
    })
    mounter.mountDca(document.body)
    await flush()
    const card = host.querySelector<HTMLElement>('.dca-card')!
    expect(card.dataset.dcaStatus).toBe('active')
    expect(card.dataset.dcaStale).toBe('failed')
    expect(button(host, 'pause').disabled).toBe(true)
    const hint = card.querySelector<HTMLElement>('.dca-card__stale')!
    expect(hint).toHaveTextContent('state may be stale · ↻')
    // It sits right above the footer; a quiet read flashes no footer error.
    expect(hint.nextElementSibling).toHaveClass('dca-card__foot')
    expect(card.querySelector('.dca-card__refresh-error')).toBeNull()

    // The hint's ↻ re-reads; a success lifts the stale state.
    read.mockImplementationOnce(() => Promise.resolve(activeFor('mandate-active')))
    hint.querySelector<HTMLButtonElement>('.dca-card__stale-refresh')!.click()
    await flush()
    const live = host.querySelector<HTMLElement>('.dca-card')!
    expect(live.dataset.dcaStatus).toBe('active')
    expect(live).not.toHaveAttribute('data-dca-stale')
    expect(live.querySelector('.dca-card__stale')).toBeNull()
    expect(button(host, 'pause').disabled).toBe(false)
    mounter.destroyAll()
  })

  it('draws a re-mounted card from the newest state it has seen, then re-reads it', async () => {
    const read = vi.fn((): Promise<unknown> => Promise.resolve(activeFor('mandate-awaiting')))
    const first = placeholder('/a')
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-awaiting')),
      call: read,
      actions: { call: vi.fn() },
      now: () => FETCHED_AT,
    })
    mounter.mountDca(document.body)
    await flush()
    expect(first.querySelector('.dca-card')).toHaveAttribute('data-dca-status', 'active')

    // The transcript re-renders: a new placeholder for the same artifact.
    first.remove()
    const later = deferred<unknown>()
    read.mockImplementationOnce(() => later.promise)
    const second = placeholder('/a')
    mounter.mountDca(document.body)
    await flush()
    // No flash of the stale snapshot: the cached live state, controls on.
    const card = second.querySelector<HTMLElement>('.dca-card')!
    expect(card.dataset.dcaStatus).toBe('active')
    expect(card).not.toHaveAttribute('data-dca-stale')
    expect(button(second, 'pause').disabled).toBe(false)
    // …and the live read still runs in the background.
    expect(read).toHaveBeenCalledTimes(2)
    later.resolve(completedFor('mandate-awaiting'))
    await flush()
    expect(second.querySelector('.dca-card')).toHaveAttribute('data-dca-status', 'completed')

    // `trading.dca.changed` feeds the cache too, and the cache outlives
    // destroyAll (it holds no DOM): the same mounter re-mounts from it.
    const paused = fixture('mandate-awaiting')
    ;(paused.mandate as Json).status = 'paused'
    mounter.mandateChanged(paused)
    mounter.destroyAll()
    second.remove()
    read.mockImplementation(() => new Promise(() => {}))
    const third = placeholder('/a')
    mounter.mountDca(document.body)
    await flush()
    expect(third.querySelector('.dca-card')).toHaveAttribute('data-dca-status', 'paused')
    expect(third.querySelector('.dca-card')).not.toHaveAttribute('data-dca-stale')
    mounter.destroyAll()

    // A fresh mounter has no cache: the snapshot, controls off, re-reading.
    third.remove()
    const fourth = placeholder('/a')
    const fresh = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-awaiting')),
      call: read,
      now: () => FETCHED_AT,
    })
    fresh.mountDca(document.body)
    await flush()
    expect(fourth.querySelector('.dca-card')).toHaveAttribute(
      'data-dca-status',
      'awaiting_approval',
    )
    expect(fourth.querySelector('.dca-card')).toHaveAttribute('data-dca-stale', 'checking')
    fresh.destroyAll()
  })

  it('swaps cached rows into a re-mounted list card', async () => {
    const read = vi.fn(() => new Promise<unknown>(() => {}))
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandates')),
      call: read,
      now: () => FETCHED_AT,
    })
    mounter.mandateChanged(mandatePayloadFor('paused'))
    const host = placeholder()
    mounter.mountDca(document.body)
    await flush()
    const card = host.querySelector<HTMLElement>('.dca-card')!
    expect(card.querySelector('.dca-row[data-dca-id="dca_1a2b3c4d"]')).toHaveAttribute(
      'data-dca-status',
      'paused',
    )
    // Two of three rows are still snapshot state.
    expect(card.dataset.dcaStale).toBe('checking')
    mounter.destroyAll()
  })

  it('re-reads the card after a control is refused as out of date', async () => {
    const act = vi.fn(() =>
      Promise.reject(
        Object.assign(new Error('cannot pause dca_1a2b3c4d: it is completed'), {
          code: 'trading.dca.bad_state',
        }),
      ),
    )
    const answers = [fixture('mandate-active'), completedFor('mandate-active')]
    const read = vi.fn(() => Promise.resolve(answers.shift()))
    const host = placeholder()
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-active')),
      call: read,
      actions: { call: act },
      now: () => FETCHED_AT,
    })
    mounter.mountDca(document.body)
    await flush()
    // The mount's read still said active (the engine moved on after it).
    expect(button(host, 'pause').disabled).toBe(false)
    button(host, 'pause').click()
    await flush()
    expect(act).toHaveBeenCalledWith('trading.dca.pause', { mandateId: 'dca_1a2b3c4d' })
    expect(read).toHaveBeenCalledTimes(2)
    const card = host.querySelector<HTMLElement>('.dca-card')!
    expect(card.dataset.dcaStatus).toBe('completed')
    expect(card).not.toHaveAttribute('data-dca-stale')
    expect(card.querySelector('.dca-actions')).toBeNull()
    mounter.destroyAll()
  })

  it('also re-reads after not_found, and leaves other refusals alone', async () => {
    const codes = ['trading.dca.not_found', 'trading.failed']
    const act = vi.fn(() =>
      Promise.reject(Object.assign(new Error('nope'), { code: codes.shift() })),
    )
    const read = vi.fn(() => Promise.resolve(fixture('mandate-active')))
    const host = placeholder()
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-active')),
      call: read,
      actions: { call: act },
      now: () => FETCHED_AT,
    })
    mounter.mountDca(document.body)
    await flush()
    button(host, 'pause').click()
    await flush()
    expect(read).toHaveBeenCalledTimes(2)
    button(host, 'pause').click()
    await flush()
    expect(read).toHaveBeenCalledTimes(2)
    expect(host.querySelector('.dca-actions__error')).toHaveTextContent('nope')
    mounter.destroyAll()
  })
})

/* ── mounter plumbing ──────────────────────────────────────────────────── */

describe('createDcaMounter', () => {
  it('renders into the placeholder once and hides the loading line', async () => {
    const host = placeholder()
    const fetchPayload = vi.fn(() => Promise.resolve(fixture('mandate-active')))
    const mounter = createDcaMounter({ fetchPayload })
    mounter.mountDca(document.body)
    mounter.mountDca(document.body)
    await flush()
    expect(fetchPayload).toHaveBeenCalledTimes(1)
    expect(fetchPayload).toHaveBeenCalledWith('/api/v1/artifacts/dca-1')
    expect(host.querySelectorAll('.dca-card')).toHaveLength(1)
    expect(host.dataset.dcaHost).toBe('rendered')
    expect(host.querySelector<HTMLElement>('.msg-artifact-dca__status')!.hidden).toBe(true)
    mounter.destroyAll()
  })

  it('says why a card could not be drawn', async () => {
    const bad = placeholder('/a')
    const broken = placeholder('/b')
    const empty = placeholder('')
    const mounter = createDcaMounter({
      fetchPayload: (url) =>
        url === '/a' ? Promise.resolve({ kind: 'nope' }) : Promise.reject(new Error('HTTP 500')),
    })
    mounter.mountDca(document.body)
    await flush()
    expect(bad.querySelector('.msg-artifact-dca__status')).toHaveTextContent(
      'DCA data could not be read.',
    )
    expect(broken.querySelector('.msg-artifact-dca__status')).toHaveTextContent(
      'DCA card failed to load.',
    )
    expect(empty.querySelector('.msg-artifact-dca__status')).toHaveTextContent(
      'DCA data is unavailable.',
    )
  })

  it('does not render into a host that left the document mid-fetch', async () => {
    const host = placeholder()
    const answer = deferred<unknown>()
    const mounter = createDcaMounter({ fetchPayload: () => answer.promise })
    mounter.mountDca(document.body)
    host.remove()
    answer.resolve(fixture('mandate-active'))
    await flush()
    expect(host.querySelector('.dca-card')).toBeNull()
  })

  it('clears pending copy and confirm resets on destroyAll', async () => {
    vi.useFakeTimers()
    const host = placeholder()
    const mounter = createDcaMounter({
      fetchPayload: () => Promise.resolve(fixture('mandate-active')),
      actions: { call: vi.fn() },
      copyText: () => {},
    })
    mounter.mountDca(document.body)
    await flush()
    host.querySelector<HTMLButtonElement>('[data-dca-foot="copy"]')!.click()
    button(host, 'stop').click()
    await flush()
    expect(vi.getTimerCount()).toBe(3)
    mounter.destroyAll()
    expect(vi.getTimerCount()).toBe(0)
  })
})

/* ── measured layout ───────────────────────────────────────────────────── */

describe('layout', () => {
  it('goes wide from DCA_WIDE_MIN_PX', () => {
    expect(dcaLayoutFor(DCA_WIDE_MIN_PX)).toBe('wide')
    expect(dcaLayoutFor(DCA_WIDE_MIN_PX - 1)).toBe('narrow')
  })

  it('stamps data-dca-layout from the measured width, leaving an unmeasured card alone', () => {
    const card = render(payload('mandate-active'))
    layoutDcaCard(card)
    expect(card.dataset.dcaLayout).toBe('narrow')
    Object.defineProperty(card, 'offsetWidth', { configurable: true, value: 600 })
    layoutDcaCard(card)
    expect(card.dataset.dcaLayout).toBe('wide')
    Object.defineProperty(card, 'offsetWidth', { configurable: true, value: 360 })
    layoutDcaCard(card)
    expect(card.dataset.dcaLayout).toBe('narrow')
  })
})

/* ── in-flight runs and reason codes ───────────────────────────────────── */

describe('pending runs and reason codes', () => {
  const pendingRun: Json = {
    n: 15,
    at: '2026-09-28T05:47:40Z',
    manual: false,
    status: 'pending',
    reason: null,
    reasonCode: null,
    usd: 10,
    amount: null,
    priceUsd: 2979.9,
    orderId: 'ord_pending01',
    txHash: null,
    explorerUrl: null,
    gasUsd: null,
  }

  it('keeps the machine reason code next to the human reason', () => {
    const { mandate } = payload<DcaMandatePayload>('mandate-active')
    const skipped = mandate.history.find((r) => r.status === 'skipped')!
    expect(skipped.reasonCode).toBe('max_price')
    expect(skipped.reason).toBe('ETH at $3,148 above $3,100')
    expect(mandate.history[0]!.reasonCode).toBeNull()
  })

  it('draws a buy in flight as "buying…", never as a bar', () => {
    const p = normalizeDcaPayload(
      activeWith((m) => {
        ;(m.history as Json[]).unshift(pendingRun)
      }),
    ) as DcaMandatePayload
    expect(p.mandate.history[0]!.status).toBe('pending')
    const card = render(p)
    const row = card.querySelector<HTMLElement>('.dca-run')!
    expect(row.dataset.dcaRunStatus).toBe('pending')
    expect(row.querySelector('.dca-run__status')).toHaveTextContent('buying…')
    expect(row.querySelector('.dca-run__detail')).toHaveTextContent('$10')
    const model = buildDcaChartModel(p.mandate)!
    expect(model.columns[model.columns.length - 1]!.kind).toBe('pending')
    const chart = card.querySelector('.dca-chart')!
    expect(chart.querySelectorAll('rect.dca-chart__pending')).toHaveLength(1)
    expect(chart.querySelectorAll('rect.dca-chart__bar')).toHaveLength(12)
    expect(chart.querySelector('.dca-chart__key[data-key="pending"]')).toHaveTextContent('buying…')
  })

  it('words a skip from its code when the engine sent no reason', () => {
    const p = normalizeDcaPayload(
      activeWith((m) => {
        const skipped = (m.history as Json[]).find((r) => r.status === 'skipped')!
        skipped.reason = null
        skipped.reasonCode = 'daily_cap'
        m.history = (m.history as Json[]).slice(5)
      }),
    ) as DcaMandatePayload
    const card = render(p)
    const row = card.querySelector<HTMLElement>('.dca-run[data-dca-run-status="skipped"]')!
    expect(row.dataset.dcaReason).toBe('daily_cap')
    expect(row.querySelector('.dca-run__detail')).toHaveTextContent('daily cap reached')
  })
})

/* ── engine fixtures (written by the Python side) ──────────────────────── */

const engineFixtures = existsSync(ENGINE_FIXTURES)
  ? readdirSync(ENGINE_FIXTURES).filter((name) => name.endsWith('.json'))
  : []

describe.skipIf(engineFixtures.length === 0)('engine fixtures', () => {
  it.each(engineFixtures)('%s normalizes and renders', (name) => {
    const raw = JSON.parse(readFileSync(`${ENGINE_FIXTURES}/${name}`, 'utf8')) as {
      kind?: string
      mandate?: { id?: string; status?: string; token?: { symbol?: string } }
      mandates?: unknown[]
    }
    const p = normalizeDcaPayload(raw)
    expect(p, name).not.toBeNull()
    expect(p!.kind).toBe(raw.kind)
    const card = render(p!, ctx({ canWrite: true, canRefresh: true }))
    expect(card.dataset.dcaKind).toBe(raw.kind)
    expect(card.querySelector('.dca-card__foot')).not.toBeNull()
    if (p!.kind === 'mandate') {
      const m = raw.mandate!
      expect(card.dataset.dcaStatus).toBe(m.status)
      expect(card.dataset.dcaId).toBe(m.id)
      // Symbols render exactly as the engine sent them.
      expect(card.querySelector('.dca-pair__token')!.textContent).toBe(m.token?.symbol)
      const history = (p as DcaMandatePayload).mandate.history as DcaRun[]
      expect(card.querySelectorAll('.dca-run')).toHaveLength(Math.min(5, history.length))
    } else {
      expect(card.querySelectorAll('.dca-row')).toHaveLength(
        (p as DcaMandatesPayload).mandates.length,
      )
      expect((p as DcaMandatesPayload).mandates).toHaveLength(raw.mandates!.length)
    }
  })
})
