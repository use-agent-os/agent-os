import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DCA_ARTIFACT_MIME, formatCountdown, formatDcaPrice, formatDcaUsd } from './dca'
import { formatTokenAmount } from './lp'
import {
  TRIGGER_ARTIFACT_MIME,
  TRIGGER_CONFIRM_MS,
  TRIGGER_NEAR_PCT,
  TRIGGER_RECENT_FIRES,
  TRIGGER_WIDE_MIN_PX,
  agoText,
  buildTriggerCard,
  conditionLabel,
  createTriggerMounter,
  distancePctOf,
  distanceText,
  formatDistance,
  formatTriggerPrice,
  heroText,
  isTriggerArtifact,
  isTriggerTerminal,
  layoutTriggerCard,
  normalizeTriggerPayload,
  normalizeTriggerRequest,
  nowText,
  proximityOf,
  rowNowText,
  rowPlanText,
  sizeText,
  triggerActionsFor,
  triggerChangedPayload,
  triggerErrorText,
  triggerGauge,
  triggerLayoutFor,
  triggerReadMethod,
  triggerTickDelay,
  type Trigger,
  type TriggerListPayload,
  type TriggerOnePayload,
  type TriggerPayload,
  type TriggerRenderContext,
  type TriggerStatus,
  unbreakable,
} from './trigger'

// The engine writes its payloads here (regenerate with
// AGENTOS_REGEN_TRIGGER_FIXTURES=1); the renderer reads the very same files,
// so the two sides cannot drift. The assertions below stay structural (keys,
// statuses, text shapes), never pinned to a fixture's exact figures: where a
// test needs a figure it patches it into the raw body itself.
const FIXTURES = '../tests/fixtures/trigger_cards'
const NAMES = [
  'trigger-armed',
  'trigger-awaiting',
  'trigger-done',
  'trigger-alert',
  'triggers',
  'triggers-empty',
] as const
type Name = (typeof NAMES)[number]

type Json = Record<string, unknown>

function fixture(name: Name): Json {
  return JSON.parse(readFileSync(`${FIXTURES}/${name}.json`, 'utf8')) as Json
}

function payload<T extends TriggerPayload>(body: Json): T {
  const normalized = normalizeTriggerPayload(body)
  if (!normalized) throw new Error('fixture did not normalize')
  return normalized as T
}

function one(name: Name): TriggerOnePayload {
  return payload<TriggerOnePayload>(fixture(name))
}

function idOf(name: Name): string {
  return (fixture(name).trigger as Json).id as string
}

/** The clock a fixture was taken at: its `fetchedAt`. */
function fetchedAtOf(name: Name): number {
  const at = Date.parse(fixture(name).fetchedAt as string)
  if (Number.isNaN(at)) throw new Error(`${name} has no fetchedAt`)
  return at
}

/** A fixture's raw body with its trigger patched. */
function withTrigger(name: Name, patch: (trigger: Json) => void): Json {
  const raw = fixture(name)
  patch(raw.trigger as Json)
  return raw
}

/** A fixture's raw body with its trigger in `status` (and its stamp moved on). */
function inStatus(name: Name, status: TriggerStatus, extra: Json = {}): Json {
  return withTrigger(name, (tr) => {
    tr.status = status
    tr.updatedAt = '2026-12-31T00:00:00Z'
    Object.assign(tr, extra)
  })
}

/** The trigger of a fixture with its condition and market overridden. */
function triggerWith(name: Name, condition: Json, market: Json = {}, extra: Json = {}): Trigger {
  const raw = withTrigger(name, (tr) => {
    tr.condition = { ...(tr.condition as Json), label: '', ...condition }
    tr.market = { ...(tr.market as Json), ...market }
    Object.assign(tr, extra)
  })
  return payload<TriggerOnePayload>(raw).trigger
}

const NOW = Date.parse('2026-10-04T09:15:00Z')

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

