import { describe, expect, it, vi } from 'vitest'
import { bridgeOrder, order, WALLET } from '../test-utils'
import {
  watchTabStrip,
  approvalFacts,
  askRisk,
  batchFacts,
  bookConcession,
  BOOK_MIN,
  CHAT_MIN,
  composeSendPrompt,
  groupAsks,
  parseRecipientLines,
  SEND_TAG,
  validateSend,
  type SendForm,
  alreadyDecided,
  batchIdsOf,
  formatExpiryShort,
  missionStopDue,
  missionStopRule,
  orderKindWord,
  orderLine,
  untilEpoch,
  withBatchLegs,
  composeMissionPrompt,
  composerPlaceholder,
  formatExpiryWhole,
  isDryRunText,
  isDeskMission,
  isSessionMission,
  MISSION_COMPLETE_MARKER,
  missionCronPayload,
  missionFromJob,
  missionPrefill,
  missionStatus,
  needsFullOutput,
  ordersForSession,
  PLACEHOLDERS,
  rejectionMessage,
  riskStamp,
  runSaysComplete,
  statusWord,
  tradingProjectKnowledge,
  validateMission,
  withoutDryRun,
} from './desk-logic'

describe('tradingProjectKnowledge', () => {
  const second = {
    ...WALLET,
    address: '0x2222222222222222222222222222222222222222',
    label: '10k',
    primary: false,
  }

  it('names the primary as the order wallet and the others as name-only', () => {
    const text = tradingProjectKnowledge({
      wallets: [WALLET, second],
      chains: [8453],
      limits: null,
    })
    expect(text).toContain(`Primary wallet: Main (${WALLET.address}). Every order comes from it`)
    expect(text).toMatch(/do not read or report any other wallet's balance/)
    expect(text).toContain(`Other wallets, only when the user names them: 10k (${second.address}).`)
    expect(text).toContain('Chains: Base.')
    // The primary is not listed twice, and never as a peer of the others.
    expect(text.indexOf(WALLET.address)).toBe(text.lastIndexOf(WALLET.address))
  })

  it('falls back to the first wallet when none is marked primary, and says so when there are none', () => {
    const alone = tradingProjectKnowledge({ wallets: [second], chains: [], limits: null })
    expect(alone).toContain(`Primary wallet: 10k (${second.address})`)
    expect(alone).not.toContain('Other wallets')
    expect(tradingProjectKnowledge({ wallets: [], chains: [], limits: null })).toContain(
      'Wallets: none yet.',
    )
  })

  it('carries the engine limits', () => {
    const text = tradingProjectKnowledge({
      wallets: [WALLET],
      chains: [8453],
      limits: { thresholdUsd: 100, dailyCapUsd: 1000 },
    })
    expect(text).toContain(
      "Orders above $100.00 wait for the user's approval; $1,000.00 per wallet per day.",
    )
  })
})

describe('statusWord', () => {
  it('ranks awaiting over running over idle', () => {
    expect(statusWord({ pendingApprovals: 1, missionRunning: true })).toBe('awaiting')
    expect(statusWord({ pendingApprovals: 0, missionRunning: true })).toBe('running')
    expect(statusWord({ pendingApprovals: 0, missionRunning: false })).toBe('idle')
  })
})

describe('riskStamp', () => {
  it('is high from 500 USD or 3% impact', () => {
    expect(riskStamp({ valueUsd: 499, priceImpactPct: 0.5 })).toBe('normal')
    expect(riskStamp({ valueUsd: 500, priceImpactPct: 0.5 })).toBe('high')
    expect(riskStamp({ valueUsd: 10, priceImpactPct: 3 })).toBe('high')
    expect(riskStamp({ valueUsd: null, priceImpactPct: null })).toBe('normal')
  })
})

describe('approvalFacts', () => {
  const labels = { wallet: 'Wallet', pay: 'Pay', expires: 'Expires', gas: 'Gas' }
  it('binds the order values and omits what is absent', () => {
    const facts = approvalFacts(
      order({ gasUsd: null, priceImpactPct: null, expiresAt: null }),
      [WALLET],
      labels,
    )
    const keys = facts.map((f) => f.key)
    expect(keys).toContain('wallet')
    expect(keys).toContain('pay')
    expect(keys).not.toContain('gas')
    expect(keys).not.toContain('impact')
    expect(keys).not.toContain('expires')
    expect(facts.find((f) => f.key === 'wallet')?.value).toBe('Main · 0x1111…1111')
    expect(facts.find((f) => f.key === 'pay')?.value).toBe('0.2 ETH')
    expect(facts.find((f) => f.key === 'wallet')?.label).toBe('Wallet')
  })
  it('prints the expiry whole with a UTC offset, never a countdown', () => {
    const facts = approvalFacts(order({ expiresAt: Date.UTC(2026, 8, 13, 9, 30) }), [], labels)
    const expires = facts.find((f) => f.key === 'expires')?.value ?? ''
    expect(expires).toMatch(/\(UTC[+−]\d/)
    expect(expires).not.toMatch(/in \d/)
    expect(formatExpiryWhole(Date.UTC(2026, 0, 1), 'en-US')).toMatch(/2026/)
  })
  it('tones a heavy price impact', () => {
    const facts = approvalFacts(order({ priceImpactPct: 4 }), [], labels)
    expect(facts.find((f) => f.key === 'impact')?.tone).toBe('danger')
  })
})

describe('ordersForSession / rejectionMessage', () => {
  it('keeps only this chat’s orders', () => {
    const mine = order({ orderId: 'a', sessionKey: 's1' })
    const other = order({ orderId: 'b', sessionKey: 's2' })
    expect(ordersForSession([mine, other], 's1')).toEqual([mine])
  })
  it('writes the reason the agent reads', () => {
    expect(rejectionMessage({ orderId: 'o9' }, ' too much slippage ')).toBe(
      'Rejected order o9: too much slippage',
    )
    expect(rejectionMessage({ orderId: 'o9' }, '')).toContain('Do not retry')
  })
})

describe('missions', () => {
  it('prefills the four chips and validates the contract', () => {
    const dca = missionPrefill('dca', { primary: WALLET.address })
    expect(dca.interval).toEqual({ kind: 'every', seconds: 86_400 })
    expect(dca.wallets).toEqual([WALLET.address])
    expect(validateMission(dca).ok).toBe(true)
    expect(validateMission({ ...dca, name: '' })).toEqual({ ok: false, error: 'name' })
    expect(validateMission({ ...dca, goal: '  ' })).toEqual({ ok: false, error: 'goal' })
    expect(validateMission({ ...dca, budgetTotalUsd: '-1' })).toEqual({
      ok: false,
      error: 'budget',
    })
    expect(validateMission({ ...dca, interval: { kind: 'every', seconds: 30 } })).toEqual({
      ok: false,
      error: 'interval',
    })
    expect(validateMission({ ...dca, stop: { kind: 'runs', runs: 0 } })).toEqual({
      ok: false,
      error: 'stop',
    })
  })

  it('composes a deterministic prompt with budget, cadence, stop and dry run', () => {
    const form = missionPrefill('dip', { primary: WALLET.address })
    const prompt = composeMissionPrompt(form, {
      wallets: [WALLET],
      limits: { thresholdUsd: 100, dailyCapUsd: 1000 },
    })
    expect(prompt.split('\n')[0]).toBe('[Trading desk mission] Buy the dip')
    expect(prompt).toContain('Wallets: Main · 0x1111…1111 · Chains: Base')
    expect(prompt).toContain('at most $100.00 in total')
    expect(prompt).toContain('at most $25.00 per order')
    expect(prompt).toContain('orders above $100.00 wait for approval')
    expect(prompt).toContain('every 5 min')
    expect(prompt).toContain(MISSION_COMPLETE_MARKER)
    // `--wait-seconds` without `--wait` returns at once; the prompt says both.
    expect(prompt).toContain('`agentos trade order <id> --wait --wait-seconds 600 --json`')
    expect(prompt).not.toMatch(/order <id> --wait-seconds/)
    // A budgeted mission counts what it already spent from the ledger, by note prefix.
    expect(prompt).toContain(
      'Before any order in a mission with a budget: `agentos trade orders --json`, sum `valueUsd` of `confirmed` orders whose `note` starts with `Buy the dip:`;',
    )
    expect(prompt).toContain(
      `if that sum plus this order would exceed the total budget, do nothing and reply \`${MISSION_COMPLETE_MARKER}\`. Every order's \`--note\` starts with \`Buy the dip:\`.`,
    )
    // No budget, no bookkeeping clause.
    const free = composeMissionPrompt(
      { ...form, budgetTotalUsd: '', budgetPerOrderUsd: '' },
      { wallets: [WALLET], limits: null },
    )
    expect(free).not.toContain('Budget:')
    expect(free).not.toContain('Before any order in a mission with a budget')
    expect(isDryRunText(prompt)).toBe(true)
    const stripped = withoutDryRun(prompt)
    expect(isDryRunText(stripped)).toBe(false)
    expect(stripped).toContain('Goal:')
    expect(
      composeMissionPrompt(form, {
        wallets: [WALLET],
        limits: { thresholdUsd: 100, dailyCapUsd: 1000 },
      }),
    ).toBe(prompt)
  })

  it('builds the cron.add payload that posts into this chat', () => {
    const form = missionPrefill('dca', { primary: WALLET.address })
    form.name = 'DCA ETH'
    const payload = missionCronPayload(
      form,
      'PROMPT',
      'agent:trading:webchat:trading-x',
      'Asia/Tokyo',
    )
    expect(payload).toMatchObject({
      name: 'DCA ETH',
      enabled: true,
      payloadKind: 'agent_turn',
      agentId: 'trading',
      sessionTarget: 'current',
      targetSessionKey: 'agent:trading:webchat:trading-x',
      text: 'PROMPT',
      schedule: { kind: 'every', every_seconds: 86_400 },
      tz: 'Asia/Tokyo',
    })
    form.interval = { kind: 'cron', expr: '0 9 * * *' }
    expect(missionCronPayload(form, 'P', 's').schedule).toEqual({ kind: 'cron', expr: '0 9 * * *' })
  })

  it('matches jobs to the session by any of the target keys', () => {
    expect(isSessionMission({ targetSessionKey: 's' }, 's')).toBe(true)
    expect(isSessionMission({ target_session_key: 's' }, 's')).toBe(true)
    expect(isSessionMission({ sessionKey: 'other' }, 's')).toBe(false)
    expect(isSessionMission({ targetSessionKey: 's' }, '')).toBe(false)
    // A desk lists its own missions, and the enabled ones of other trading chats.
    const other = 'agent:trading:webchat:trading-x'
    expect(isDeskMission({ targetSessionKey: 's', enabled: false }, 's')).toBe(true)
    expect(isDeskMission({ targetSessionKey: other }, 's')).toBe(true)
    expect(isDeskMission({ targetSessionKey: other, enabled: false }, 's')).toBe(false)
    expect(isDeskMission({ targetSessionKey: 'agent:main:webchat:x' }, 's')).toBe(false)
    expect(isDeskMission({}, 's')).toBe(false)
  })

  it('reads a mission state from the job and the live signals', () => {
    const next = new Date(Date.now() + 600_000).toISOString()
    expect(
      missionStatus({ enabled: true, next_run: next }, { running: true, pendingApprovals: 0 })
        .state,
    ).toBe('running')
    expect(
      missionStatus({ enabled: true, next_run: next }, { running: false, pendingApprovals: 2 })
        .state,
    ).toBe('awaiting')
    const sleeping = missionStatus(
      { enabled: true, next_run: next },
      { running: false, pendingApprovals: 0 },
    )
    expect(sleeping.state).toBe('sleeping')
    expect(sleeping.until).toBeGreaterThan(Date.now())
    expect(missionStatus({ enabled: false }, { running: false, pendingApprovals: 0 }).state).toBe(
      'paused',
    )
    expect(
      missionStatus({ enabled: true, next_run: null }, { running: false, pendingApprovals: 0 })
        .state,
    ).toBe('done')
  })

  // `cron.list` never sends `last_status`; reading it made `failed`
  // unreachable, so a mission that errored every run read as sleeping.
  it('reads failure from the counters the gateway actually sends', () => {
    const next = new Date(Date.now() + 60_000).toISOString()
    const ctx = { running: false, pendingApprovals: 0 }
    expect(missionStatus({ enabled: true, next_run: next, consecutive_errors: 2 }, ctx).state).toBe(
      'failed',
    )
    expect(missionStatus({ enabled: true, next_run: next, lastResult: 'boom' }, ctx).state).toBe(
      'failed',
    )
    expect(missionStatus({ enabled: true, next_run: next, status: 'failed' }, ctx).state).toBe(
      'failed',
    )
    // Disabled after failing reads as failed, not as a plain pause.
    expect(missionStatus({ enabled: false, lastResult: 'boom' }, ctx).state).toBe('failed')
    // A success clears both, so the job is back to sleeping.
    expect(
      missionStatus({ enabled: true, next_run: next, consecutive_errors: 0, lastResult: null }, ctx)
        .state,
    ).toBe('sleeping')
  })

  it('needs the full output only when the preview could hide the marker', () => {
    expect(runSaysComplete(`all done\n${MISSION_COMPLETE_MARKER}`)).toBe(true)
    expect(runSaysComplete('nothing to do this run')).toBe(false)
    expect(runSaysComplete(undefined)).toBe(false)
    // Short and marker-free: settled, no second call.
    expect(needsFullOutput({ summary: 'nothing to do' })).toBe(false)
    // Truncated: the marker is the last thing the agent says, so it may be cut.
    expect(needsFullOutput({ summary: 'nothing to do', summaryTruncated: true })).toBe(true)
    // Truncated but the marker already showed: no second call needed.
    expect(
      needsFullOutput({ summary: `x ${MISSION_COMPLETE_MARKER}`, summaryTruncated: true }),
    ).toBe(false)
  })

  it('edits a job back into a form', () => {
    const second = {
      ...WALLET,
      address: '0x2222222222222222222222222222222222222222',
      label: '10k',
      primary: false,
    }
    const form = missionFromJob(
      {
        name: 'DCA ETH',
        message: [
          '[Trading desk mission] DCA ETH',
          'Goal: Buy 10 USD of ETH.',
          'Wallets: 10k · 0x2222…2222, Main · 0x1111…1111 · Chains: Robinhood Chain, Base',
          'Budget: at most $300.00 in total for this mission and at most $10.00 per order. Never exceed it.',
          'Cadence: this message arrives every 1 hour from a scheduled job.',
          'Rules: use the wallet-trading skill.',
          'Stop: after 30 runs. Count the previous runs in this conversation; on the last one, end your reply with "MISSION COMPLETE".',
          'Dry run: this run, only quote and report. Do not swap.',
        ].join('\n'),
        scheduleKind: 'every',
        scheduleRaw: 3600,
      },
      WALLET.address,
      [WALLET, second],
    )
    expect(form.name).toBe('DCA ETH')
    expect(form.goal).toBe('Buy 10 USD of ETH.')
    expect(form.dryRun).toBe(true)
    expect(form.interval).toEqual({ kind: 'every', seconds: 3600 })
    // Not the blank contract's defaults: what the job actually says.
    expect(form.budgetTotalUsd).toBe('300')
    expect(form.budgetPerOrderUsd).toBe('10')
    expect(form.wallets).toEqual([second.address, WALLET.address])
    expect(form.chains).toEqual([4663, 8453])
    expect(form.stop).toEqual({ kind: 'runs', runs: 30 })
  })

  it('reads every stop rule and an absent budget back', () => {
    const read = (stopLine: string) =>
      missionFromJob(
        { name: 'x', message: `[Trading desk mission] x\nGoal: g\n${stopLine}` },
        WALLET.address,
        [WALLET],
      )
    expect(
      read('Stop: after 2026-12-31. When that moment has passed, reply exactly "MISSION COMPLETE".')
        .stop,
    ).toEqual({ kind: 'until', until: '2026-12-31' })
    expect(
      read('Stop: when the goal is reached, end your reply with "MISSION COMPLETE".').stop,
    ).toEqual({
      kind: 'goal',
    })
    const none = read('Rules: none')
    expect(none.stop).toEqual({ kind: 'none' })
    expect(none.budgetTotalUsd).toBe('')
    expect(none.budgetPerOrderUsd).toBe('')
    // A wallet the list does not know cannot be ticked; the primary stands in.
    expect(
      missionFromJob(
        { name: 'x', message: 'Goal: g\nWallets: 0x9999…9999 · Chains: Base' },
        WALLET.address,
        [WALLET],
      ).wallets,
    ).toEqual([WALLET.address])
  })

  it('round-trips every prefilled contract through its prompt', () => {
    const second = {
      ...WALLET,
      address: '0x2222222222222222222222222222222222222222',
      label: '10k',
      primary: false,
    }
    const wallets = [WALLET, second]
    const ctx = { wallets, limits: { thresholdUsd: 100, dailyCapUsd: 1000 } }
    for (const kind of ['swap', 'dca', 'dip', 'rebalance', 'custom'] as const) {
      const form = missionPrefill(kind, { primary: WALLET.address, chains: [8453, 4663] })
      form.name = `Mission ${kind}`
      form.goal = form.goal || 'Do the thing.'
      form.wallets = [WALLET.address, second.address]
      const job = {
        name: form.name,
        message: composeMissionPrompt(form, ctx),
        scheduleKind: 'every',
        scheduleRaw: form.interval.kind === 'every' ? form.interval.seconds : 0,
      }
      const back = missionFromJob(job, WALLET.address, wallets)
      // A job always edits as a custom contract; every other field survives.
      expect({ ...back, kind }).toEqual(form)
    }
    const until = missionPrefill('custom', { primary: WALLET.address })
    until.name = 'Until'
    until.goal = 'g'
    until.stop = { kind: 'until', until: '2026-12-31' }
    until.budgetTotalUsd = '1234.5'
    until.budgetPerOrderUsd = ''
    const back = missionFromJob(
      {
        name: 'Until',
        message: composeMissionPrompt(until, ctx),
        scheduleKind: 'every',
        scheduleRaw: 3600,
      },
      WALLET.address,
      wallets,
    )
    expect(back).toEqual(until)
  })
})

describe('bookConcession', () => {
  it('keeps the preference while the chat has its floor, then shrinks, then collapses', () => {
    expect(bookConcession(1200, 360, true)).toEqual({ book: 360, collapsed: false, cramped: false })
    expect(bookConcession(900, 360, true)).toEqual({ book: 300, collapsed: false, cramped: false })
    expect(bookConcession(850, 360, true)).toEqual({ book: 48, collapsed: true, cramped: true })
    expect(bookConcession(1200, 360, false)).toEqual({
      book: 48,
      collapsed: true,
      cramped: false,
    })
    expect(bookConcession(1600, 999, true)).toEqual({ book: 520, collapsed: false, cramped: false })
  })

  it('reports a frame with no room for a split, open or not', () => {
    // The spine's open button used to run its handler, set the preference, and
    // leave the panel exactly where it was, because the chain overruled it on
    // the very next render. `cramped` is what lets the spine say so instead —
    // and it must be true whether or not the preference is currently open,
    // since pressing open at this width is what does nothing.
    const tooNarrow = CHAT_MIN + BOOK_MIN - 1
    expect(bookConcession(tooNarrow, 360, true).cramped).toBe(true)
    expect(bookConcession(tooNarrow, 360, false).cramped).toBe(true)
    // One pixel more and the split is possible again.
    expect(bookConcession(CHAT_MIN + BOOK_MIN, 360, true)).toEqual({
      book: BOOK_MIN,
      collapsed: false,
      cramped: false,
    })
  })
})

describe('composerPlaceholder', () => {
  it('prefers steering copy while busy, then the mission word, then rotates real orders', () => {
    expect(
      composerPlaceholder({
        missionWord: 'DCA · Sleeping',
        busy: true,
        tick: 0,
        steering: 'steer',
      }),
    ).toBe('steer')
    expect(
      composerPlaceholder({
        missionWord: 'DCA · Sleeping',
        busy: false,
        tick: 0,
        steering: 'steer',
      }),
    ).toBe('DCA · Sleeping')
    expect(
      composerPlaceholder({ missionWord: null, busy: false, tick: 0, steering: 'steer' }),
    ).toBe(PLACEHOLDERS[0])
    expect(
      composerPlaceholder({
        missionWord: null,
        busy: false,
        tick: PLACEHOLDERS.length + 1,
        steering: 'steer',
      }),
    ).toBe(PLACEHOLDERS[1])
    // Stock Tokens are refused on Robinhood Chain, so the desk never suggests one.
    expect(PLACEHOLDERS).toContain('Sell 0.01 ETH for USDC on Base')
    expect(PLACEHOLDERS.join('\n')).not.toMatch(/AAPL|Robinhood/)
  })
})

describe('sends and batches', () => {
  const send = (extra: Parameters<typeof order>[0] = {}) =>
    order({
      kind: 'send',
      tokenOut: order().tokenIn,
      expectedOut: null,
      minOut: null,
      priceImpactPct: null,
      recipient: '0x2222222222222222222222222222222222222222',
      recipientLabel: null,
      amountIn: '0.1',
      valueUsd: 250,
      ...extra,
    })

  it('folds the legs of a multisend into one ask with a total', () => {
    const legs = [
      send({ orderId: 'a', batchId: 'bat_1', amountIn: '0.1', valueUsd: 250 }),
      send({ orderId: 'b', batchId: 'bat_1', amountIn: '0.25', valueUsd: 625 }),
      order({ orderId: 'c' }),
      send({ orderId: 'd', amountIn: '1', valueUsd: null }),
    ]
    const asks = groupAsks(legs)
    expect(asks.map((a) => a.key)).toEqual(['bat_1', 'c', 'd'])
    expect(asks[0]).toMatchObject({ kind: 'send', batch: true, totalUsd: 875, totalAmount: '0.35' })
    expect(asks[0]!.orders.map((o) => o.orderId)).toEqual(['a', 'b'])
    expect(asks[1]).toMatchObject({ kind: 'swap', batch: false })
    expect(asks[2]).toMatchObject({ batch: false, totalUsd: null })
    // Any unpriced leg leaves the batch unpriced too.
    const mixed = groupAsks([
      send({ orderId: 'x', batchId: 'b2', valueUsd: 1 }),
      send({ orderId: 'y', batchId: 'b2', valueUsd: null }),
    ])
    expect(mixed[0]!.totalUsd).toBeNull()
  })

  it('stamps risk on the batch total and on an unpriced send', () => {
    expect(riskStamp(send({ valueUsd: 100 }))).toBe('normal')
    expect(riskStamp(send({ valueUsd: null }))).toBe('high')
    const ask = groupAsks([
      send({ orderId: 'a', batchId: 'b', valueUsd: 300 }),
      send({ orderId: 'b', batchId: 'b', valueUsd: 300 }),
    ])[0]!
    expect(askRisk(ask)).toBe('high')
  })

  it('binds send and revoke facts, and batch facts on the whole', () => {
    const labels = { to: 'To', send: 'Send', spender: 'Spender', allowance: 'Allowance' }
    const facts = approvalFacts(send({ gasUsd: 0.01 }), [WALLET], labels)
    const keys = facts.map((f) => f.key)
    expect(keys).toEqual(['wallet', 'chain', 'to', 'send', 'value', 'gas', 'order', 'expires'])
    expect(facts.find((f) => f.key === 'to')!.value).toBe(
      '0x2222222222222222222222222222222222222222',
    )
    expect(facts.find((f) => f.key === 'send')!.value).toBe('0.1 ETH')
    const revoke = approvalFacts(
      send({
        kind: 'revoke',
        amountIn: 'unlimited',
        recipientLabel: 'Permit2',
        gasUsd: null,
        valueUsd: 0,
      }),
      [WALLET],
      labels,
    )
    expect(revoke.map((f) => f.key)).toEqual([
      'wallet',
      'chain',
      'token',
      'spender',
      'allowance',
      'order',
      'expires',
    ])
    expect(revoke.find((f) => f.key === 'spender')!.value).toBe(
      'Permit2 · 0x2222222222222222222222222222222222222222',
    )
    expect(revoke.find((f) => f.key === 'allowance')).toMatchObject({
      value: 'unlimited',
      tone: 'danger',
    })
    const ask = groupAsks([
      send({ orderId: 'a', batchId: 'bat_9', gasUsd: 0.01 }),
      send({ orderId: 'b', batchId: 'bat_9', gasUsd: 0.02 }),
    ])[0]!
    const batch = batchFacts(ask, [WALLET], { recipients: 'Recipients', total: 'Total' })
    expect(batch.map((f) => f.key)).toEqual([
      'wallet',
      'chain',
      'recipients',
      'total',
      'value',
      'gas',
      'batch',
      'expires',
    ])
    expect(batch.find((f) => f.key === 'recipients')!.value).toBe('2')
    expect(batch.find((f) => f.key === 'total')!.value).toBe('0.2 ETH')
    expect(batch.find((f) => f.key === 'gas')!.value).toBe('$0.03')
  })

  it('names the batch when a leg is rejected', () => {
    expect(rejectionMessage(send({ batchId: 'bat_1' }), 'wrong list', 3)).toBe(
      'Rejected batch bat_1 (3 sends): wrong list',
    )
    expect(rejectionMessage(send(), '')).toBe(
      'Rejected send o1. Do not retry it without asking first.',
    )
    expect(rejectionMessage(order(), 'no')).toBe('Rejected order o1: no')
  })
})

describe('send form', () => {
  const base: SendForm = {
    chainId: 8453,
    wallet: WALLET.address,
    token: 'USDC',
    recipients: [],
    amount: '',
    usd: '',
    note: '',
  }
  const A = '0x2222222222222222222222222222222222222222'
  const B = '0x3333333333333333333333333333333333333333'

  it('parses pasted lines in every shape people use', () => {
    expect(parseRecipientLines(`${A}\n${B}=25\n# note\n\n${A} 3\n${B},4`)).toEqual([
      { address: A, amount: '' },
      { address: B, amount: '25' },
      { address: A, amount: '3' },
      { address: B, amount: '4' },
    ])
    expect(parseRecipientLines('vitalik.eth')).toEqual([{ address: 'vitalik.eth', amount: '' }])
  })

  it('validates addresses, duplicates and sizing', () => {
    expect(validateSend(base)).toMatchObject({ ok: false, error: 'recipients' })
    expect(validateSend({ ...base, token: '' })).toMatchObject({ ok: false, error: 'token' })
    expect(
      validateSend({ ...base, recipients: [{ address: 'nope', amount: '' }], amount: '1' }),
    ).toMatchObject({ ok: false, error: 'address', index: 0 })
    expect(
      validateSend({
        ...base,
        recipients: [
          { address: A, amount: '' },
          { address: A.toUpperCase().replace('0X', '0x'), amount: '' },
        ],
        amount: '1',
      }),
    ).toMatchObject({ ok: false, error: 'duplicate', index: 1 })
    expect(validateSend({ ...base, recipients: [{ address: A, amount: '' }] })).toMatchObject({
      ok: false,
      error: 'amount',
      index: 0,
    })
    expect(validateSend({ ...base, recipients: [{ address: A, amount: 'x' }] })).toMatchObject({
      ok: false,
      error: 'amount',
    })
    expect(validateSend({ ...base, recipients: [{ address: A, amount: '5' }] })).toEqual({
      ok: true,
    })
    expect(
      validateSend({
        ...base,
        recipients: [
          { address: A, amount: '' },
          { address: B, amount: '2' },
        ],
        usd: '5',
      }),
    ).toEqual({ ok: true })
  })

  it('composes a deterministic prompt with exact addresses and amounts', () => {
    const prompt = composeSendPrompt(
      {
        ...base,
        recipients: [
          { address: A, amount: '' },
          { address: B, amount: '2.5' },
        ],
        usd: '5',
        note: 'payroll',
      },
      { wallets: [WALLET] },
    )
    const lines = prompt.split('\n')
    expect(lines[0]).toBe(`${SEND_TAG} Multisend to 2 recipients`)
    expect(lines[1]).toBe('Chain: Base · Token: USDC')
    expect(lines[2]).toBe('From: Main · 0x1111…1111')
    expect(lines[4]).toBe(`- ${A} → $5.00 worth of USDC`)
    expect(lines[5]).toBe(`- ${B} → 2.5 USDC`)
    expect(lines[6]).toBe('Note: payroll')
    expect(prompt).toContain('`agentos trade send` once')
    expect(prompt).toContain('wait for my approval')
    const single = composeSendPrompt(
      { ...base, wallet: null, recipients: [{ address: A, amount: '' }], amount: '10' },
      { wallets: [] },
    )
    expect(single).toContain(`${SEND_TAG} Send`)
    expect(single).toContain('From: the primary wallet')
    expect(single).toContain(`- ${A} → 10 USDC`)
  })
})

describe('validateSend · the USD field is a strict decimal', () => {
  const A = '0x2222222222222222222222222222222222222222'
  const base: SendForm = {
    chainId: 8453,
    wallet: null,
    token: 'USDC',
    recipients: [{ address: A, amount: '' }],
    amount: '',
    usd: '',
    note: '',
  }
  it('refuses what Number() would happily read as money', () => {
    // "1e3" is a thousand dollars and "0x10" is sixteen to Number(); nobody typed that.
    for (const usd of ['1e3', '0x10', 'Infinity', '-5', 'abc']) {
      expect(validateSend({ ...base, usd })).toMatchObject({ ok: false, error: 'amount' })
    }
  })
  it('takes a plain decimal, with surrounding spaces', () => {
    expect(validateSend({ ...base, usd: ' 5 ' })).toEqual({ ok: true })
    expect(validateSend({ ...base, usd: '12.50' })).toEqual({ ok: true })
  })
})

describe('mission stop rules are measured, not only described', () => {
  const runsJob = (run_count: unknown) => ({
    id: 'j1',
    enabled: true,
    run_count,
    message:
      'Goal: buy\nStop: after 3 runs. Count the previous runs in this conversation; on the last one, end your reply with "MISSION COMPLETE".',
  })
  it('reads the rule back from the prompt', () => {
    expect(missionStopRule(runsJob(0))).toEqual({ kind: 'runs', runs: 3 })
    expect(
      missionStopRule({
        message:
          'Stop: after 2026-09-30. When that moment has passed, reply exactly "MISSION COMPLETE" and do nothing else.',
      }),
    ).toEqual({ kind: 'until', until: '2026-09-30' })
    expect(missionStopRule({ message: 'Goal: buy' })).toEqual({ kind: 'none' })
  })
  it('is due once run_count reaches "after N runs", and not before', () => {
    const now = Date.now()
    expect(missionStopDue(runsJob(2), now)).toBe(false)
    expect(missionStopDue(runsJob(3), now)).toBe(true)
    expect(missionStopDue(runsJob(7), now)).toBe(true)
    // No counter yet: nothing to measure.
    expect(missionStopDue(runsJob(undefined), now)).toBe(false)
  })
  it('includes the whole "until" day, then is due', () => {
    const job = {
      message:
        'Stop: after 2026-09-30. When that moment has passed, reply exactly "MISSION COMPLETE" and do nothing else.',
    }
    const endOfDay = new Date(2026, 8, 30, 23, 0).getTime()
    const nextDay = new Date(2026, 9, 1, 0, 1).getTime()
    expect(missionStopDue(job, endOfDay)).toBe(false)
    expect(missionStopDue(job, nextDay)).toBe(true)
    expect(untilEpoch('not a date')).toBeNull()
  })
  it('leaves a goal rule to the agent', () => {
    expect(
      missionStopDue(
        { message: 'Stop: when the goal is reached, end your reply with "MISSION COMPLETE".' },
        Date.now(),
      ),
    ).toBe(false)
  })
})

describe('withBatchLegs · a card is built from the batch, not the page', () => {
  const leg = (orderId: string, extra: Partial<ReturnType<typeof order>> = {}) =>
    order({ orderId, kind: 'send', batchId: 'bat_1', recipient: '0x' + '2'.repeat(40), ...extra })
  it('replaces the page legs with every awaiting leg of the batch, once, in place', () => {
    const page = [order({ orderId: 'solo' }), leg('a')]
    const batch = [leg('a'), leg('b'), leg('c'), leg('d', { status: 'rejected' })]
    const out = withBatchLegs(page, new Map([['bat_1', batch]]))
    expect(out.map((o) => o.orderId)).toEqual(['solo', 'a', 'b', 'c'])
    expect(batchIdsOf(page)).toEqual(['bat_1'])
  })
  it('keeps the page legs while the batch lookup has not answered', () => {
    const page = [leg('a'), leg('b')]
    expect(withBatchLegs(page, new Map()).map((o) => o.orderId)).toEqual(['a', 'b'])
  })
})

describe('orderLine and orderKindWord · what a toast names', () => {
  const to = '0x6c83B17cBF1115735460cA0000000000000a8312'
  it('names a send by its recipient, a revoke by its spender, a swap by its pair', () => {
    expect(orderLine(order())).toBe('0.2 ETH → USDC')
    expect(
      orderLine(order({ kind: 'send', amountIn: '0.00001', recipient: to, recipientLabel: null })),
    ).toBe('0.00001 ETH → 0x6c83…8312')
    expect(
      orderLine(
        order({
          kind: 'revoke',
          tokenIn: order().tokenOut,
          recipient: to,
          recipientLabel: 'Permit2',
        }),
      ),
    ).toBe('USDC ⛨ Permit2')
    const legs = [
      order({ kind: 'send', batchId: 'b', amountIn: '0.1' }),
      order({ kind: 'send', batchId: 'b', amountIn: '0.15' }),
    ]
    expect(orderLine(legs[0]!, legs)).toBe('0.25 ETH → 2 recipients')
    expect(orderKindWord(legs[0]!, 2)).toBe('multisend')
    expect(orderKindWord(legs[0]!, 1)).toBe('send')
    expect(orderKindWord(order())).toBe('swap')
  })
  it('names a bridge by its destination chain, never by a recipient', () => {
    const bridge = bridgeOrder()
    expect(orderLine(bridge)).toBe('0.01 ETH → Robinhood Chain')
    expect(orderKindWord(bridge)).toBe('bridge')
    // No chain object from the engine: our registry names the id.
    expect(orderLine(bridgeOrder({ bridge: { ...bridge.bridge!, destinationChain: null } }))).toBe(
      '0.01 ETH → Robinhood Chain',
    )
    expect(rejectionMessage(bridge, '')).toBe(
      'Rejected bridge o1. Do not retry it without asking first.',
    )
  })
  it('recognises the engine refusing a second decision', () => {
    expect(alreadyDecided('trading.invalid: order o1 is no longer awaiting approval')).toBe(true)
    expect(alreadyDecided('trading.invalid: no batch x')).toBe(false)
  })
})

describe('approval facts · a bridge', () => {
  const labels = {
    chain: 'Chain',
    fromChain: 'From',
    toChain: 'To',
    send: 'Send',
    receive: 'Receive (expected)',
    bridgeFees: 'Bridge fees',
    value: 'Value',
    gas: 'Gas',
    order: 'Order',
    expires: 'Expires',
  }
  it('lists both chains, what leaves, what should land and the fees — no recipient', () => {
    const facts = approvalFacts(bridgeOrder(), [WALLET], labels)
    expect(facts.map((f) => f.key)).toEqual([
      'wallet',
      'chain',
      'fromChain',
      'toChain',
      'send',
      'receive',
      'bridgeFees',
      'value',
      'gas',
      'order',
      'expires',
    ])
    const by = (k: string) => facts.find((f) => f.key === k)!
    expect(by('fromChain').value).toBe('Base')
    expect(by('toChain').value).toBe('Robinhood Chain')
    expect(by('send').value).toBe('0.01 ETH')
    expect(by('receive').value).toBe('≈ 0.0099 ETH')
    expect(by('bridgeFees').value).toBe('$0.12')
    expect(by('bridgeFees').full).toBe('gas $0.02 · relayer $0.10')
    expect(facts.some((f) => f.key === 'to')).toBe(false)
    expect(facts.some((f) => f.wide)).toBe(false)
  })
  it('once confirmed, the receive row is what arrived, not the estimate', () => {
    const facts = approvalFacts(
      bridgeOrder({ status: 'confirmed', receivedOut: '0.00985' }),
      [WALLET],
      labels,
    )
    expect(facts.find((f) => f.key === 'receive')!.value).toBe('0.00985 ETH')
  })
})

describe('approval facts · addresses in full, expiry short', () => {
  const to = '0x6c83B17cBF1115735460cA0000000000000a8312'
  it('prints the recipient of a send whole, on a wide row', () => {
    const facts = approvalFacts(
      order({ kind: 'send', recipient: to, recipientLabel: null }),
      [WALLET],
      {},
    )
    const row = facts.find((f) => f.key === 'to')
    expect(row).toMatchObject({ value: to, wide: true })
  })
  it('prints the spender of a revoke whole with its label, and the unlimited word from the labels', () => {
    const facts = approvalFacts(
      order({ kind: 'revoke', recipient: to, recipientLabel: 'Permit2', amountIn: 'unlimited' }),
      [WALLET],
      { unlimited: 'Unlimited' },
    )
    expect(facts.find((f) => f.key === 'spender')).toMatchObject({
      value: `Permit2 · ${to}`,
      wide: true,
    })
    expect(facts.find((f) => f.key === 'allowance')).toMatchObject({
      value: 'Unlimited',
      tone: 'danger',
    })
  })
  it('shows the time alone when the expiry is today, the short date otherwise', () => {
    const now = new Date(2026, 8, 20, 12, 0).getTime()
    const later = new Date(2026, 8, 20, 14, 15).getTime()
    const tomorrow = new Date(2026, 8, 21, 14, 15).getTime()
    expect(formatExpiryShort(later, now, 'en-US')).toMatch(/^2:15 PM \(UTC[+−]\d/)
    expect(formatExpiryShort(tomorrow, now, 'en-US')).toMatch(/^Sep 21, 2:15 PM \(UTC[+−]\d/)
    expect(formatExpiryShort(tomorrow, now, 'en-US')).not.toContain('2026')
  })
})

// Live test 2026-10-06: at a 300 px BOOK nothing said the tab strip
// scrolled, and at 520 px "History" was clipped mid-glyph by the pips.
describe('watchTabStrip', () => {
  function strip(widths: { labelled: number; icons: number; client: number }) {
    const el = document.createElement('div')
    el.innerHTML = '<button aria-selected="false"></button><button aria-selected="true"></button>'
    let left = 0
    Object.defineProperty(el, 'clientWidth', { get: () => widths.client })
    Object.defineProperty(el, 'scrollWidth', {
      get: () => (el.dataset.labels === 'off' ? widths.icons : widths.labelled),
    })
    Object.defineProperty(el, 'scrollLeft', {
      get: () => left,
      set: (v: number) => (left = v),
    })
    return { el, scrollTo: (v: number) => ((left = v), el.dispatchEvent(new Event('scroll'))) }
  }

  it('keeps the words while the tabs fit, with no fade', () => {
    const { el } = strip({ labelled: 400, icons: 200, client: 420 })
    const stop = watchTabStrip(el)
    expect(el.dataset.labels).toBe('on')
    expect(el.dataset.fade).toBeUndefined()
    stop()
  })

  it('drops the words before a label clips, and fades the side that hides tabs', () => {
    const { el, scrollTo } = strip({ labelled: 520, icons: 260, client: 200 })
    const stop = watchTabStrip(el)
    expect(el.dataset.labels).toBe('off')
    expect(el.dataset.fade).toBe('end')
    scrollTo(30)
    expect(el.dataset.fade).toBe('both')
    scrollTo(60)
    expect(el.dataset.fade).toBe('start')
    stop()
    // Detached: a scroll no longer touches the strip.
    scrollTo(0)
    expect(el.dataset.fade).toBe('start')
  })

  it('a resize that still overflows brings the selected tab back into view', () => {
    // Labels are already off at 520 px and at 300 px, so only the width
    // changes; History (selected) sits past the right edge afterwards.
    const widths = { labelled: 520, icons: 260, client: 300 }
    const { el } = strip(widths)
    const selected = el.querySelector<HTMLElement>('[aria-selected="true"]')!
    const into = vi.fn()
    selected.scrollIntoView = into
    let observe: (() => void) | null = null
    const RO = class {
      constructor(cb: () => void) {
        observe = cb
      }
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', RO)
    const stop = watchTabStrip(el)
    expect(el.dataset.labels).toBe('off')
    into.mockClear()
    widths.client = 200
    observe!()
    expect(into).toHaveBeenCalledTimes(1)
    stop()
    vi.unstubAllGlobals()
  })

  it('icons alone that fit: no words, no fade', () => {
    const { el } = strip({ labelled: 520, icons: 250, client: 300 })
    watchTabStrip(el)()
    expect(el.dataset.labels).toBe('off')
    expect(el.dataset.fade).toBeUndefined()
  })
})