function placeholder(src = '/api/v1/artifacts/trg-1'): HTMLElement {
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

describe('isTriggerArtifact', () => {
  it('matches the trigger mime, with or without parameters, in any case', () => {
    expect(isTriggerArtifact({ mime: TRIGGER_ARTIFACT_MIME })).toBe(true)
    expect(isTriggerArtifact({ mime: `${TRIGGER_ARTIFACT_MIME}; charset=utf-8` })).toBe(true)
    expect(isTriggerArtifact({ mime: TRIGGER_ARTIFACT_MIME.toUpperCase() })).toBe(true)
  })

  it('rejects everything else', () => {
    for (const mime of [DCA_ARTIFACT_MIME, 'application/json', '', 'application/vnd.agentos']) {
      expect(isTriggerArtifact({ mime }), mime).toBe(false)
    }
    expect(isTriggerArtifact(null)).toBe(false)
    expect(isTriggerArtifact(undefined)).toBe(false)
    expect(isTriggerArtifact({})).toBe(false)
  })
})

describe('normalizeTriggerPayload', () => {
  it('reads every fixture', () => {
    for (const name of NAMES) {
      const raw = fixture(name)
      const p = normalizeTriggerPayload(raw)
      expect(p, name).not.toBeNull()
      expect(p!.kind, name).toBe(raw.kind)
      expect(p!.request?.kind, name).toBe(raw.kind === 'trigger' ? 'get' : 'list')
    }
  })

  it('keeps a trigger as the engine sent it', () => {
    const raw = fixture('trigger-armed')
    const tr = raw.trigger as Json
    const { trigger } = one('trigger-armed')
    expect(trigger.id).toBe(tr.id)
    expect(trigger.id).toMatch(/^trg_/)
    expect(trigger.name).toBe(tr.name)
    expect(trigger.kind).toBe('sell')
    expect(trigger.status).toBe('armed')
    expect(trigger.token.symbol).toBe((tr.token as Json).symbol)
    expect(trigger.chain?.key).toBe((tr.chain as Json).key)
    expect(trigger.condition.direction).toBe((tr.condition as Json).direction)
    expect(trigger.condition.priceUsd).toBe((tr.condition as Json).priceUsd)
    expect(trigger.condition.confirmTicks).toBe((tr.condition as Json).confirmTicks)
    expect(trigger.condition.hits).toBe((tr.condition as Json).hits)
    expect(trigger.action.kind).toBe('sell')
    expect(trigger.market.priceUsd).toBe((tr.market as Json).priceUsd)
    expect(trigger.wallet?.address).toBe((tr.wallet as Json).address)
  })

  it('reads the awaiting proposal, the done sell and the trailing alert', () => {
    const awaiting = one('trigger-awaiting').trigger
    expect(awaiting.status).toBe('awaiting_approval')
    expect(awaiting.kind).toBe('buy')
    expect(awaiting.initiator).toBe('agent')
    expect(awaiting.condition.fromPriceUsd).not.toBeNull()
    expect(awaiting.expiresAt).not.toBeNull()

    const done = one('trigger-done').trigger
    expect(done.status).toBe('done')
    expect(done.result?.orderId).toBeTruthy()
    expect(done.result?.amountIn?.human).toBeTruthy()
    expect(done.fires.length).toBeGreaterThan(0)

    const alert = one('trigger-alert').trigger
    expect(alert.kind).toBe('alert')
    expect(alert.condition.direction).toBe('trail')
    expect(alert.condition.peakPriceUsd).not.toBeNull()
    expect(alert.condition.stopPriceUsd).not.toBeNull()
    expect(alert.market.balance).toBeNull()
  })

  it('keeps statusReason, the balance, the result and fire reasons as the engine sent them', () => {
    const bodies = [
      ...NAMES.filter((n) => fixture(n).kind === 'trigger').map((n) => fixture(n).trigger as Json),
      ...(fixture('triggers').triggers as Json[]),
    ]
    const seen = { reason: 0, balance: 0, result: 0, fireReason: 0 }
    for (const raw of bodies) {
      const tr = payload<TriggerOnePayload>({ kind: 'trigger', trigger: raw }).trigger
      const id = raw.id as string
      expect(tr.statusReason, id).toBe(raw.statusReason ?? null)
      if (raw.statusReason) seen.reason++

      const balance = (raw.market as Json).balance as Json | null
      expect(tr.market.balance, id).toEqual(
        balance ? { raw: balance.raw, human: balance.human, usd: balance.usd ?? null } : null,
      )
      if (balance) seen.balance++

      const result = raw.result as Json | null
      if (result) {
        seen.result++
        const amount = (a: Json | null) =>
          a ? { raw: a.raw, human: a.human, usd: a.usd ?? null } : null
        expect(tr.result, id).toEqual({
          orderId: result.orderId,
          txHash: result.txHash ?? null,
          explorerUrl: expect.stringMatching(/^https:\/\//) as unknown,
          amountIn: amount(result.amountIn as Json | null),
          amountOut: amount(result.amountOut as Json | null),
          priceUsd: result.priceUsd ?? null,
          gasUsd: result.gasUsd ?? null,
        })
        if (result.txHash) expect(tr.result!.explorerUrl).toContain(result.txHash as string)
      } else {
        expect(tr.result, id).toBeNull()
      }

      const fires = raw.fires as Json[]
      expect(
        tr.fires.map((f) => [f.n, f.status, f.reasonCode, f.reason, f.manual]),
        id,
      ).toEqual(
        fires.map((f) => [
          f.n,
          f.status,
          f.reasonCode ?? null,
          f.reason ?? null,
          f.manual === true,
        ]),
      )
      seen.fireReason += fires.filter((f) => f.reasonCode).length
    }
    // The fixtures exercise each of these, so a regenerated set that drops one is noticed.
    expect(seen.reason).toBeGreaterThan(0)
    expect(seen.balance).toBeGreaterThan(0)
    expect(seen.result).toBeGreaterThan(0)
    expect(seen.fireReason).toBeGreaterThan(0)
  })

  it('reads the list and its totals, and the empty list', () => {
    const raw = fixture('triggers')
    const list = payload<TriggerListPayload>(raw)
    expect(list.triggers).toHaveLength((raw.triggers as unknown[]).length)
    expect(list.totals.count).toBe((raw.totals as Json).count)
    const live = new Set(['awaiting_approval', 'armed', 'triggered', 'paused'])
    expect(list.triggers.every((tr) => live.has(tr.status))).toBe(true)
    const empty = payload<TriggerListPayload>(fixture('triggers-empty'))
    expect(empty.triggers).toEqual([])
    expect(empty.totals.count).toBe(0)
  })

  it('reads numbers sent as strings and treats a missing USD as unknown, never zero', () => {
    const trigger = triggerWith(
      'trigger-armed',
      { priceUsd: '3800', hits: '1', confirmTicks: '2' },
      { priceUsd: null, distancePct: null, balance: null },
      { token: { symbol: 'WETH', address: '0x4200000000000000000000000000000000000006' } },
    )
    expect(trigger.condition.priceUsd).toBe(3800)
    expect(trigger.condition.hits).toBe(1)
    expect(trigger.market.priceUsd).toBeNull()
    expect(trigger.market.distancePct).toBeNull()
    expect(trigger.market.balance).toBeNull()
    const zero = triggerWith('trigger-armed', { priceUsd: 0 }, { priceUsd: 0 })
    expect(zero.condition.priceUsd).toBeNull()
  })

  it('never throws on junk and returns null when there is nothing to draw', () => {
    for (const junk of [
      null,
      undefined,
      42,
      'trigger',
      [],
      {},
      { kind: 'nope' },
      { kind: 'trigger' },
      { kind: 'trigger', trigger: 'x' },
      { kind: 'trigger', trigger: { id: 'trg_x' } },
      { kind: 'trigger', trigger: { token: { symbol: 'ETH' } } },
      { kind: 'triggers', triggers: 'x' },
    ]) {
      expect(() => normalizeTriggerPayload(junk), JSON.stringify(junk)).not.toThrow()
      expect(normalizeTriggerPayload(junk), JSON.stringify(junk)).toBeNull()
    }
  })

  it('tolerates a trigger with only its core fields', () => {
    const p = normalizeTriggerPayload({
      kind: 'trigger',
      trigger: { id: 'trg_bare0001', token: { symbol: 'ETH' } },
    }) as TriggerOnePayload
    expect(p.trigger.kind).toBe('unknown')
    expect(p.trigger.status).toBe('unknown')
    expect(p.trigger.condition.direction).toBe('unknown')
    expect(p.trigger.condition.confirmTicks).toBe(2)
    expect(p.trigger.fires).toEqual([])
    expect(p.trigger.result).toBeNull()
    expect(p.trigger.name).toContain('ETH')
    expect(p.warnings).toEqual([])
    expect(p.request).toBeNull()
    // It still draws, with no controls and no gauge.
    const card = render(p, ctx({ canWrite: true }))
    expect(card.querySelector('.trigger-actions')).toBeNull()
    expect(card.querySelector('.trigger-gauge')).toBeNull()
    expect(card.querySelector('.trigger-card__foot')).not.toBeNull()
  })

  it('keeps every known status and drops an unknown one to "unknown"', () => {
    for (const status of [
      'awaiting_approval',
      'armed',
      'triggered',
      'paused',
      'done',
      'stopped',
      'rejected',
      'expired',
    ] as const) {
      expect(payload<TriggerOnePayload>(inStatus('trigger-armed', status)).trigger.status).toBe(
        status,
      )
    }
    for (const junk of ['ARMED ', 'firing', '', null, 3]) {
      const status = payload<TriggerOnePayload>(
        withTrigger('trigger-armed', (tr) => (tr.status = junk)),
      ).trigger.status
      expect(status, String(junk)).toBe(junk === 'ARMED ' ? 'armed' : 'unknown')
    }
  })

  it('maps an unknown fire status to "unknown" and drops unusable fires', () => {
    const p = payload<TriggerOnePayload>(
      withTrigger('trigger-done', (tr) => {
        tr.fires = [{ n: 2, status: 'exploded' }, 'junk', null, ...(tr.fires as unknown[])]
      }),
    )
    expect(p.trigger.fires[0]!.status).toBe('unknown')
    expect(p.trigger.fires).toHaveLength(1 + (one('trigger-done').trigger.fires.length ?? 0))
  })

  it('never keeps a non-http explorer link', () => {
    const p = payload<TriggerOnePayload>(
      withTrigger('trigger-done', (tr) => {
        tr.chain = { id: 8453, key: 'base', name: 'Base', explorer: 'javascript:alert(1)' }
        tr.fires = [
          { n: 1, status: 'filled', txHash: 'not-a-hash', explorerUrl: 'javascript:alert(1)' },
          { n: 2, status: 'filled', txHash: null, explorerUrl: 'https://basescan.org/tx/0xabc' },
        ]
        tr.result = {
          orderId: 'ord_1',
          txHash: null,
          explorerUrl: 'data:text/html,hi',
          amountIn: null,
        }
      }),
    )
    expect(p.trigger.chain?.explorer).toBe('')
    expect(p.trigger.fires[0]!.explorerUrl).toBe('')
    expect(p.trigger.fires[1]!.explorerUrl).toBe('https://basescan.org/tx/0xabc')
    expect(p.trigger.result?.explorerUrl).toBe('')
  })

  it('builds a fire link from the chain explorer and a hex hash', () => {
    const hash = `0x${'ab'.repeat(32)}`
    const p = payload<TriggerOnePayload>(
      withTrigger('trigger-done', (tr) => {
        tr.fires = [{ n: 1, status: 'filled', txHash: hash, explorerUrl: 'https://evil.example' }]
      }),
    )
    expect(p.trigger.fires[0]!.explorerUrl).toBe(`${p.trigger.chain!.explorer}/tx/${hash}`)
  })

  it('only accepts get and list as the request to re-run', () => {
    expect(normalizeTriggerRequest({ kind: 'get', params: { triggerId: 'trg_1' } })).toEqual({
      kind: 'get',
      params: { triggerId: 'trg_1' },
    })
    expect(normalizeTriggerRequest({ kind: 'list' })).toEqual({ kind: 'list', params: {} })
    for (const kind of ['approve', 'fire', 'stop', '', null]) {
      expect(normalizeTriggerRequest({ kind, params: {} }), String(kind)).toBeNull()
    }
    expect(triggerReadMethod({ kind: 'get', params: {} })).toBe('trading.trigger.get')
    expect(triggerReadMethod({ kind: 'list', params: {} })).toBe('trading.trigger.list')
  })

  it('reads the fire a "Fire now" answered with', () => {
    const raw = inStatus('trigger-armed', 'triggered')
    raw.fire = { n: 1, at: '2026-10-04T09:15:01Z', status: 'pending', orderId: 'ord_x', manual: 1 }
    const p = payload<TriggerOnePayload>(raw)
    expect(p.fire).toMatchObject({ n: 1, status: 'pending', orderId: 'ord_x', manual: true })
  })
})

/* ── text ──────────────────────────────────────────────────────────────── */

describe('hero, condition and distance text', () => {
  it('says what fires and when, in one sentence', () => {
    expect(heroText(one('trigger-armed').trigger)).toMatch(/^sell \d+(\.\d+)? % of \S+ when /)
    expect(heroText(one('trigger-awaiting').trigger)).toMatch(/^buy \$[\d,.]+ of \S+ when /)
    expect(heroText(one('trigger-alert').trigger)).toMatch(/^alert when \S+ is /)
    const sellAmount = triggerWith('trigger-armed', {}, {}, {})
    expect(heroText(sellAmount)).toContain(conditionLabel(sellAmount))
  })

  it('words each size form', () => {
    const base = one('trigger-armed').trigger
    const pct = {
      ...base,
      action: { ...base.action, amountPct: 50, amount: null, amountUsd: null },
    }
    const amount = {
      ...base,
      action: {
        ...base.action,
        amountPct: null,
        amount: { raw: '1', human: '0.05', usd: null },
        amountUsd: null,
      },
    }
    const usd = {
      ...base,
      action: { ...base.action, amountPct: null, amount: null, amountUsd: 100 },
    }
    const sym = base.token.symbol
    expect(heroText({ ...pct, condition: { ...pct.condition, label: 'under $3,800' } })).toBe(
      `sell 50 % of ${sym} when under $3,800`,
    )
    expect(heroText(amount)).toMatch(new RegExp(`^sell 0\\.05 ${sym} when `))
    expect(heroText(usd)).toMatch(new RegExp(`^sell \\$100 of ${sym} when `))
    expect(sizeText({ ...pct, action: { ...pct.action, estimatedUsd: 189 } })).toBe('50 % · ≈ $189')
    expect(sizeText(usd)).toBe('$100')
    expect(sizeText(one('trigger-alert').trigger)).toBe('notify only')
  })

  it('derives the condition when the payload has no label', () => {
    expect(
      conditionLabel(triggerWith('trigger-armed', { direction: 'below', priceUsd: 3800 })),
    ).toBe('under $3,800')
    expect(
      conditionLabel(triggerWith('trigger-armed', { direction: 'above', priceUsd: 5000 })),
    ).toBe('over $5,000')
    expect(conditionLabel(triggerWith('trigger-alert', { direction: 'trail', trailPct: 10 }))).toBe(
      '10 % below peak',
    )
    expect(conditionLabel(triggerWith('trigger-armed', { direction: '??' }))).toBe(
      'its condition holds',
    )
    // The engine's words win when given.
    const labelled = one('trigger-armed').trigger
    expect(conditionLabel(labelled)).toBe(labelled.condition.label || conditionLabel(labelled))
  })

  it('measures the distance to the line from the price, signed', () => {
    const above = triggerWith(
      'trigger-armed',
      { direction: 'below', priceUsd: 3780 },
      { priceUsd: 3790, distancePct: null },
    )
    expect(distancePctOf(above)).toBeCloseTo(-0.264, 2)
    expect(distanceText(above)).toBe('0.3 % above the line')
    const under = triggerWith(
      'trigger-armed',
      { direction: 'above', priceUsd: 5000 },
      { priceUsd: 4800, distancePct: null },
    )
    expect(distancePctOf(under)).toBeCloseTo(4.167, 2)
    expect(distanceText(under)).toBe('4.2 % below the line')
    const met = triggerWith(
      'trigger-armed',
      { direction: 'below', priceUsd: 3800 },
      { priceUsd: 3700, distancePct: null },
    )
    expect(distancePctOf(met)).toBe(0)
    expect(distanceText(met)).toBe('at the line')
    const trail = triggerWith(
      'trigger-alert',
      { direction: 'trail', stopPriceUsd: 3582 },
      { priceUsd: 3795, distancePct: null },
    )
    expect(distanceText(trail)).toMatch(/^\d+\.\d % above the stop$/)
    // The engine's figure wins over the derived one.
    expect(
      distancePctOf(triggerWith('trigger-armed', {}, { priceUsd: 3790, distancePct: -2.1 })),
    ).toBe(-2.1)
    // No price anywhere (the market's, nor the token's it falls back to).
    const unpriced = triggerWith(
      'trigger-armed',
      {},
      { priceUsd: null, distancePct: null },
      { token: { symbol: 'WETH', address: '0x4200000000000000000000000000000000000006' } },
    )
    expect(unpriced.market.priceUsd).toBeNull()
    expect(distanceText(unpriced)).toBe('')
    // The token's price stands in for a missing market price.
    const fallback = triggerWith('trigger-armed', {}, { priceUsd: null })
    expect(fallback.market.priceUsd).toBe(fallback.token.priceUsd)
  })

  it('signs a list row distance', () => {
    expect(formatDistance(-0.26)).toBe('−0.3 %')
    expect(formatDistance(4)).toBe('+4.0 %')
    expect(formatDistance(0)).toBe('at the line')
    expect(formatDistance(null)).toBe('')
  })

  it('counts "checked N ago" at the right grain', () => {
    const at = '2026-10-04T09:00:00Z'
    const base = Date.parse(at)
    expect(agoText(at, base + 12_000)).toBe('12 s ago')
    expect(agoText(at, base + 4 * 60_000 + 5_000)).toBe('4 m ago')
    expect(agoText(at, base + 3 * 3_600_000)).toBe('3 h ago')
    expect(agoText(at, base + 2 * 86_400_000)).toBe('2 d ago')
    expect(agoText(null, base)).toBe('')
    expect(agoText('nope', base)).toBe('')
  })
})

describe('the live line', () => {
  const checkedAt = '2026-10-04T09:14:48Z'

  it('reads price · distance · checked for an armed trigger', () => {
    const tr = triggerWith(
      'trigger-armed',
      { direction: 'below', priceUsd: 3780, hits: 0 },
      { priceUsd: 3790, distancePct: null, checkedAt },
    )
    expect(nowText(tr, NOW)).toBe(
      `${tr.token.symbol} $3,790 · 0.3 % above the line · checked 12 s ago`,
    )
  })

  it('counts down the confirmation once the condition held once', () => {
    const tr = triggerWith('trigger-armed', { hits: 1, confirmTicks: 2 }, { checkedAt })
    expect(nowText(tr, NOW)).toMatch(/· fires after 1 more check · checked \d+ s ago$/)
    const three = triggerWith('trigger-armed', { hits: 1, confirmTicks: 3 }, { checkedAt })
    expect(nowText(three, NOW)).toContain('fires after 2 more checks')
  })

  it('says it waits when there is no price', () => {
    const tr = triggerWith(
      'trigger-armed',
      {},
      { priceUsd: null, checkedAt },
      {
        token: { symbol: 'WETH', address: '0x4200000000000000000000000000000000000006' },
      },
    )
    expect(nowText(tr, NOW)).toBe('armed, waiting for a price · checked 12 s ago')
  })

  it('words every other status', () => {
    // Read at the fixture's own clock: its expiry is relative to `fetchedAt`.
    const awaiting = one('trigger-awaiting').trigger
    const fetchedAt = fetchedAtOf('trigger-awaiting')
    const expiresAt = Date.parse(awaiting.expiresAt!)
    expect(expiresAt).toBeGreaterThan(fetchedAt)
    expect(nowText(awaiting, fetchedAt)).toBe(
      `awaiting approval · proposal expires in ${unbreakable(formatCountdown(expiresAt - fetchedAt))}`,
    )
    // Past its expiry the engine has not swept it yet: the line says so, no countdown.
    expect(nowText(awaiting, expiresAt)).toBe('awaiting approval · proposal expiring')
    expect(nowText(awaiting, expiresAt + 60_000)).toBe('awaiting approval · proposal expiring')
    expect(nowText({ ...awaiting, expiresAt: null }, NOW)).toBe('awaiting approval')
    const done = one('trigger-done').trigger
    expect(nowText(done, NOW)).toMatch(/^done · sold [\d.,]+ \S+ at \$[\d,.]+$/)
    const bought = {
      ...done,
      kind: 'buy' as const,
      result: { ...done.result!, amountOut: { raw: '1', human: '0.0132', usd: null } },
    }
    expect(nowText(bought, NOW)).toMatch(/^done · bought 0\.0132 \S+ at /)
    const alerted = {
      ...one('trigger-alert').trigger,
      status: 'done' as const,
      fires: [{ ...done.fires[0]!, status: 'alerted' as const, priceUsd: 3790 }],
    }
    expect(nowText(alerted, NOW)).toBe('done · alerted at $3,790')
    const base = one('trigger-armed').trigger
    expect(nowText({ ...base, status: 'triggered' }, NOW)).toBe('fired · order open')
    expect(nowText({ ...base, status: 'paused' }, NOW)).toMatch(/^paused/)
    expect(nowText({ ...base, status: 'stopped' }, NOW)).toMatch(/^stopped/)
    expect(nowText({ ...base, status: 'rejected' }, NOW)).toMatch(/^rejected/)
    expect(nowText({ ...base, status: 'expired' }, NOW)).toMatch(/^expired/)
    expect(nowText({ ...base, status: 'unknown' }, NOW)).toBe('')
  })

  it('ticks every second while the stamp counts seconds, then on the minute', () => {
    const tr = triggerWith('trigger-armed', {}, { checkedAt: '2026-10-04T09:15:00Z' })
    expect(triggerTickDelay(tr, NOW + 5_000)).toBe(1_000)
    expect(triggerTickDelay(tr, NOW + 90_000)).toBe(30_000)
    expect(triggerTickDelay(tr, NOW + 2 * 3_600_000)).toBe(60_000)
    expect(triggerTickDelay({ ...tr, status: 'paused' }, NOW)).toBe(60_000)
    const awaiting = one('trigger-awaiting').trigger
    const expires = Date.parse(awaiting.expiresAt!)
    expect(triggerTickDelay(awaiting, expires - 30 * 60_000)).toBe(1_000)
  })
})

/* ── gauge geometry ────────────────────────────────────────────────────── */

describe('triggerGauge', () => {
  it('places the price and the line on a padded rail, shading where it fires', () => {
    const below = triggerWith(
      'trigger-armed',
      { direction: 'below', priceUsd: 3800 },
      { priceUsd: 3900, distancePct: null },
    )
    const g = triggerGauge(below)!
    expect(g.lo).toBeLessThan(3800)
    expect(g.hi).toBeGreaterThan(3900)
    expect(g.current!).toBeGreaterThan(g.trigger!)
    for (const x of [g.current!, g.trigger!]) {
      expect(x).toBeGreaterThan(0)
      expect(x).toBeLessThan(1)
    }
    expect(g.zone).toEqual([0, g.trigger])
    expect(g.peak).toBeNull()
    expect(g.stop).toBeNull()
    expect(g.proximity).toBe('far')

    const above = triggerGauge(
      triggerWith('trigger-armed', { direction: 'above', priceUsd: 5000 }, { priceUsd: 4800 }),
    )!
    expect(above.current!).toBeLessThan(above.trigger!)
    expect(above.zone).toEqual([above.trigger, 1])
  })

  it('draws the peak and the stop for a trailing condition', () => {
    const g = triggerGauge(
      triggerWith(
        'trigger-alert',
        { direction: 'trail', trailPct: 10, peakPriceUsd: 3980, stopPriceUsd: 3582 },
        { priceUsd: 3795, distancePct: null },
      ),
    )!
    expect(g.trigger).toBeNull()
    expect(g.stop!).toBeLessThan(g.current!)
    expect(g.current!).toBeLessThan(g.peak!)
    expect(g.zone).toEqual([0, g.stop])
  })

  it('pads a line alone and keeps a near price apart from it', () => {
    const alone = triggerGauge(
      triggerWith(
        'trigger-armed',
        { direction: 'below', priceUsd: 3800 },
        { priceUsd: null },
        {
          token: { symbol: 'WETH', address: '0x4200000000000000000000000000000000000006' },
        },
      ),
    )!
    expect(alone.current).toBeNull()
    expect(alone.trigger).toBeCloseTo(0.5, 5)
    const near = triggerGauge(
      triggerWith(
        'trigger-armed',
        { direction: 'below', priceUsd: 3800 },
        { priceUsd: 3810, distancePct: null },
      ),
    )!
    expect(near.current! - near.trigger!).toBeGreaterThan(0.05)
    expect(near.proximity).toBe('near')
  })

  it('has nothing to draw without a line', () => {
    expect(triggerGauge(triggerWith('trigger-armed', { priceUsd: null }))).toBeNull()
    expect(triggerGauge(triggerWith('trigger-alert', { stopPriceUsd: null }))).toBeNull()
    expect(triggerGauge(triggerWith('trigger-armed', { direction: 'sideways' }))).toBeNull()
  })

  it('reads proximity from the signed distance', () => {
    expect(proximityOf(0)).toBe('met')
    expect(proximityOf(-TRIGGER_NEAR_PCT)).toBe('near')
    expect(proximityOf(0.4)).toBe('near')
    expect(proximityOf(-3)).toBe('far')
    expect(proximityOf(null)).toBeNull()
  })

  it('renders the gauge as SVG with its hooks and labels', () => {
    const p = payload<TriggerOnePayload>(
      withTrigger('trigger-armed', (tr) => {
        tr.condition = { ...(tr.condition as Json), direction: 'below', priceUsd: 3780, hits: 0 }
        tr.market = { ...(tr.market as Json), priceUsd: 3790, distancePct: -0.26 }
      }),
    )
    const gauge = render(p).querySelector<HTMLElement>('.trigger-gauge')!
    expect(gauge.dataset.triggerDist).toBe('-0.26')
    expect(gauge.dataset.triggerProximity).toBe('near')
    expect(gauge.dataset.triggerDirection).toBe('below')
    expect(gauge.getAttribute('role')).toBe('img')
    expect(gauge.getAttribute('aria-label')).toContain('$3,790')
    expect(gauge.querySelector('svg.trigger-gauge__svg')).not.toBeNull()
    expect(gauge.querySelector('.trigger-gauge__rail')).not.toBeNull()
    expect(gauge.querySelector('.trigger-gauge__zone')).not.toBeNull()
    expect(gauge.querySelector('.trigger-gauge__line')?.getAttribute('data-mark')).toBe('line')
    expect(gauge.querySelector('.trigger-gauge__now')?.getAttribute('cx')).toMatch(/%$/)
    expect(texts(gauge, '.trigger-gauge__label')).toEqual(['line $3,780', 'now $3,790'])

    const trail = render(one('trigger-alert')).querySelector<HTMLElement>('.trigger-gauge')!
    expect(trail.dataset.triggerDirection).toBe('trail')
    expect(trail.querySelector('.trigger-gauge__line')?.getAttribute('data-mark')).toBe('stop')
    expect(trail.querySelector('.trigger-gauge__peak')).not.toBeNull()
    expect(
      [...trail.querySelectorAll<HTMLElement>('.trigger-gauge__label')].map((n) => n.dataset.mark),
    ).toEqual(['stop', 'peak', 'now'])
  })

  // A sell over $0.50 read "over $0.50" in its hero (the engine's label) and
  // "line $0.5" on its gauge: one card, two voices for one price.
  it('says the gauge prices in the hero’s voice: whole cents keep two decimals', () => {
    expect(formatTriggerPrice(0.5)).toBe('$0.50')
    expect(formatTriggerPrice(0.1)).toBe('$0.10')
    expect(formatTriggerPrice(0.05)).toBe('$0.05')
    expect(formatTriggerPrice(0.1234)).toBe('$0.123')
    expect(formatTriggerPrice(1)).toBe('$1.00')
    expect(formatTriggerPrice(3780)).toBe('$3,780')
    expect(formatTriggerPrice(null)).toBe(formatDcaPrice(null))
    const p = payload<TriggerOnePayload>(
      withTrigger('trigger-armed', (tr) => {
        tr.condition = {
          ...(tr.condition as Json),
          direction: 'above',
          priceUsd: 0.5,
          label: 'over $0.50',
          hits: 0,
        }
        tr.market = { ...(tr.market as Json), priceUsd: 1, distancePct: 0 }
      }),
    )
    const card = render(p)
    expect(card.querySelector('.trigger-card__hero')?.textContent).toContain('over $0.50')
    const gauge = card.querySelector<HTMLElement>('.trigger-gauge')!
    expect(texts(gauge, '.trigger-gauge__label')).toEqual(['line $0.50', 'now $1.00'])
    // …and the derived label, when the engine sent none, says it the same way.
    expect(
      conditionLabel(triggerWith('trigger-armed', { direction: 'above', priceUsd: 0.5 })),
    ).toBe('over $0.50')
  })
})

/* ── the card ──────────────────────────────────────────────────────────── */

describe('buildTriggerCard — trigger', () => {
  it('reads like an instrument: head, pill, hero, live line, gauge, facts', () => {
    const p = one('trigger-armed')
    const card = render(p)
    expect(card.tagName).toBe('ARTICLE')
    expect(card.classList.contains('trigger-card')).toBe(true)
    expect(card.dataset.triggerKind).toBe('trigger')
    expect(card.dataset.triggerAction).toBe('sell')
    expect(card.dataset.triggerStatus).toBe('armed')
    expect(card.dataset.triggerChain).toBe(p.trigger.chain!.key)
    expect(card.dataset.triggerLayout).toBe('narrow')
    expect(card.dataset.triggerId).toBe(p.trigger.id)
    expect(card.querySelector('.trigger-card__head .trigger-card__name')).toHaveTextContent(
      p.trigger.name,
    )
    expect(card.querySelector('.trigger-card__head .trigger-chain')).toHaveTextContent(
      p.trigger.chain!.name,
    )
    const pill = card.querySelector<HTMLElement>('.trigger-card__head .trigger-pill')!
    expect(pill.dataset.status).toBe('armed')
    expect(pill).toHaveTextContent('Armed')
    // The glyph is CSS only: no text, no emoji in the DOM.
    expect(card.querySelector('.trigger-card__glyph')?.textContent).toBe('')
    expect(card.querySelector('.trigger-card__hero-line')).toHaveTextContent(heroText(p.trigger))
    expect(card.querySelector('.trigger-card__now')?.textContent).toBe(nowText(p.trigger, NOW))
    expect(card.querySelector('.trigger-gauge')).not.toBeNull()
    expect(
      [...card.querySelectorAll<HTMLElement>('.trigger-card__facts .trigger-fact')].map(
        (n) => n.dataset.triggerFact,
      ),
    ).toEqual(['size', 'balance', 'valid', 'approval'])
    const balance = p.trigger.market.balance
    const balanceValue = card.querySelector<HTMLElement>(
      '[data-trigger-fact="balance"] .trigger-fact__value',
    )!
    if (balance) {
      // A sell is sized from the token it holds.
      expect(balanceValue.textContent).toBe(
        `${formatTokenAmount(balance.human)} ${p.trigger.token.symbol}`,
      )
      expect(balanceValue.dataset.triggerNoValue).toBeUndefined()
    } else {
      expect(balanceValue.dataset.triggerNoValue).toBe('true')
    }
    const valid = card.querySelector<HTMLElement>('[data-trigger-fact="valid"]')!
    const validValue = valid.querySelector('.trigger-fact__value')!.textContent
    if (p.trigger.validUntil) {
      // A dated condition reads its day, with the full stamp on hover.
      expect(validValue).not.toBe('GTC')
      expect(validValue).toMatch(/^[A-Z][a-z]{2} \d{1,2}, \d{4}$/)
      expect(valid.title).not.toBe('')
    } else {
      expect(validValue).toBe('GTC')
      expect(valid.title).toBe('')
    }
  })

  it('reads GTC when the condition has no end, and a date when it has one', () => {
    const gtc = render(payload(withTrigger('trigger-armed', (tr) => (tr.validUntil = null))))
    expect(gtc.querySelector('[data-trigger-fact="valid"] .trigger-fact__value')).toHaveTextContent(
      'GTC',
    )
    const dated = render(
      payload(withTrigger('trigger-armed', (tr) => (tr.validUntil = '2027-03-05T12:00:00Z'))),
    )
    expect(
      dated.querySelector('[data-trigger-fact="valid"] .trigger-fact__value'),
    ).toHaveTextContent('Mar 5, 2027')
  })

  it('shows the hits as a hook on the live line', () => {
    const card = render(
      payload(withTrigger('trigger-armed', (tr) => ((tr.condition as Json).hits = 1))),
    )
    expect(card.querySelector<HTMLElement>('.trigger-card__now')!.dataset.triggerHits).toBe('1/2')
  })

  it('draws the awaiting proposal with where its line came from', () => {
    const p = one('trigger-awaiting')
    const { condition } = p.trigger
    expect(condition.fromPriceUsd).not.toBeNull()
    const card = render(p)
    expect(card.dataset.triggerStatus).toBe('awaiting_approval')
    expect(card.dataset.triggerAction).toBe('buy')
    const from = card.querySelector('.trigger-card__from')?.textContent ?? ''
    expect(from).toMatch(/^line set [−+]?\d+\.\d % from \$[\d,.]+$/)
    expect(from.endsWith(`from ${formatDcaPrice(condition.fromPriceUsd)}`)).toBe(true)
    const move = ((condition.priceUsd! - condition.fromPriceUsd!) / condition.fromPriceUsd!) * 100
    expect(from).toContain(`${Math.abs(move).toFixed(1)} %`)
    // A buy waits on the quote it spends.
    const balance = card.querySelector('[data-trigger-fact="balance"] .trigger-fact__value')
    if (p.trigger.market.balance) {
      expect(balance).toHaveTextContent(
        `${formatTokenAmount(p.trigger.market.balance.human)} ${p.trigger.quote.symbol}`,
      )
    }
    if (p.trigger.action.needsApproval) {
      expect(card.querySelector('[data-trigger-fact="approval"]')).toHaveAttribute(
        'data-trigger-waits',
        'true',
      )
    }
  })

  it('draws a done sell with its fill and no reason line', () => {
    const p = one('trigger-done')
    const card = render(p)
    expect(card.dataset.triggerStatus).toBe('done')
    expect(card.querySelector('.trigger-card__now')?.textContent).toMatch(/^done · sold /)
    expect(card.querySelector('.trigger-card__reason')).toBeNull()
    const fire = card.querySelector<HTMLElement>('.trigger-fires .trigger-fire')!
    expect(fire.dataset.triggerFireStatus).toBe('filled')
    expect(fire.querySelector('.trigger-fire__n')?.textContent).toMatch(/^#\d+$/)
    expect(fire.querySelector('.trigger-fire__detail')?.textContent).toMatch(
      /^[\d.,]+ \S+ → [\d.,]+ \S+ @\u00a0\$[\d,.]+$/,
    )
    const link = fire.querySelector<HTMLAnchorElement>('a.trigger-fire__link')!
    expect(link.href).toMatch(/^https:\/\//)
    expect(link.rel).toBe('noopener noreferrer')
    expect(link.target).toBe('_blank')
  })

  it('reads a done fill as what it moved, never the wallet now', () => {
    const p = one('trigger-done')
    const tr = p.trigger
    const { amountIn, amountOut } = tr.result!
    // A sell spends the token and receives the quote.
    const moved = `${formatTokenAmount(amountIn!.human)} ${tr.token.symbol} → ${formatTokenAmount(
      amountOut!.human,
    )} ${tr.quote.symbol}`
    const usd = amountIn!.usd !== null ? ` · ≈ ${formatDcaUsd(amountIn!.usd)}` : ''
    expect(sizeText(tr)).toBe(`${moved}${usd}`)
    const card = render(p)
    const facts = [...card.querySelectorAll<HTMLElement>('.trigger-card__facts .trigger-fact')]
    // The balance is the wallet now: a finished trigger leaves it out, whatever the engine sent.
    expect(facts.map((n) => n.dataset.triggerFact)).toEqual(['size', 'valid', 'approval'])
    expect(card.querySelector('[data-trigger-fact="size"] .trigger-fact__value')?.textContent).toBe(
      `${moved}${usd}`,
    )
    expect(card.querySelector('[data-trigger-fact="size"]')).toHaveAttribute(
      'title',
      `${moved}${usd}`,
    )
    expect(card.querySelector('.trigger-card__facts')?.textContent).not.toMatch(/wallet balance/)
    // Its hero still says how it ended.
    expect(card.querySelector('.trigger-card__now')?.textContent).toBe(nowText(tr, NOW))
    expect(card.querySelector('.trigger-card__now')?.textContent).toMatch(/^done · sold /)

    // Out side unknown: the in side alone, with the in side's USD.
    const inOnly = { ...tr, result: { ...tr.result!, amountOut: null } }
    expect(sizeText(inOnly)).toBe(`${formatTokenAmount(amountIn!.human)} ${tr.token.symbol}${usd}`)
    // A buy spends the quote and receives the token.
    const bought = { ...tr, kind: 'buy' as const }
    expect(sizeText(bought)).toBe(
      `${formatTokenAmount(amountIn!.human)} ${tr.quote.symbol} → ${formatTokenAmount(
        amountOut!.human,
      )} ${tr.token.symbol}${usd}`,
    )
    // No result (stopped, expired): the plan as it was.
    const stopped = { ...tr, status: 'stopped' as const, result: null }
    expect(sizeText(stopped)).not.toContain('→')
    // An old engine that still sends a balance and a stale estimate cannot leak into a done card.
    const stale = render(
      payload(
        withTrigger('trigger-done', (raw) => {
          raw.market = {
            ...(raw.market as Json),
            balance: { raw: '0', human: '0', usd: 0 },
          }
          raw.action = { ...(raw.action as Json), amountPct: 100, estimatedUsd: 0 }
        }),
      ),
    )
    expect(stale.querySelector('[data-trigger-fact="balance"]')).toBeNull()
    expect(stale.querySelector('[data-trigger-fact="size"]')?.textContent).toContain(moved)
    expect(stale.querySelector('[data-trigger-fact="size"]')?.textContent).not.toContain('100 %')
  })

  it('keeps the live balance and approval hook only while the trigger can fire', () => {
    const waiting = withTrigger('trigger-done', (raw) => {
      raw.action = { ...(raw.action as Json), needsApproval: true }
    })
    const done = render(payload(waiting))
    expect(done.querySelector('[data-trigger-fact="approval"]')).not.toHaveAttribute(
      'data-trigger-waits',
    )
    const armed = render(
      payload({ ...waiting, trigger: { ...(waiting.trigger as Json), status: 'armed' } }),
    )
    expect(armed.querySelector('[data-trigger-fact="approval"]')).toHaveAttribute(
      'data-trigger-waits',
      'true',
    )
    expect(armed.querySelector('[data-trigger-fact="balance"]')).not.toBeNull()
    for (const status of ['done', 'stopped', 'rejected', 'expired'] as const) {
      expect(isTriggerTerminal(status), status).toBe(true)
    }
    for (const status of ['awaiting_approval', 'armed', 'triggered', 'paused'] as const) {
      expect(isTriggerTerminal(status), status).toBe(false)
    }
  })

  it('draws an alert as notify only: no balance, no on-fire fact', () => {
    const p = one('trigger-alert')
    expect(p.trigger.kind).toBe('alert')
    const card = render(p)
    expect(
      [...card.querySelectorAll<HTMLElement>('.trigger-card__facts .trigger-fact')].map(
        (n) => n.dataset.triggerFact,
      ),
    ).toEqual(['size', 'valid'])
    const size = card.querySelector<HTMLElement>('[data-trigger-fact="size"] .trigger-fact__value')!
    expect(size.textContent).toBe('notify only')
    expect(size.dataset.triggerNoValue).toBeUndefined()
  })

  it('ends a done alert at its price, with no distance after it or on the gauge', () => {
    const fired = one('trigger-done').trigger.fires[0]!
    const p = payload<TriggerOnePayload>(
      inStatus('trigger-alert', 'done', {
        statusReason: 'alerted at $1.00',
        fires: [{ ...fired, status: 'alerted', priceUsd: 1, orderId: null, txHash: null }],
      }),
    )
    // The engine's last distance is stale once the alert has fired.
    expect(distancePctOf(p.trigger)).not.toBeNull()
    const card = render(p)
    expect(nowText(p.trigger, NOW)).toBe('done · alerted at $1.00')
    expect(card.querySelector('.trigger-card__now')?.textContent).toBe('done · alerted at $1.00')
    expect(card.querySelector('.trigger-card__reason')).toBeNull()
    const gauge = card.querySelector<HTMLElement>('.trigger-gauge')
    if (gauge) {
      expect(gauge.dataset.triggerDist).toBeUndefined()
      expect(gauge.dataset.triggerProximity).toBeUndefined()
    }
    expect(card.querySelector('[data-trigger-fact="size"]')?.textContent).toContain('notify only')
    expect(card.querySelector('[data-trigger-fact="approval"]')).toBeNull()
    expect(card.querySelector('[data-trigger-fact="balance"]')).toBeNull()
  })

  it('lists at most the five newest fires and words each outcome', () => {
    const p = payload<TriggerOnePayload>(
      withTrigger('trigger-armed', (tr) => {
        tr.fires = [
          {
            n: 7,
            at: '2026-10-04T09:14:00Z',
            status: 'failed',
            reasonCode: 'trading.quote_failed',
          },
          {
            n: 6,
            at: '2026-10-04T09:10:00Z',
            status: 'skipped',
            reasonCode: 'insufficient_balance',
          },
          { n: 5, at: '2026-10-04T09:00:00Z', status: 'parked', priceUsd: 3790, orderId: 'ord_p' },
          { n: 4, at: '2026-10-04T08:00:00Z', status: 'pending', manual: true },
          { n: 3, at: '2026-10-04T07:00:00Z', status: 'failed', reason: 'quote: no route' },
          { n: 2, at: '2026-10-04T06:00:00Z', status: 'failed' },
        ]
      }),
    )
    const card = render(p)
    const rows = [...card.querySelectorAll<HTMLElement>('.trigger-fire')]
    expect(rows).toHaveLength(TRIGGER_RECENT_FIRES)
    expect(rows.map((r) => r.dataset.triggerFireStatus)).toEqual([
      'failed',
      'skipped',
      'parked',
      'pending',
      'failed',
    ])
    expect(card.querySelector('.trigger-fires__title')).toHaveTextContent('Recent fires')
    expect(rows[0]!.querySelector('.trigger-fire__detail')).toHaveTextContent(
      'trading.quote_failed',
    )
    expect(rows[1]!.querySelector('.trigger-fire__detail')).toHaveTextContent(
      'insufficient balance',
    )
    expect(rows[2]!.querySelector('.trigger-fire__status')).toHaveTextContent('awaiting approval')
    expect(rows[2]!.querySelector('.trigger-fire__detail')).toHaveTextContent('@ $3,790')
    // "@" and its price are one unit: a wrapping detail never splits them.
    expect(rows[2]!.querySelector('.trigger-fire__detail')?.textContent).toBe('@\u00a0$3,790')
    expect(rows[3]!.querySelector('.trigger-fire__manual')).not.toBeNull()
    expect(rows[4]!.querySelector('.trigger-fire__detail')).toHaveTextContent('quote: no route')
  })

  it('says "by you" for a pause the owner made', () => {
    const card = render(payload(inStatus('trigger-armed', 'paused', { statusReason: 'user' })))
    expect(card.querySelector('.trigger-card__reason')).toHaveTextContent('by you')
  })

  it('writes the warnings and the footer: id, wallet, as of', () => {
    const raw = fixture('trigger-armed')
    raw.warnings = ['WETH is already at $3,795, under $3,800: this fires after the next two checks']
    const p = payload<TriggerOnePayload>(raw)
    const card = render(p, ctx({ canRefresh: true }))
    expect(texts(card, '.trigger-card__warnings .trigger-card__warning')).toEqual(raw.warnings)
    const meta = card.querySelector('.trigger-card__foot-meta')!.textContent!
    expect(meta.startsWith(`${p.trigger.id} · `)).toBe(true)
    expect(meta).toMatch(/\(0x[0-9a-fA-F]{4}…[0-9a-fA-F]{4}\)/)
    expect(meta).toMatch(/as of /)
    expect(card.querySelector('[data-trigger-foot="refresh"]')).not.toBeNull()
    expect(card.querySelector('[data-trigger-foot="copy"]')).not.toBeNull()
    // The footer is last.
    expect(card.lastElementChild).toHaveClass('trigger-card__foot')
  })

  it('copies the trigger id and says so briefly', async () => {
    const copyText = vi.fn()
    const p = one('trigger-armed')
    const card = render(p, ctx({ copyText }))
    const button = card.querySelector<HTMLButtonElement>('[data-trigger-foot="copy"]')!
    button.click()
    await flush()
    expect(copyText).toHaveBeenCalledWith(p.trigger.id)
    expect(button.dataset.triggerCopied).toBe('true')
  })

  it('keeps attacker-chosen strings as text', () => {
    const evil = '<img src=x onerror=alert(1)>'
    const raw = withTrigger('trigger-done', (tr) => {
      tr.name = evil
      tr.token = { ...(tr.token as Json), symbol: evil }
      tr.statusReason = evil
      ;(tr.condition as Json).label = evil
      ;(tr.action as Json).label = evil
    })
    raw.warnings = [evil]
    const card = render(payload(raw), ctx({ canWrite: true, canRefresh: true }))
    expect(card.querySelector('img')).toBeNull()
    expect(card.querySelector('.trigger-card__name')?.textContent).toBe(evil)
    expect(card.querySelector('.trigger-card__hero-line')?.textContent).toContain(evil)
  })
})

describe('buildTriggerCard — triggers', () => {
  it('lists every trigger with its plan and live price', () => {
    const raw = fixture('triggers')
    const p = payload<TriggerListPayload>(raw)
    const card = render(p)
    expect(card.dataset.triggerKind).toBe('triggers')
    expect(card.querySelector('.trigger-card__list-title')).toHaveTextContent('Triggers')
    expect(card.querySelector('.trigger-card__count')).toHaveTextContent(String(p.totals.count))
    expect(card.querySelector('.trigger-totals')?.textContent).toMatch(
      /^\d+ (armed|awaiting|triggered)( · \d+ (armed|awaiting|triggered))*$/,
    )
    const rows = [...card.querySelectorAll<HTMLElement>('.trigger-rows .trigger-row')]
    expect(rows.map((r) => r.dataset.triggerId)).toEqual(p.triggers.map((tr) => tr.id))
    expect(rows.map((r) => r.dataset.triggerStatus)).toEqual(p.triggers.map((tr) => tr.status))
    rows.forEach((row, i) => {
      const tr = p.triggers[i]!
      expect(row.dataset.triggerAction).toBe(tr.kind)
      expect(row.querySelector('.trigger-row__name')).toHaveTextContent(tr.name)
      expect(row.querySelector('.trigger-row__plan')?.textContent).toBe(rowPlanText(tr))
      expect(row.querySelector('.trigger-row__plan')?.textContent).toMatch(
        /^(sell|buy|alert) .+ · .+$/,
      )
      expect(row.querySelector('.trigger-row__dot')).toHaveAttribute('aria-hidden', 'true')
    })
    // No ↻ without a read call, no copy id on a list.
    expect(card.querySelector('[data-trigger-foot]')).toBeNull()
    expect(card.querySelector('.trigger-actions')).toBeNull()
  })

  it('writes the row price line', () => {
    const tr = triggerWith(
      'trigger-armed',
      { direction: 'below', priceUsd: 3780, hits: 0 },
      { priceUsd: 3790, distancePct: -0.26 },
    )
    expect(rowNowText(tr)).toBe(`${tr.token.symbol} $3,790 · −0.3 %`)
    expect(rowNowText({ ...tr, status: 'triggered' })).toBe(
      `${tr.token.symbol} $3,790 · fired · order open`,
    )
    expect(rowPlanText({ ...tr, condition: { ...tr.condition, label: 'under $3,780' } })).toMatch(
      /^sell \d+ % \S+ · under \$3,780$/,
    )
  })

  it('offers compact controls per row on the desk', () => {
    const p = payload<TriggerListPayload>(fixture('triggers'))
    const card = render(p, ctx({ canWrite: true }))
    for (const tr of p.triggers) {
      const row = card.querySelector(`.trigger-row[data-trigger-id="${tr.id}"]`)!
      expect(
        [...row.querySelectorAll<HTMLElement>('[data-trigger-op]')].map((b) => b.dataset.triggerOp),
      ).toEqual(triggerActionsFor(tr.status))
      row.querySelectorAll<HTMLElement>('[data-trigger-op]').forEach((b) => {
        expect(b.dataset.triggerCompact).toBe('true')
      })
    }
  })

  it('says so when there are none', () => {
    const card = render(payload(fixture('triggers-empty')))
    expect(card.querySelector('.trigger-card__empty')).toHaveTextContent('No triggers yet.')
    expect(card.querySelector('.trigger-rows')).toBeNull()
    expect(card.querySelector('.trigger-totals')).toBeNull()
  })
})

/* ── controls ──────────────────────────────────────────────────────────── */

describe('controls', () => {
  it('offers the controls each status allows', () => {
    expect(triggerActionsFor('awaiting_approval')).toEqual(['approve', 'reject'])
    expect(triggerActionsFor('armed')).toEqual(['pause', 'fire', 'stop'])
    expect(triggerActionsFor('paused')).toEqual(['resume', 'fire', 'stop'])
    expect(triggerActionsFor('triggered')).toEqual(['stop'])
    for (const s of ['done', 'stopped', 'rejected', 'expired', 'unknown'] as const) {
      expect(triggerActionsFor(s)).toEqual([])
    }
  })

  it('draws each status with exactly its controls on the desk', () => {
    for (const status of [
      'awaiting_approval',
      'armed',
      'triggered',
      'paused',
      'done',
      'stopped',
      'rejected',
      'expired',
    ] as const) {
      const card = render(payload(inStatus('trigger-armed', status)), ctx({ canWrite: true }))
      expect(card.dataset.triggerStatus, status).toBe(status)
      expect(card.querySelector('.trigger-pill')?.getAttribute('data-status'), status).toBe(status)
      expect(
        [...card.querySelectorAll<HTMLElement>('.trigger-actions [data-trigger-op]')].map(
          (b) => b.dataset.triggerOp,
        ),
        status,
      ).toEqual(triggerActionsFor(status))
      expect(card.querySelector('.trigger-card__now')?.textContent, status).not.toBe('')
    }
  })

  it('renders no controls at all without canWrite', () => {
    for (const name of ['trigger-armed', 'trigger-awaiting', 'trigger-alert'] as const) {
      expect(render(one(name)).querySelector('.trigger-actions'), name).toBeNull()
    }
    expect(render(payload(fixture('triggers'))).querySelector('.trigger-actions')).toBeNull()
  })

  it('renders real buttons keyed by op and trigger id, worded for the kind', () => {
    const awaiting = one('trigger-awaiting')
    const card = render(awaiting, ctx({ canWrite: true }))
    const buttons = [
      ...card.querySelectorAll<HTMLButtonElement>('.trigger-actions [data-trigger-op]'),
    ]
    expect(
      buttons.map((b) => [
        b.tagName,
        b.type,
        b.dataset.triggerOp,
        b.dataset.triggerId,
        b.textContent,
      ]),
    ).toEqual([
      ['BUTTON', 'button', 'approve', awaiting.trigger.id, 'Approve & arm'],
      ['BUTTON', 'button', 'reject', awaiting.trigger.id, 'Reject'],
    ])
    expect(buttons[0]!.dataset.triggerTone).toBe('primary')
    expect(buttons[1]!.dataset.triggerTone).toBe('danger')
    const row = card.querySelector<HTMLElement>('.trigger-actions')!
    expect(row.dataset.triggerId).toBe(awaiting.trigger.id)
    const error = row.querySelector<HTMLElement>('.trigger-actions__error')!
    expect(error.hidden).toBe(true)
    expect(error).toHaveAttribute('role', 'alert')

    expect(
      texts(render(one('trigger-armed'), ctx({ canWrite: true })), '[data-trigger-op]'),
    ).toEqual(['Pause', 'Sell now', 'Stop'])
    expect(
      texts(render(one('trigger-alert'), ctx({ canWrite: true })), '[data-trigger-op]'),
    ).toEqual(['Pause', 'Fire now', 'Stop'])
    const buy = payload(inStatus('trigger-awaiting', 'armed'))
    expect(texts(render(buy, ctx({ canWrite: true })), '[data-trigger-op]')).toEqual([
      'Pause',
      'Buy now',
      'Stop',
    ])
  })

  it('maps an operator refusal to plain words, else shows the RPC message', () => {
    expect(triggerErrorText({ code: 'trading.operator_required', message: 'x' })).toBe(
      'Only the desktop app can do this.',
    )
    expect(
      triggerErrorText({ code: 'trading.trigger.bad_state', message: 'trigger is done' }),
    ).toBe('trigger is done')
    expect(triggerErrorText(new Error('boom'))).toBe('boom')
  })
})

function mountWith(
  body: Json,
  call: (method: string, params: Record<string, unknown>) => Promise<unknown>,
  extra: { onOrder?: (id: string) => void; read?: typeof call } = {},
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

describe('control → RPC → payload swap', () => {
  it('pauses: disables the row while in flight, then swaps in the answer', async () => {
    const answer = deferred<unknown>()
    const call = vi.fn(() => answer.promise)
    const { host, mounter } = mountWith(fixture('trigger-armed'), call)
    await flush()
    button(host, 'pause').click()
    expect(call).toHaveBeenCalledWith('trading.trigger.pause', { triggerId: idOf('trigger-armed') })
    const row = host.querySelector<HTMLElement>('.trigger-actions')!
    expect(row.dataset.triggerBusy).toBe('pause')
    expect(row).toHaveAttribute('aria-busy', 'true')
    expect(host.querySelector('.trigger-card')).toHaveAttribute('data-trigger-busy', 'true')
    expect(button(host, 'pause').dataset.triggerPending).toBe('true')
    expect(
      [...row.querySelectorAll('button')].every((b) => (b as HTMLButtonElement).disabled),
    ).toBe(true)
    // A second click while in flight does nothing.
    button(host, 'stop').click()
    button(host, 'stop').click()
    expect(call).toHaveBeenCalledTimes(1)

    answer.resolve(inStatus('trigger-armed', 'paused'))
    await flush()
    const card = host.querySelector<HTMLElement>('.trigger-card')!
    expect(card.dataset.triggerStatus).toBe('paused')
    expect(card).not.toHaveAttribute('data-trigger-busy')
    expect(texts(host, '.trigger-actions [data-trigger-op]')).toEqual([
      'Resume',
      'Sell now',
      'Stop',
    ])
    expect(button(host, 'resume').disabled).toBe(false)
    mounter.destroyAll()
  })

  it('approves a pending proposal', async () => {
    const call = vi.fn(() => Promise.resolve(inStatus('trigger-awaiting', 'armed')))
    const { host, mounter } = mountWith(fixture('trigger-awaiting'), call)
    await flush()
    button(host, 'approve').click()
    await flush()
    expect(call).toHaveBeenCalledWith('trading.trigger.approve', {
      triggerId: idOf('trigger-awaiting'),
    })
    expect(host.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'armed')
    mounter.destroyAll()
  })

  it('asks for a second click before firing, then hands the order to the desk', async () => {
    vi.useFakeTimers()
    const onOrder = vi.fn()
    const call = vi.fn(() => {
      const raw = inStatus('trigger-armed', 'triggered')
      raw.fire = {
        n: 1,
        at: '2026-10-04T09:15:01Z',
        status: 'pending',
        orderId: 'ord_fire_now',
        manual: true,
      }
      return Promise.resolve(raw)
    })
    const { host, mounter } = mountWith(fixture('trigger-armed'), call, { onOrder })
    await flush()
    const fire = button(host, 'fire')
    fire.click()
    expect(call).not.toHaveBeenCalled()
    expect(fire.dataset.triggerConfirm).toBe('true')
    expect(fire).toHaveTextContent('Sell now — click again')
    // Left alone, it disarms.
    vi.advanceTimersByTime(TRIGGER_CONFIRM_MS)
    expect(fire.dataset.triggerConfirm).toBeUndefined()
    expect(fire).toHaveTextContent('Sell now')

    fire.click()
    fire.click()
    await flush()
    expect(call).toHaveBeenCalledWith('trading.trigger.fire', { triggerId: idOf('trigger-armed') })
    expect(onOrder).toHaveBeenCalledWith('ord_fire_now')
    const card = host.querySelector<HTMLElement>('.trigger-card')!
    expect(card.dataset.triggerStatus).toBe('triggered')
    expect(texts(host, '.trigger-actions [data-trigger-op]')).toEqual(['Stop'])
    mounter.destroyAll()
  })

  it('asks for a second click before stopping', async () => {
    vi.useFakeTimers()
    const call = vi.fn(() => Promise.resolve(inStatus('trigger-armed', 'stopped')))
    const { host, mounter } = mountWith(fixture('trigger-armed'), call)
    await flush()
    const stop = button(host, 'stop')
    stop.click()
    expect(call).not.toHaveBeenCalled()
    expect(stop).toHaveTextContent('Stop — click again')
    stop.click()
    await flush()
    expect(call).toHaveBeenCalledWith('trading.trigger.stop', { triggerId: idOf('trigger-armed') })
    const card = host.querySelector<HTMLElement>('.trigger-card')!
    expect(card.dataset.triggerStatus).toBe('stopped')
    expect(card.querySelector('.trigger-actions')).toBeNull()
    mounter.destroyAll()
  })

  it('shows a refusal inline and frees the buttons', async () => {
    const call = vi.fn(() =>
      Promise.reject(Object.assign(new Error('wallet is locked'), { code: 'wallet.locked' })),
    )
    const { host, mounter } = mountWith(fixture('trigger-armed'), call)
    await flush()
    button(host, 'pause').click()
    await flush()
    const error = host.querySelector<HTMLElement>('.trigger-actions__error')!
    expect(error.hidden).toBe(false)
    expect(error).toHaveTextContent('wallet is locked')
    expect(host.querySelector('.trigger-actions')).not.toHaveAttribute('data-trigger-busy')
    expect(button(host, 'pause').disabled).toBe(false)
    expect(host.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'armed')
    mounter.destroyAll()
  })

  it('treats an answer that is not a trigger payload as an error', async () => {
    const call = vi.fn(() => Promise.resolve({ ok: true }))
    const { host, mounter } = mountWith(fixture('trigger-armed'), call)
    await flush()
    button(host, 'pause').click()
    await flush()
    expect(host.querySelector('.trigger-actions__error')).toHaveTextContent(
      'Trigger data could not be read.',
    )
    mounter.destroyAll()
  })

  it('swaps a row of a list card in place', async () => {
    const list = payload<TriggerListPayload>(fixture('triggers'))
    const target = list.triggers.find((tr) => tr.status === 'armed')!
    const call = vi.fn(() =>
      Promise.resolve({
        kind: 'trigger',
        fetchedAt: '2026-10-04T09:16:00Z',
        trigger: {
          ...(fixture('triggers').triggers as Json[]).find((tr) => tr.id === target.id),
          status: 'paused',
          updatedAt: '2026-12-31T00:00:00Z',
        },
      }),
    )
    const { host, mounter } = mountWith(fixture('triggers'), call)
    await flush()
    const armedBefore = list.triggers.filter((tr) => tr.status === 'armed').length
    expect(host.querySelector('.trigger-totals')?.textContent).toContain(`${armedBefore} armed`)
    host
      .querySelector<HTMLButtonElement>(
        `.trigger-row[data-trigger-id="${target.id}"] [data-trigger-op="pause"]`,
      )!
      .click()
    await flush()
    const card = host.querySelector<HTMLElement>('.trigger-card')!
    expect(card.dataset.triggerKind).toBe('triggers')
    expect(
      [...card.querySelectorAll<HTMLElement>('.trigger-row')].map((r) => r.dataset.triggerStatus),
    ).toEqual(list.triggers.map((tr) => (tr.id === target.id ? 'paused' : tr.status)))
    // The header follows the rows: one armed fewer, the count unchanged.
    const totals = card.querySelector('.trigger-totals')?.textContent ?? ''
    if (armedBefore > 1) expect(totals).toContain(`${armedBefore - 1} armed`)
    else expect(totals).not.toMatch(/armed/)
    expect(card.querySelector('.trigger-card__count')).toHaveTextContent(String(list.totals.count))
    mounter.destroyAll()
  })

  it('reads the actions getter at click time', async () => {
    let actions: { call: typeof call } | null = null
    const call = vi.fn(() => Promise.resolve(inStatus('trigger-armed', 'paused')))
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('trigger-armed')),
      actions: () => actions,
    })
    mounter.mountTrigger(document.body)
    await flush()
    expect(host.querySelector('.trigger-actions')).toBeNull()
    actions = { call }
    mounter.triggerChanged(fixture('trigger-armed'))
    button(host, 'pause').click()
    await flush()
    expect(call).toHaveBeenCalledTimes(1)
    mounter.destroyAll()
  })
})

/* ── refresh + events ──────────────────────────────────────────────────── */

describe('refresh and events', () => {
  it('↻ re-runs the echoed read and redraws', async () => {
    const read = vi.fn(() => Promise.resolve(inStatus('trigger-armed', 'paused')))
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('trigger-armed')),
      call: read,
      now: () => NOW,
    })
    mounter.mountTrigger(document.body)
    await flush()
    read.mockClear()
    host.querySelector<HTMLButtonElement>('[data-trigger-foot="refresh"]')!.click()
    expect(host.querySelector('.trigger-card')).toHaveAttribute('data-trigger-refreshing', 'true')
    await flush()
    expect(read).toHaveBeenCalledWith('trading.trigger.get', { triggerId: idOf('trigger-armed') })
    expect(host.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'paused')
    expect(host.querySelector('.trigger-card')).not.toHaveAttribute('data-trigger-refreshing')
    mounter.destroyAll()
  })

  it('says why a ↻ failed, under the footer', async () => {
    const read = vi.fn((): Promise<unknown> => Promise.resolve(fixture('triggers')))
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('triggers')),
      call: read,
    })
    mounter.mountTrigger(document.body)
    await flush()
    read.mockImplementationOnce(() => Promise.reject(new Error('gateway down')))
    host.querySelector<HTMLButtonElement>('[data-trigger-foot="refresh"]')!.click()
    await flush()
    expect(read).toHaveBeenLastCalledWith(
      'trading.trigger.list',
      (fixture('triggers').request as Json).params,
    )
    expect(host.querySelector('.trigger-card__refresh-error')).toHaveTextContent(
      'refresh failed: gateway down',
    )
    expect(host.querySelector<HTMLButtonElement>('[data-trigger-foot="refresh"]')!.disabled).toBe(
      false,
    )
    mounter.destroyAll()
  })

  it('swaps a changed trigger into every card that draws it', async () => {
    const list = payload<TriggerListPayload>(fixture('triggers'))
    const armedId = idOf('trigger-armed')
    expect(list.triggers.some((tr) => tr.id === armedId)).toBe(true)
    const card = placeholder('/a')
    const listHost = placeholder('/b')
    const other = placeholder('/c')
    const mounter = createTriggerMounter({
      fetchPayload: (url) =>
        Promise.resolve(
          fixture(url === '/a' ? 'trigger-armed' : url === '/b' ? 'triggers' : 'trigger-done'),
        ),
    })
    mounter.mountTrigger(document.body)
    await flush()
    // The engine's event body: the bare trigger under `trigger`.
    const paused = inStatus('trigger-armed', 'paused').trigger as Json
    mounter.triggerChanged({ trigger: paused })
    expect(card.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'paused')
    expect(listHost.querySelector(`.trigger-row[data-trigger-id="${armedId}"]`)).toHaveAttribute(
      'data-trigger-status',
      'paused',
    )
    expect(other.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'done')
    // The full payload works too.
    mounter.triggerChanged(inStatus('trigger-armed', 'stopped'))
    expect(card.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'stopped')
    mounter.triggerChanged({ nothing: true })
    mounter.triggerChanged(null)
    expect(card.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'stopped')
    mounter.destroyAll()
  })

  it('reads the three event body shapes', () => {
    const full = inStatus('trigger-armed', 'paused')
    expect(triggerChangedPayload(full)?.payload?.trigger.status).toBe('paused')
    expect(triggerChangedPayload({ trigger: full })?.payload?.request?.kind).toBe('get')
    const bare = triggerChangedPayload({ trigger: full.trigger })
    expect(bare?.trigger.id).toBe(idOf('trigger-armed'))
    expect(bare?.payload).toBeNull()
    expect(triggerChangedPayload({ trigger: 'x' })).toBeNull()
  })

  it('re-reads a card when one of its orders settles', async () => {
    const read = vi.fn(() => Promise.resolve(fixture('trigger-done')))
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('trigger-done')),
      call: read,
    })
    mounter.mountTrigger(document.body)
    await flush()
    // The mount's own live read.
    expect(read).toHaveBeenCalledTimes(1)
    read.mockClear()
    mounter.orderFinished('ord_unrelated')
    expect(read).not.toHaveBeenCalled()
    const orderId = one('trigger-done').trigger.fires[0]!.orderId!
    mounter.orderFinished(orderId)
    await flush()
    expect(read).toHaveBeenCalledWith('trading.trigger.get', { triggerId: idOf('trigger-done') })
    // An order the card has never seen, but which names the trigger.
    read.mockClear()
    mounter.orderFinished('ord_new', idOf('trigger-done'))
    await flush()
    expect(read).toHaveBeenCalledTimes(1)
    expect(host.querySelector('.trigger-card')).not.toBeNull()
    mounter.destroyAll()
  })

  it('ticks "checked N s ago" on the live line', async () => {
    vi.useFakeTimers()
    let clock = Date.parse('2026-10-04T09:15:00Z')
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () =>
        Promise.resolve(
          withTrigger('trigger-armed', (tr) => {
            tr.condition = { ...(tr.condition as Json), hits: 0 }
            tr.market = { ...(tr.market as Json), checkedAt: '2026-10-04T09:14:48Z' }
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
    clock += 60_000
    vi.advanceTimersByTime(60_000)
    expect(line()).toMatch(/checked 1 m ago$/)
    mounter.destroyAll()
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('live state over the artifact snapshot', () => {
  it('re-reads on mount; the snapshot is a placeholder with its controls off', async () => {
    const answer = deferred<unknown>()
    const read = vi.fn(() => answer.promise)
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('trigger-awaiting')),
      call: read,
      actions: { call: vi.fn() },
      now: () => NOW,
    })
    mounter.mountTrigger(document.body)
    await flush()
    expect(read).toHaveBeenCalledWith('trading.trigger.get', {
      triggerId: idOf('trigger-awaiting'),
    })
    const snapshot = host.querySelector<HTMLElement>('.trigger-card')!
    expect(snapshot.dataset.triggerStatus).toBe('awaiting_approval')
    expect(snapshot.dataset.triggerStale).toBe('checking')
    // The mount's read is quiet: no dimming, no hint yet.
    expect(snapshot).not.toHaveAttribute('data-trigger-refreshing')
    expect(snapshot.querySelector('.trigger-card__stale')).toBeNull()
    expect(button(host, 'approve').disabled).toBe(true)
    expect(button(host, 'reject').disabled).toBe(true)

    answer.resolve(inStatus('trigger-awaiting', 'rejected'))
    await flush()
    const card = host.querySelector<HTMLElement>('.trigger-card')!
    expect(card.dataset.triggerStatus).toBe('rejected')
    expect(card).not.toHaveAttribute('data-trigger-stale')
    expect(card.querySelector('.trigger-actions')).toBeNull()
    mounter.destroyAll()
  })

  it('keeps the snapshot with its controls off and says so when the read fails', async () => {
    const read = vi.fn((): Promise<unknown> => Promise.reject(new Error('gateway down')))
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('trigger-awaiting')),
      call: read,
      actions: { call: vi.fn() },
      now: () => NOW,
    })
    mounter.mountTrigger(document.body)
    await flush()
    const card = host.querySelector<HTMLElement>('.trigger-card')!
    expect(card.dataset.triggerStale).toBe('failed')
    expect(button(host, 'approve').disabled).toBe(true)
    const hint = card.querySelector<HTMLElement>('.trigger-card__stale')!
    expect(hint).toHaveTextContent('state may be stale · ↻')
    expect(hint.nextElementSibling).toHaveClass('trigger-card__foot')
    expect(card.querySelector('.trigger-card__refresh-error')).toBeNull()

    // The hint's ↻ re-reads; a success lifts the stale state.
    read.mockImplementationOnce(() => Promise.resolve(inStatus('trigger-awaiting', 'armed')))
    hint.querySelector<HTMLButtonElement>('.trigger-card__stale-refresh')!.click()
    await flush()
    const live = host.querySelector<HTMLElement>('.trigger-card')!
    expect(live.dataset.triggerStatus).toBe('armed')
    expect(live).not.toHaveAttribute('data-trigger-stale')
    expect(live.querySelector('.trigger-card__stale')).toBeNull()
    expect(button(host, 'pause').disabled).toBe(false)
    mounter.destroyAll()
  })

  it('draws a re-mounted card from the newest state it has seen, then re-reads it', async () => {
    const read = vi.fn((): Promise<unknown> =>
      Promise.resolve(inStatus('trigger-awaiting', 'armed')),
    )
    const first = placeholder('/a')
    const mounter = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('trigger-awaiting')),
      call: read,
      actions: { call: vi.fn() },
      now: () => NOW,
    })
    mounter.mountTrigger(document.body)
    await flush()
    expect(first.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'armed')

    // The transcript re-renders: a new placeholder for the same artifact.
    first.remove()
    const later = deferred<unknown>()
    read.mockImplementationOnce(() => later.promise)
    const second = placeholder('/a')
    mounter.mountTrigger(document.body)
    await flush()
    const card = second.querySelector<HTMLElement>('.trigger-card')!
    expect(card.dataset.triggerStatus).toBe('armed')
    expect(card).not.toHaveAttribute('data-trigger-stale')
    expect(button(second, 'pause').disabled).toBe(false)
    expect(read).toHaveBeenCalledTimes(2)
    later.resolve(inStatus('trigger-awaiting', 'done', { updatedAt: '2027-01-01T00:00:00Z' }))
    await flush()
    expect(second.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'done')

    // `trading.trigger.changed` feeds the cache too, and the cache outlives
    // destroyAll: the same mounter re-mounts from it.
    mounter.triggerChanged(
      inStatus('trigger-awaiting', 'stopped', { updatedAt: '2027-02-01T00:00:00Z' }),
    )
    mounter.destroyAll()
    second.remove()
    read.mockImplementation(() => new Promise(() => {}))
    const third = placeholder('/a')
    mounter.mountTrigger(document.body)
    await flush()
    expect(third.querySelector('.trigger-card')).toHaveAttribute('data-trigger-status', 'stopped')
    expect(third.querySelector('.trigger-card')).not.toHaveAttribute('data-trigger-stale')
    mounter.dispose()

    // A fresh mounter has no cache: the snapshot, controls off, re-reading.
    third.remove()
    const fourth = placeholder('/a')
    const fresh = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('trigger-awaiting')),
      call: read,
      now: () => NOW,
    })
    fresh.mountTrigger(document.body)
    await flush()
    expect(fourth.querySelector('.trigger-card')).toHaveAttribute(
      'data-trigger-status',
      'awaiting_approval',
    )
    expect(fourth.querySelector('.trigger-card')).toHaveAttribute('data-trigger-stale', 'checking')
    fresh.destroyAll()
  })

  it('re-reads the card after a control is refused as out of date', async () => {
    const act = vi.fn(() =>
      Promise.reject(
        Object.assign(new Error('cannot approve: it is expired'), {
          code: 'trading.trigger.bad_state',
        }),
      ),
    )
    const answers = [fixture('trigger-awaiting'), inStatus('trigger-awaiting', 'expired')]
    const read = vi.fn(() => Promise.resolve(answers.shift()))
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('trigger-awaiting')),
      call: read,
      actions: { call: act },
      now: () => NOW,
    })
    mounter.mountTrigger(document.body)
    await flush()
    expect(button(host, 'approve').disabled).toBe(false)
    button(host, 'approve').click()
    await flush()
    expect(act).toHaveBeenCalledWith('trading.trigger.approve', {
      triggerId: idOf('trigger-awaiting'),
    })
    expect(read).toHaveBeenCalledTimes(2)
    const card = host.querySelector<HTMLElement>('.trigger-card')!
    expect(card.dataset.triggerStatus).toBe('expired')
    expect(card).not.toHaveAttribute('data-trigger-stale')
    expect(card.querySelector('.trigger-actions')).toBeNull()
    mounter.destroyAll()
  })

  it('also re-reads after not_found, and leaves other refusals alone', async () => {
    const codes = ['trading.trigger.not_found', 'trading.failed']
    const act = vi.fn(() =>
      Promise.reject(Object.assign(new Error('nope'), { code: codes.shift() })),
    )
    const read = vi.fn(() => Promise.resolve(fixture('trigger-armed')))
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('trigger-armed')),
      call: read,
      actions: { call: act },
      now: () => NOW,
    })
    mounter.mountTrigger(document.body)
    await flush()
    button(host, 'pause').click()
    await flush()
    expect(read).toHaveBeenCalledTimes(2)
    button(host, 'pause').click()
    await flush()
    expect(read).toHaveBeenCalledTimes(2)
    expect(host.querySelector('.trigger-actions__error')).toHaveTextContent('nope')
    mounter.destroyAll()
  })
})

/* ── mounter plumbing ──────────────────────────────────────────────────── */

describe('createTriggerMounter', () => {
  it('renders into the placeholder once and hides the loading line', async () => {
    const host = placeholder()
    const fetchPayload = vi.fn(() => Promise.resolve(fixture('trigger-armed')))
    const mounter = createTriggerMounter({ fetchPayload })
    mounter.mountTrigger(document.body)
    mounter.mountTrigger(document.body)
    await flush()
    expect(fetchPayload).toHaveBeenCalledTimes(1)
    expect(fetchPayload).toHaveBeenCalledWith('/api/v1/artifacts/trg-1')
    expect(host.querySelectorAll('.trigger-card')).toHaveLength(1)
    expect(host.dataset.triggerHost).toBe('rendered')
    expect(host.querySelector<HTMLElement>('.msg-artifact-trigger__status')!.hidden).toBe(true)
    // No read call → no ↻, no stale state.
    expect(host.querySelector('[data-trigger-foot="refresh"]')).toBeNull()
    expect(host.querySelector('.trigger-card')).not.toHaveAttribute('data-trigger-stale')
    mounter.destroyAll()
  })

  it('says why a card could not be drawn', async () => {
    const bad = placeholder('/a')
    const broken = placeholder('/b')
    const empty = placeholder('')
    const mounter = createTriggerMounter({
      fetchPayload: (url) =>
        url === '/a' ? Promise.resolve({ kind: 'nope' }) : Promise.reject(new Error('HTTP 500')),
    })
    mounter.mountTrigger(document.body)
    await flush()
    expect(bad.querySelector('.msg-artifact-trigger__status')).toHaveTextContent(
      'Trigger data could not be read.',
    )
    expect(broken.querySelector('.msg-artifact-trigger__status')).toHaveTextContent(
      'Trigger card failed to load.',
    )
    expect(empty.querySelector('.msg-artifact-trigger__status')).toHaveTextContent(
      'Trigger data is unavailable.',
    )
  })

  it('does not render into a host that left the document mid-fetch', async () => {
    const host = placeholder()
    const answer = deferred<unknown>()
    const mounter = createTriggerMounter({ fetchPayload: () => answer.promise })
    mounter.mountTrigger(document.body)
    host.remove()
    answer.resolve(fixture('trigger-armed'))
    await flush()
    expect(host.querySelector('.trigger-card')).toBeNull()
  })

  it('clears pending copy and confirm resets and the clock on destroyAll', async () => {
    vi.useFakeTimers()
    const host = placeholder()
    const mounter = createTriggerMounter({
      fetchPayload: () => Promise.resolve(fixture('trigger-armed')),
      actions: { call: vi.fn() },
      copyText: () => {},
    })
    mounter.mountTrigger(document.body)
    await flush()
    host.querySelector<HTMLButtonElement>('[data-trigger-foot="copy"]')!.click()
    button(host, 'stop').click()
    await flush()
    expect(vi.getTimerCount()).toBe(3)
    mounter.destroyAll()
    expect(vi.getTimerCount()).toBe(0)
  })
})

/* ── measured layout ───────────────────────────────────────────────────── */

describe('layout', () => {
  it('goes wide from TRIGGER_WIDE_MIN_PX', () => {
    expect(triggerLayoutFor(TRIGGER_WIDE_MIN_PX)).toBe('wide')
    expect(triggerLayoutFor(TRIGGER_WIDE_MIN_PX - 1)).toBe('narrow')
  })

  it('stamps data-trigger-layout from the measured width, leaving an unmeasured card alone', () => {
    const card = render(one('trigger-armed'))
    layoutTriggerCard(card)
    expect(card.dataset.triggerLayout).toBe('narrow')
    Object.defineProperty(card, 'offsetWidth', { configurable: true, value: 640 })
    layoutTriggerCard(card)
    expect(card.dataset.triggerLayout).toBe('wide')
  })
})

/* ── every engine fixture ──────────────────────────────────────────────── */

const engineFixtures = existsSync(FIXTURES)
  ? readdirSync(FIXTURES).filter((name) => name.endsWith('.json'))
  : []

describe('engine fixtures', () => {
  it('ships the six the contract names', () => {
    expect([...engineFixtures].sort()).toEqual(
      expect.arrayContaining(NAMES.map((n) => `${n}.json`)),
    )
  })

  it.each(engineFixtures)('%s normalizes and renders', (name) => {
    const raw = JSON.parse(readFileSync(`${FIXTURES}/${name}`, 'utf8')) as Json
    const p = normalizeTriggerPayload(raw)
    expect(p, name).not.toBeNull()
    expect(p!.kind).toBe(raw.kind)
    const card = render(p!, ctx({ canWrite: true, canRefresh: true }))
    expect(card.dataset.triggerKind).toBe(raw.kind)
    expect(card.querySelector('.trigger-card__foot')).not.toBeNull()
    if (p!.kind === 'trigger') {
      const tr = raw.trigger as Json
      expect(card.dataset.triggerStatus).toBe(tr.status)
      expect(card.dataset.triggerAction).toBe(tr.kind)
      expect(card.querySelector('.trigger-card__hero-line')?.textContent).not.toBe('')
      expect(
        [...card.querySelectorAll<HTMLElement>('.trigger-actions [data-trigger-op]')].map(
          (b) => b.dataset.triggerOp,
        ),
      ).toEqual(triggerActionsFor(p!.trigger.status))
    } else if (p!.kind === 'bracket') {
      // docs/brackets.md; bracket.test.ts covers the card itself.
      expect(card.dataset.triggerStatus).toBe((raw.bracket as Json).status)
      expect(card.querySelectorAll('.bracket-leg')).toHaveLength(2)
    } else {
      const rows = (raw.kind === 'brackets' ? raw.brackets : raw.triggers) as unknown[]
      expect(card.querySelectorAll('.trigger-row')).toHaveLength(rows.length)
    }
  })
})
