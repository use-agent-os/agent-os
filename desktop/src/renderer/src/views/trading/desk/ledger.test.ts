import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import {
  cardCallFromResult,
  dcaCallFromResult,
  commandFromToolInput,
  exitCodeOf,
  isBracketKind,
  ledgerRuns,
  lpCallFromResult,
  parseTradeCommand,
  parseTradeResult,
  triggerCallFromResult,
  withLiveMandate,
  withLiveTrigger,
} from './ledger'

describe('commandFromToolInput', () => {
  it('reads the command out of an object, a JSON string, or a truncated preview', () => {
    expect(commandFromToolInput({ command: 'agentos trade status --json' })).toBe(
      'agentos trade status --json',
    )
    expect(commandFromToolInput('{"command": "agentos wallet balances --json"}')).toBe(
      'agentos wallet balances --json',
    )
    expect(commandFromToolInput('{\n  "command": "agentos trade quote --chain base --in ETH')).toBe(
      'agentos trade quote --chain base --in ETH',
    )
    expect(commandFromToolInput('ls -la')).toBe('ls -la')
    expect(commandFromToolInput(null)).toBeNull()
  })
})

describe('parseTradeCommand', () => {
  it('recognises desk commands and summarises their arguments', () => {
    const swap = parseTradeCommand(
      'uv run agentos trade swap --chain base --in ETH --out USDC --amount 0.01 --wait --json',
    )
    expect(swap).toMatchObject({ kind: 'swap', title: 'Swap', detail: '0.01 ETH → USDC · Base' })
    expect(
      parseTradeCommand('agentos trade quote --chain robinhood --in USDC --out AAPL --pct 50'),
    ).toMatchObject({ kind: 'quote', detail: '50% USDC → AAPL · Robinhood' })
    expect(parseTradeCommand('agentos wallet balances --json')).toMatchObject({ kind: 'balances' })
    expect(parseTradeCommand('agentos wallet list --json')).toMatchObject({
      kind: 'wallet',
      detail: 'list',
    })
    expect(
      parseTradeCommand('agentos trade order ord_123 --wait-seconds 600 --json'),
    ).toMatchObject({
      kind: 'order',
      detail: 'ord_123',
    })
    expect(parseTradeCommand('cd /tmp && agentos trade portfolio --json')).toMatchObject({
      kind: 'portfolio',
    })
  })
  it('ignores everything else', () => {
    expect(parseTradeCommand('ls -la')).toBeNull()
    expect(parseTradeCommand('agentos skills list')).toBeNull()
    expect(parseTradeCommand('')).toBeNull()
  })
})

describe('bridges (agentos trade bridge)', () => {
  const create = parseTradeCommand(
    'agentos trade bridge create --chain base --to-chain robinhood --token ETH --amount 0.01 --note "user: bridge" --client-id bridge-eth-1 --wait --wait-seconds 600 --json',
  )!
  const bridgeOrder = (extra: Record<string, unknown> = {}) => ({
    orderId: 'o9',
    kind: 'bridge',
    status: 'awaiting_approval',
    chainId: 8453,
    amountIn: '0.01',
    tokenIn: { symbol: 'ETH' },
    tokenOut: { symbol: 'ETH' },
    expectedOut: '0.0099',
    provider: 'relay',
    bridge: {
      originChainId: 8453,
      destinationChainId: 4663,
      destinationChain: { chainId: 4663, key: 'robinhood', name: 'Robinhood Chain' },
      fillStatus: null,
    },
    ...extra,
  })

  it('reads the three subcommands like the LP writes', () => {
    expect(create).toMatchObject({
      kind: 'bridge',
      title: 'Bridge',
      detail: '0.01 ETH · Base → Robinhood',
    })
    expect(
      parseTradeCommand(
        'agentos trade bridge quote --chain robinhood --to-chain base --token USDG --usd 20 --json',
      ),
    ).toMatchObject({
      kind: 'bridge_quote',
      title: 'Bridge quote',
      detail: '$20 of USDG · Robinhood → Base',
    })
    expect(parseTradeCommand('agentos trade bridge status o9 --json')).toMatchObject({
      kind: 'bridge_status',
      title: 'Bridge status',
      detail: 'o9',
    })
  })

  it('reads `create` from `order`: parked, then sent, then confirmed with what landed', () => {
    const parked = parseTradeResult(create, JSON.stringify({ order: bridgeOrder() }))
    expect(parked.awaiting).toBe(true)
    expect(parked.orderId).toBe('o9')
    expect(parked.provider).toBe('relay')
    expect(parked.summary).toBe('0.01 ETH → Robinhood Chain (≈ 0.0099 ETH) · awaiting approval')
    const done = parseTradeResult(
      create,
      JSON.stringify({
        order: bridgeOrder({
          status: 'confirmed',
          txHash: '0xdep',
          explorerUrl: 'https://basescan.org/tx/0xdep',
          receivedOut: '0.00985',
          bridge: { ...bridgeOrder().bridge, fillStatus: 'success' },
        }),
      }),
    )
    expect(done.confirmed).toBe(true)
    expect(done.txHash).toBe('0xdep')
    expect(done.summary).toBe('0.01 ETH → Robinhood Chain (0.00985 ETH) · confirmed')
  })

  it('reads `status` from `order` and `bridge.fillStatus` while the fill is in flight', () => {
    const status = parseTradeCommand('agentos trade bridge status o9 --json')!
    const pending = parseTradeResult(
      status,
      JSON.stringify({
        order: bridgeOrder({ status: 'submitted', txHash: '0xdep' }),
        bridge: { requestId: 'req_1', fillStatus: 'pending' },
      }),
    )
    expect(pending.status).toBe('submitted')
    expect(pending.summary).toBe('0.01 ETH → Robinhood Chain (≈ 0.0099 ETH) · sent · fill pending')
    expect(pending.confirmed).toBe(false)
    const refunded = parseTradeResult(
      status,
      JSON.stringify({
        order: bridgeOrder({ status: 'failed', reason: 'refund: relay refunded the deposit' }),
        bridge: { fillStatus: 'refund' },
      }),
    )
    expect(refunded.error).toBe('refund: relay refunded the deposit')
    expect(refunded.summary).not.toContain('fill')
  })

  it('summarises a quote', () => {
    const quote = parseTradeCommand(
      'agentos trade bridge quote --chain base --to-chain robinhood --token ETH --amount 0.01 --json',
    )!
    const out = parseTradeResult(
      quote,
      JSON.stringify({
        quote: {
          expectedOut: '0.0099',
          tokenOut: { symbol: 'ETH' },
          feesUsd: 0.12,
          timeEstimateSeconds: 30,
        },
      }),
    )
    expect(out.summary).toBe('≈ 0.0099 ETH · fees $0.12 · ~30s')
    expect(out.error).toBeNull()
  })
})

describe('parseTradeResult', () => {
  it('describes a projected-away result from the call itself, without error styling', () => {
    const call = parseTradeCommand(
      'agentos trade quote --chain base --in ETH --out USDC --amount 0.0001',
    )!
    const out = parseTradeResult(
      call,
      '[tool_result_projection]\ntool_result_handle: tr-809b\nsha256: cbf9e8',
    )
    expect(out.summary).toBe(call.detail)
    expect(out.error).toBeNull()
  })

  const swap = parseTradeCommand(
    'agentos trade swap --chain base --in ETH --out USDC --amount 0.01',
  )!

  it('reads a single order and earns the stamps only on proof', () => {
    const awaiting = parseTradeResult(
      swap,
      'exit_code=0\n' +
        JSON.stringify({
          orders: [
            {
              orderId: 'o1',
              status: 'awaiting_approval',
              amountIn: '0.01',
              tokenIn: { symbol: 'ETH' },
              tokenOut: { symbol: 'USDC' },
              expectedOut: '25.1',
              provider: 'uniswap',
            },
          ],
        }),
    )
    expect(awaiting.awaiting).toBe(true)
    expect(awaiting.confirmed).toBe(false)
    expect(awaiting.orderId).toBe('o1')
    expect(awaiting.provider).toBe('uniswap')
    expect(awaiting.summary).toBe('0.01 ETH → 25.1 USDC · awaiting approval')

    const confirmedNoHash = parseTradeResult(
      swap,
      JSON.stringify({
        orders: [
          { orderId: 'o2', status: 'confirmed', amountIn: '1', tokenIn: 'ETH', tokenOut: 'USDC' },
        ],
      }),
    )
    expect(confirmedNoHash.confirmed).toBe(false)

    const confirmed = parseTradeResult(
      swap,
      JSON.stringify({
        orders: [
          {
            orderId: 'o3',
            status: 'confirmed',
            txHash: '0xabc',
            explorerUrl: 'https://x/tx/0xabc',
            amountIn: '1',
            tokenIn: 'ETH',
            tokenOut: 'USDC',
          },
        ],
      }),
    )
    expect(confirmed.confirmed).toBe(true)
    expect(confirmed.txHash).toBe('0xabc')
    expect(confirmed.explorerUrl).toBe('https://x/tx/0xabc')
  })

  it('summarises a batch, a quote, balances and a portfolio', () => {
    const batch = parseTradeResult(
      swap,
      JSON.stringify({
        orders: [{ status: 'confirmed', txHash: '0x1' }, { status: 'awaiting_approval' }],
      }),
    )
    expect(batch.summary).toBe('2 wallets · 1 confirmed, 1 awaiting approval')
    expect(batch.awaiting).toBe(true)

    const quote = parseTradeResult(
      parseTradeCommand('agentos trade quote --in ETH --out USDC --amount 0.1')!,
      JSON.stringify({
        amountIn: '0.1',
        tokenIn: { symbol: 'ETH' },
        amountOut: '250',
        tokenOut: { symbol: 'USDC' },
        priceImpactPct: 0.12,
        guard: { decision: 'needs_approval' },
        provider: 'aggregator',
      }),
    )
    expect(quote.summary).toBe('0.1 ETH → 250 USDC · impact 0.12% · would need approval')
    expect(quote.provider).toBe('aggregator')

    const balances = parseTradeResult(
      parseTradeCommand('agentos wallet balances --json')!,
      JSON.stringify({
        balances: [
          { amount: '0.5', valueUsd: 1200 },
          { amount: '0', valueUsd: 0 },
        ],
        updatedAt: 1_700_000_000_000,
      }),
    )
    expect(balances.summary).toBe('1 balances · $1,200.00')
    expect(balances.marketAt).toBe(1_700_000_000_000)

    const portfolio = parseTradeResult(
      parseTradeCommand('agentos trade portfolio --json')!,
      JSON.stringify({ totals: { valueUsd: 42.5 }, holdings: [{}, {}] }),
    )
    expect(portfolio.summary).toBe('$42.50 · 2 holdings')
  })

  it('falls back to the first line and reports a failing exit code', () => {
    const plain = parseTradeResult(swap, 'exit_code=0\nnothing to see here\nsecond line')
    expect(plain.summary).toBe('nothing to see here')
    expect(plain.error).toBeNull()
    const failed = parseTradeResult(swap, 'exit_code=1\nError: gateway unreachable')
    expect(failed.error).toBe('Error: gateway unreachable')
    const rpcError = parseTradeResult(
      swap,
      JSON.stringify({
        error: { message: 'No Uniswap API key configured', code: 'trading.no_api_key' },
      }),
    )
    expect(rpcError.error).toBe('No Uniswap API key configured')
    expect(exitCodeOf('exit_code=2\n')).toBe(2)
    expect(exitCodeOf('{}')).toBeNull()
  })
})

describe('ledgerRuns', () => {
  it('folds runs of three or more, leaves shorter runs alone', () => {
    expect(ledgerRuns([true, true, true, false, true, true])).toEqual([{ start: 0, length: 3 }])
    expect(ledgerRuns([false, true, true, true, true])).toEqual([{ start: 1, length: 4 }])
    expect(ledgerRuns([true, false, true])).toEqual([])
  })
})

describe('sends, allowances, decode and network rows', () => {
  const A = '0x2222222222222222222222222222222222222222'
  const B = '0x3333333333333333333333333333333333333333'

  it('recognises the new commands and summarises their arguments', () => {
    expect(
      parseTradeCommand(
        `agentos trade send --chain base --token USDC --to ${A} --amount 25 --json`,
      ),
    ).toMatchObject({ kind: 'send', title: 'Send', detail: '25 USDC → 0x2222…2222 · Base' })
    expect(
      parseTradeCommand(
        `agentos trade send --chain base --token ETH --to ${A}=0.1 --to ${B}=0.2 --json`,
      ),
    ).toMatchObject({ kind: 'send', detail: 'ETH → 2 recipients · Base' })
    expect(
      parseTradeCommand(
        'agentos trade send --chain robinhood --token USDG --file list.txt --usd 5',
      ),
    ).toMatchObject({ kind: 'send', detail: '$5 of USDG → a list · Robinhood' })
    expect(parseTradeCommand('agentos trade allowances --json')).toMatchObject({
      kind: 'allowances',
      title: 'Allowances',
    })
    expect(
      parseTradeCommand(`agentos trade revoke --chain base --token ${A} --spender ${B} --json`),
    ).toMatchObject({ kind: 'revoke', detail: '0x2222…2222 for 0x3333…3333 · Base' })
    expect(
      parseTradeCommand(`agentos trade decode --chain base 0x${'ab'.repeat(32)} --json`),
    ).toMatchObject({ kind: 'decode', detail: '0xabababab… · Base' })
    expect(parseTradeCommand('agentos trade decode --chain base --data 0xa9 --json')).toMatchObject(
      { kind: 'decode', detail: 'calldata · Base' },
    )
    expect(parseTradeCommand('agentos trade network --json')).toMatchObject({ kind: 'network' })
  })

  it('reads a send order and a multisend batch', () => {
    const send = parseTradeCommand(`agentos trade send --chain base --token USDC --to ${A}`)!
    const single = parseTradeResult(
      send,
      JSON.stringify({
        orders: [
          {
            orderId: 's1',
            kind: 'send',
            status: 'awaiting_approval',
            amountIn: '25',
            tokenIn: { symbol: 'USDC' },
            tokenOut: { symbol: 'USDC' },
            recipient: A,
          },
        ],
        batchId: null,
      }),
    )
    expect(single.summary).toBe('25 USDC → 0x2222…2222 · awaiting approval')
    expect(single.awaiting).toBe(true)
    expect(single.orderId).toBe('s1')
    const batch = parseTradeResult(
      send,
      JSON.stringify({
        orders: [
          { orderId: 'a', kind: 'send', status: 'confirmed', txHash: '0x1', amountIn: '1' },
          { orderId: 'b', kind: 'send', status: 'awaiting_approval', amountIn: '2' },
          { orderId: 'c', kind: 'send', status: 'awaiting_approval', amountIn: '3' },
        ],
        batchId: 'bat_1',
      }),
    )
    expect(batch.summary).toBe('3 recipients · 1 confirmed, 2 awaiting approval')
    expect(batch.awaiting).toBe(true)
    expect(batch.confirmed).toBe(false)
    // The stamp jumps to the first leg still waiting.
    expect(batch.orderId).toBe('b')
  })

  it('reads a revoke, an allowance review, a decode and a network probe', () => {
    const revoke = parseTradeCommand('agentos trade revoke --chain base --token x --spender y')!
    expect(
      parseTradeResult(
        revoke,
        JSON.stringify({
          order: {
            orderId: 'r1',
            kind: 'revoke',
            status: 'awaiting_approval',
            amountIn: 'unlimited',
            tokenIn: { symbol: 'USDC' },
            recipient: A,
            recipientLabel: 'Permit2',
          },
        }),
      ).summary,
    ).toBe('revoke USDC for Permit2 · awaiting approval')
    const allowances = parseTradeCommand('agentos trade allowances --json')!
    expect(
      parseTradeResult(
        allowances,
        JSON.stringify({
          allowances: [
            { unlimited: true, exposureUsd: 1000 },
            { unlimited: false, exposureUsd: 12.5 },
          ],
          unlimitedCount: 1,
        }),
      ).summary,
    ).toBe('2 live · 1 unlimited · $1,012.50 at stake')
    const decode = parseTradeCommand('agentos trade decode --chain base 0xabc')!
    const known = parseTradeResult(
      decode,
      JSON.stringify({
        call: { function: 'transfer', selector: '0xa9059cbb', known: true },
        tx: { status: 'success', hash: '0x' + 'ab'.repeat(32) },
        transfers: [{}],
      }),
    )
    expect(known.summary).toBe('transfer · success · 1 transfer')
    expect(known.txHash).toBe('0x' + 'ab'.repeat(32))
    expect(
      parseTradeResult(
        decode,
        JSON.stringify({ call: { function: null, selector: '0xdeadbeef', known: false } }),
      ).summary,
    ).toBe('0xdeadbeef (unknown)')
    const network = parseTradeCommand('agentos trade network --json')!
    const probe = parseTradeResult(
      network,
      JSON.stringify({
        chains: [
          { name: 'Base', healthy: true, blockAgeS: 2 },
          { name: 'Robinhood Chain', healthy: false, blockAgeS: 90 },
        ],
      }),
    )
    expect(probe.summary).toBe('Base ✓ 2s · Robinhood Chain ✗ 90s')
    expect(probe.error).toBe('1 chain unhealthy')
  })
})

describe('liquidity reads (agentos trade lp)', () => {
  const MARKER =
    'publish_artifact path=.agentos/lp/ranges-boar-base-20260927T105305Z.json mime=application/vnd.agentos.lp+json'

  it('names the call instead of calling it a generic trade call', () => {
    const call = parseTradeCommand('agentos trade lp pool boar --chain base --json')!
    expect(call).toMatchObject({ kind: 'lp', title: 'Liquidity read', detail: 'pool boar · Base' })
    expect(
      parseTradeCommand('cd /w && uv run agentos trade lp positions --wallet 0xabc --json'),
    ).toMatchObject({ kind: 'lp', detail: 'positions' })
  })

  it('labels the result by kind, subject and chain, never by its JSON', () => {
    const call = parseTradeCommand('agentos trade lp pool boar --chain base --json')!
    const body = {
      version: 1,
      kind: 'pool',
      chain: { id: 8453, key: 'base', name: 'Base', explorer: 'https://basescan.org' },
      token: { address: '0x1', symbol: 'boar', decimals: 18, priceUsd: 0.0001 },
      pool: { poolId: '0x2', tvlUsd: 1_447_702, mcapUsd: 2_514_887 },
    }
    const out = parseTradeResult(call, `exit_code=0\n${JSON.stringify(body, null, 2)}\n${MARKER}`)
    expect(out.detail).toBe('pool boar (Base)')
    expect(out.summary).not.toContain('{')
    expect(out.summary).toBe('TVL $1.4M · mcap $2.5M')
    expect(out.error).toBeNull()
  })

  it('summarises a multi-chain positions read', () => {
    const call = parseTradeCommand('agentos trade lp positions --json')!
    const body = {
      version: 1,
      kind: 'positions',
      chain: null,
      wallets: [{ address: '0x1' }, { address: '0x2' }],
      chains: [
        { key: 'base', name: 'Base' },
        { key: 'robinhood', name: 'Robinhood' },
      ],
      positions: [],
      totals: { valueUsd: null, feesUsd: null, count: 3, outOfRange: 1 },
    }
    const out = parseTradeResult(call, JSON.stringify(body))
    expect(out.detail).toBe('positions · 2 wallets (Base + Robinhood)')
    expect(out.summary).toBe('3 open · 1 out of range')
  })

  describe('positions chain label never comes from a row', () => {
    const row = (key: string, name: string) =>
      `{"chain": {"id": 4663, "key": "${key}", "name": "${name}", "explorer": "https://x"}, "tokenId": "7"`
    const prefix =
      '{"version": 1, "kind": "positions", "chain": null, "asOfBlock": 5, ' +
      '"asOfBlocks": {"base": 5, "robinhood": 9}, "wallets": [{"address": "0xde93"}], ' +
      '"chains": [{"id": 8453, "key": "base", "name": "Base"}, {"id": 4663, "key": "robinhood", "name": "Robinhood Chain"}], ' +
      `"positions": [${row('robinhood', 'Robinhood Chain')}, "own`

    it('names every scanned chain from asOfBlocks when the JSON is truncated', () => {
      const call = parseTradeCommand('agentos trade lp positions --wallet 0xde93 --json')!
      const out = parseTradeResult(call, prefix)
      expect(out.detail).toBe('positions (Base + Robinhood)')
    })

    it('names the chains from the envelope when the JSON parses', () => {
      const call = parseTradeCommand('agentos trade lp positions --wallet 0xde93 --json')!
      const body = {
        version: 1,
        kind: 'positions',
        chain: null,
        asOfBlocks: { base: 5, robinhood: 9 },
        wallets: [{ address: '0xde93' }],
        chains: [
          { key: 'base', name: 'Base' },
          { key: 'robinhood', name: 'Robinhood Chain' },
        ],
        positions: [{ chain: { key: 'robinhood', name: 'Robinhood Chain' }, tokenId: '7' }],
        totals: { valueUsd: 12, feesUsd: 0, count: 1, outOfRange: 0 },
      }
      const out = parseTradeResult(call, JSON.stringify(body))
      expect(out.detail).toBe('positions · 1 wallet (Base + Robinhood)')
    })

    it('falls back to the command line when even asOfBlocks was cut', () => {
      const cut = `{"version": 1, "kind": "positions", "chain": null, "asOfBlo`
      const both = parseTradeCommand('agentos trade lp positions --wallet 0xde93 --json')!
      expect(parseTradeResult(both, cut).detail).toBe('positions (Base + Robinhood)')
      const one = parseTradeCommand(
        'agentos trade lp positions --wallet 0xde93 --chain base --json',
      )!
      expect(parseTradeResult(one, cut).detail).toBe('positions (Base)')
      const two = parseTradeCommand(
        'agentos trade lp positions --chain robinhood --chain=base --json',
      )!
      expect(parseTradeResult(two, cut).detail).toBe('positions (Robinhood + Base)')
    })

    it('uses the command flags over a row when asOfBlocks is gone', () => {
      const call = parseTradeCommand('agentos trade lp positions --chain base --json')!
      const noBlocks = `{"version": 1, "kind": "positions", "chain": null, "positions": [${row('robinhood', 'Robinhood Chain')}`
      expect(parseTradeResult(call, noBlocks).detail).toBe('positions (Base)')
    })

    it('omits the chain when neither the result nor the command names it', () => {
      const call = lpCallFromResult(`${prefix.slice(0, 60)}\n${MARKER}`)!
      const cut = `{"version": 1, "kind": "positions", "chain": null, "positions": [${row('robinhood', 'Robinhood Chain')}`
      expect(parseTradeResult(call, cut).detail).toBe('positions')
    })
  })

  it('still labels a result truncated past valid JSON', () => {
    const call = parseTradeCommand('agentos trade lp ranges boar --chain base --json')!
    const truncated =
      '{"version": 1, "kind": "ranges", "chain": {"id": 8453, "key": "base", "name": "Base", ' +
      '"explorer": "https://basescan.org"}, "asOfBlock": 1, "token": {"address": "0x1", "symbol": "boar", "dec'
    const out = parseTradeResult(call, truncated)
    expect(out.detail).toBe('ranges boar (Base)')
    expect(out.summary).not.toContain('{')
  })

  it('reports an lp error as an error', () => {
    const call = parseTradeCommand('agentos trade lp pool nope --chain base --json')!
    const out = parseTradeResult(
      call,
      'exit_code=1\n{"error": {"code": "trading.lp.pool_not_found", "message": "no V4 pool for nope"}}',
    )
    expect(out.error).toBe('no V4 pool for nope')
  })

  it('recognises a liquidity read from the card announcement alone', () => {
    expect(lpCallFromResult(`{"kind": "pool"}\n${MARKER}`)).toMatchObject({
      kind: 'lp',
      title: 'Liquidity read',
    })
    expect(
      lpCallFromResult(
        '[generated artifact omitted: pool-boar-base-1.json (application/vnd.agentos.lp+json)]',
      ),
    ).not.toBeNull()
    expect(lpCallFromResult('publish_artifact path=a.png mime=image/png')).toBeNull()
    expect(lpCallFromResult('{"kind": "pool"}')).toBeNull()
  })

  it('recognises all three shapes of the card line: raw, live-rewritten, projected', () => {
    const body = '{"version": 1, "kind": "positions"'
    // What the CLI prints.
    expect(lpCallFromResult(`${body}\n${MARKER}`)).not.toBeNull()
    // What a live tool result carries: publish_inline_artifacts rewrote the line (no mime).
    const live =
      '[inline artifact published and already rendered for the user: ' +
      'lp-cards/positions-wallets-20260927T105305Z.json. Do not call publish_artifact for it.]'
    expect(lpCallFromResult(`${body}\n${live}`)).toMatchObject({
      kind: 'lp',
      title: 'Liquidity read',
    })
    expect(
      lpCallFromResult(
        '[inline artifact published and already rendered for the user: ' +
          '/w/lp-cards/pool-boar-base-1.json. Do not call publish_artifact for it.]',
      ),
    ).not.toBeNull()
    // What a history projection leaves.
    expect(
      lpCallFromResult(
        `${body}\n[generated artifact omitted: positions-wallets-1.json (application/vnd.agentos.lp+json)]`,
      ),
    ).not.toBeNull()
    // Some other inline artifact is not a liquidity read.
    expect(
      lpCallFromResult(
        '[inline artifact published and already rendered for the user: charts/boar.png. Do not call publish_artifact for it.]',
      ),
    ).toBeNull()
    expect(lpCallFromResult('[inline artifact not published: no such file]')).toBeNull()
  })

  it('keeps only the subject in the command detail, never a flag value', () => {
    expect(parseTradeCommand('agentos trade lp positions --budget-seconds 60 --json')!.detail).toBe(
      'positions',
    )
    expect(
      parseTradeCommand('agentos trade lp positions --budget-seconds=60 --all --wallet 0xabc')!
        .detail,
    ).toBe('positions')
    expect(
      parseTradeCommand('agentos trade lp pool --quote USDC --json boar --no-card --chain base')!
        .detail,
    ).toBe('pool boar · Base')
    expect(parseTradeCommand('agentos trade lp position --chain=base 12345 --json')!.detail).toBe(
      'position 12345 · Base',
    )
  })

  it('keeps the full chain name when a positions read spans one chain', () => {
    const call = parseTradeCommand('agentos trade lp positions --chain robinhood --json')!
    const cut = '{"version": 1, "kind": "positions", "chain": null, "asOfBlo'
    expect(parseTradeResult(call, cut).detail).toBe('positions (Robinhood Chain)')
  })
})

describe('trade status from history', () => {
  it('reads a status cut past valid JSON as the route, never the raw JSON', () => {
    const call = parseTradeCommand('agentos trade status --json')!
    const full = {
      enabled: true,
      version: '2026.9.27',
      provider: 'aggregator',
      providers: [{ id: 'aggregator', label: 'AgentOS Aggregator', active: true }],
      apiKeyConfigured: true,
      chains: Array.from({ length: 30 }, (_, i) => ({ chainId: i, name: `chain ${i}` })),
      unlocked: true,
    }
    const whole = parseTradeResult(call, JSON.stringify(full, null, 2))
    expect(whole.provider).toBe('aggregator')
    expect(whole.summary).toBe('via AgentOS Aggregator · vault unlocked')
    for (const cut of [
      JSON.stringify(full).slice(0, 400),
      `exit_code=0\n${JSON.stringify(full, null, 2).slice(0, 400)}`,
    ]) {
      const out = parseTradeResult(call, cut)
      expect(out.provider).toBe('aggregator')
      expect(out.summary).toBe('via AgentOS Aggregator')
      expect(out.summary).not.toContain('{')
      expect(out.error).toBeNull()
    }
    // A failed status still reads as its error line.
    expect(parseTradeResult(call, 'exit_code=1\ngateway unreachable').error).toBe(
      'gateway unreachable',
    )
  })
})

describe('DCA mandates (agentos trade dca)', () => {
  const FIXTURE = readFileSync(
    'src/renderer/src/views/trading/desk/__fixtures__/dca/mandate.json',
    'utf8',
  )
  const MARKER =
    'publish_artifact path=/tmp/dca-cards/mandate-dca-eth-20260928T060000Z.json mime=application/vnd.agentos.dca+json'

  it('titles every subcommand and summarises its arguments', () => {
    expect(
      parseTradeCommand('agentos trade dca create ETH --usd 10 --every 1d --cap 300 --json'),
    ).toMatchObject({ kind: 'dca_create', title: 'Start DCA', detail: 'ETH · $10 / day' })
    expect(
      parseTradeCommand(
        'agentos trade dca create eth --usd 25 --every 6h --runs 12 --chain base --max-price 3000 --json',
      ),
    ).toMatchObject({ detail: 'ETH · $25 / 6 h · Base' })
    expect(
      parseTradeCommand('agentos trade dca create ETH --usd 5 --every 30m --json')?.detail,
    ).toBe('ETH · $5 / 30 min')
    const rows: [string, string, string, string][] = [
      ['agentos trade dca show dca_1a2b3c4d --json', 'dca', 'DCA status', 'dca_1a2b3c4d'],
      ['agentos trade dca list --json', 'dca_list', 'DCA list', ''],
      ['agentos trade dca list --all --json', 'dca_list', 'DCA list', 'all'],
      ['agentos trade dca pause dca_1a2b3c4d --json', 'dca_pause', 'Pause DCA', 'dca_1a2b3c4d'],
      ['agentos trade dca resume dca_1a2b3c4d --json', 'dca_resume', 'Resume DCA', 'dca_1a2b3c4d'],
      ['agentos trade dca run dca_1a2b3c4d --wait --json', 'dca_run', 'Buy now', 'dca_1a2b3c4d'],
      [
        'agentos trade dca stop dca_1a2b3c4d --reason "done" --json',
        'dca_stop',
        'Stop DCA',
        'dca_1a2b3c4d',
      ],
      [
        'agentos trade dca approve dca_1a2b3c4d --json',
        'dca_approve',
        'Approve DCA',
        'dca_1a2b3c4d',
      ],
      [
        'agentos trade dca update dca_1a2b3c4d --cap 500 --json',
        'dca_update',
        'Update DCA',
        'dca_1a2b3c4d',
      ],
    ]
    for (const [command, kind, title, detail] of rows) {
      expect(parseTradeCommand(command), command).toMatchObject({ kind, title, detail })
    }
  })

  it('reads a mandate card as pair, cadence, state and money — never its JSON', () => {
    const call = parseTradeCommand('agentos trade dca show dca_1a2b3c4d --json')!
    const out = parseTradeResult(call, `${FIXTURE}\n${MARKER}`)
    expect(out.detail).toBe('ETH ← USDC · $10 / day')
    expect(out.summary).toBe('active · $120 of $300 · 12/30 buys')
    expect(out.error).toBeNull()
    expect(out.summary).not.toContain('{')
  })

  it('names the fired buy of a Buy now and earns the awaiting stamp when it parks', () => {
    const payload = JSON.parse(FIXTURE) as Record<string, unknown> & {
      mandate: { history: Record<string, unknown>[] }
    }
    const parked = { ...payload, run: payload.mandate.history[0] }
    const call = parseTradeCommand('agentos trade dca run dca_1a2b3c4d --json')!
    const out = parseTradeResult(call, JSON.stringify(parked))
    expect(out.summary.startsWith('buy #14 awaiting approval')).toBe(true)
    expect(out.awaiting).toBe(true)
    expect(out.orderId).toBe('ord_7c2e91')
    const filled = { ...payload, run: payload.mandate.history[2] }
    const done = parseTradeResult(call, JSON.stringify(filled))
    expect(done.summary.startsWith('buy #12 filled')).toBe(true)
    expect(done.confirmed).toBe(true)
    expect(done.txHash).toMatch(/^0x0c/)
  })

  it('reads a list, and an agent proposal as awaiting approval', () => {
    const payload = JSON.parse(FIXTURE) as { mandate: Record<string, unknown> }
    const proposal = { ...payload.mandate, id: 'dca_99', status: 'awaiting_approval' }
    const list = {
      version: 1,
      kind: 'mandates',
      fetchedAt: '2026-09-28T06:00:00Z',
      warnings: [],
      mandates: [payload.mandate, proposal],
      totals: { count: 2, active: 1, spentUsd: 120, capUsd: 600, acquiredUsd: 127.9 },
    }
    const call = parseTradeCommand('agentos trade dca list --json')!
    const out = parseTradeResult(call, JSON.stringify(list))
    expect(out.detail).toBe('2 mandates')
    expect(out.summary).toBe('1 active · 1 awaiting approval · $120 of $600')
    expect(out.awaiting).toBe(true)
    const empty = { ...list, mandates: [], totals: { count: 0, active: 0, spentUsd: 0, capUsd: 0 } }
    expect(parseTradeResult(call, JSON.stringify(empty)).summary).toBe('none yet')
  })

  it('reads a result cut past valid JSON from its first fields', () => {
    const call = parseTradeCommand(
      'agentos trade dca create ETH --usd 10 --every 1d --cap 300 --json',
    )!
    const cut = FIXTURE.replace('"active"', '"awaiting_approval"').slice(0, 900)
    const out = parseTradeResult(call, cut)
    expect(out.detail).toBe('DCA ETH')
    expect(out.summary).toBe('awaiting approval · dca_1a2b3c4d')
    expect(out.awaiting).toBe(true)
  })

  it('lets a Start DCA row follow its mandate in the live list, else stay as recorded', () => {
    const call = parseTradeCommand(
      'agentos trade dca create ETH --usd 10 --every 1d --cap 300 --json',
    )!
    const payload = JSON.parse(FIXTURE) as { mandate: Record<string, unknown> }
    const proposed = { ...payload, mandate: { ...payload.mandate, status: 'awaiting_approval' } }
    const out = parseTradeResult(call, JSON.stringify(proposed))
    expect(out.mandateId).toBe('dca_1a2b3c4d')
    expect(out.mandateStatus).toBe('awaiting_approval')
    expect(out.awaiting).toBe(true)
    // Not in any loaded list: exactly as recorded.
    expect(withLiveMandate(out, undefined)).toBe(out)
    // Completed since: the pill and the status word follow the mandate.
    const done = withLiveMandate(out, 'completed')
    expect(done.awaiting).toBe(false)
    expect(done.mandateLive).toBe('completed')
    expect(done.summary.startsWith('done · ')).toBe(true)
    expect(withLiveMandate(out, 'active').summary.startsWith('active · ')).toBe(true)
    const still = withLiveMandate(out, 'awaiting_approval')
    expect(still.awaiting).toBe(true)
    expect(still.summary).toBe(out.summary)
    // A truncated result keeps the id too.
    const cut = FIXTURE.replace('"active"', '"awaiting_approval"').slice(0, 900)
    const truncated = withLiveMandate(parseTradeResult(call, cut), 'stopped')
    expect(truncated.summary).toBe('stopped · dca_1a2b3c4d')
    expect(truncated.awaiting).toBe(false)
    // A projected result on a command that names the mandate: the id is the command's.
    const pause = parseTradeCommand('agentos trade dca pause dca_1a2b3c4d --json')!
    const projected = parseTradeResult(pause, '{"version": 1, "kind": "mandate", "mandate": {')
    expect(projected.mandateId).toBe('dca_1a2b3c4d')
    // A Buy now that parked an order keeps the order's pill.
    const parked = parseTradeResult(
      parseTradeCommand('agentos trade dca run dca_1a2b3c4d --json')!,
      JSON.stringify({
        ...payload,
        run: (payload.mandate as { history: unknown[] }).history[0],
      }),
    )
    expect(withLiveMandate(parked, 'active')).toBe(parked)
  })

  it('reports an operator-only refusal plainly', () => {
    const call = parseTradeCommand('agentos trade dca approve dca_1a2b3c4d --json')!
    const out = parseTradeResult(
      call,
      'exit_code=1\n{"error": {"code": "trading.operator_required", "message": "only the operator may approve"}}',
    )
    expect(out.error).toBe('only the operator may approve')
  })

  it('recognises a DCA call from its card line alone, in all three shapes', () => {
    const body = '{"version": 1, "kind": "mandate"'
    expect(dcaCallFromResult(`${body}\n${MARKER}`)).toMatchObject({
      kind: 'dca',
      title: 'DCA status',
    })
    const live =
      '[inline artifact published and already rendered for the user: ' +
      'dca-cards/mandates-all-20260928T060000Z.json. Do not call publish_artifact for it.]'
    expect(dcaCallFromResult(`{"version": 1, "kind": "mandates"}\n${live}`)).toMatchObject({
      kind: 'dca_list',
      title: 'DCA list',
    })
    expect(
      dcaCallFromResult(
        '[generated artifact omitted: mandate-dca-eth-1.json (application/vnd.agentos.dca+json)]',
      ),
    ).not.toBeNull()
    // Neither an LP card nor a stray image is a DCA.
    expect(
      dcaCallFromResult('publish_artifact path=a.json mime=application/vnd.agentos.lp+json'),
    ).toBeNull()
    expect(dcaCallFromResult('publish_artifact path=a.png mime=image/png')).toBeNull()
    expect(cardCallFromResult(`${body}\n${MARKER}`)?.kind).toBe('dca')
    expect(
      cardCallFromResult('publish_artifact path=a.json mime=application/vnd.agentos.lp+json')?.kind,
    ).toBe('lp')
    // The live rewrite ends in "]" after the JSON; the row still reads the card.
    const out = parseTradeResult(dcaCallFromResult(`${FIXTURE}\n${live}`)!, `${FIXTURE}\n${live}`)
    expect(out.detail).toBe('ETH ← USDC · $10 / day')
  })
})

describe('price triggers (agentos trade trigger)', () => {
  const FIXTURE = readFileSync(
    'src/renderer/src/views/trading/desk/__fixtures__/trigger/trigger.json',
    'utf8',
  )
  const MARKER =
    'publish_artifact path=/tmp/trigger-cards/trigger-stop-loss-eth-20261004T060000Z.json mime=application/vnd.agentos.trigger+json'

  it('titles a create by what it arms, and its subject by token and line', () => {
    const rows: [string, string, string][] = [
      [
        'agentos trade trigger create ETH --sell --pct 50 --below 3800 --json',
        'Stop-loss',
        'ETH · under $3,800',
      ],
      [
        'agentos trade trigger create eth --sell --pct 50 --above +20% --json',
        'Take-profit',
        'ETH · 20 % over now',
      ],
      [
        'agentos trade trigger create ETH --sell --pct 100 --below -10% --chain base --json',
        'Stop-loss',
        'ETH · 10 % under now · Base',
      ],
      [
        'agentos trade trigger create ETH --sell --pct 100 --trail 10 --json',
        'Trailing stop',
        'ETH · 10 % below peak',
      ],
      [
        'agentos trade trigger create ETH --buy --usd 50 --below 3500 --json',
        'Buy the dip',
        'ETH · under $3,500',
      ],
      [
        'agentos trade trigger create ETH --buy --usd 50 --above 4200 --json',
        'Buy the breakout',
        'ETH · over $4,200',
      ],
      [
        'agentos trade trigger create ETH --alert --above 5000 --json',
        'Price alert',
        'ETH · over $5,000',
      ],
      [
        'agentos trade trigger create USDC --alert --above 0.5 --json',
        'Price alert',
        'USDC · over $0.5',
      ],
    ]
    for (const [command, title, detail] of rows) {
      expect(parseTradeCommand(command), command).toMatchObject({
        kind: 'trigger_create',
        title,
        detail,
      })
    }
  })

  it('titles every other subcommand and names its trigger', () => {
    const rows: [string, string, string, string][] = [
      [
        'agentos trade trigger show trg_1a2b3c4d --json',
        'trigger',
        'Trigger status',
        'trg_1a2b3c4d',
      ],
      ['agentos trade trigger list --json', 'trigger_list', 'Triggers', ''],
      ['agentos trade trigger list --all --json', 'trigger_list', 'Triggers', 'all'],
      [
        'agentos trade trigger approve trg_1a2b3c4d --json',
        'trigger_approve',
        'Approve trigger',
        'trg_1a2b3c4d',
      ],
      [
        'agentos trade trigger reject trg_1a2b3c4d --reason "no" --json',
        'trigger_reject',
        'Reject trigger',
        'trg_1a2b3c4d',
      ],
      [
        'agentos trade trigger pause trg_1a2b3c4d --json',
        'trigger_pause',
        'Pause trigger',
        'trg_1a2b3c4d',
      ],
      [
        'agentos trade trigger resume trg_1a2b3c4d --json',
        'trigger_resume',
        'Resume trigger',
        'trg_1a2b3c4d',
      ],
      [
        'agentos trade trigger stop trg_1a2b3c4d --json',
        'trigger_stop',
        'Stop trigger',
        'trg_1a2b3c4d',
      ],
      [
        'agentos trade trigger fire trg_1a2b3c4d --wait --wait-seconds 60 --json',
        'trigger_fire',
        'Fire now',
        'trg_1a2b3c4d',
      ],
    ]
    for (const [command, kind, title, detail] of rows) {
      expect(parseTradeCommand(command), command).toMatchObject({ kind, title, detail })
    }
  })

  it('reads a trigger card as subject, state, plan and price — never its JSON', () => {
    const call = parseTradeCommand('agentos trade trigger show trg_1a2b3c4d --json')!
    const out = parseTradeResult(call, `${FIXTURE}\n${MARKER}`)
    expect(out.detail).toBe('ETH · under $3,800')
    expect(out.summary).toBe('armed · sell 50 % of ETH → USDC · ETH $3,882 · −2.1 %')
    expect(out.error).toBeNull()
    expect(out.triggerId).toBe('trg_1a2b3c4d')
    expect(out.summary).not.toContain('{')
  })

  it('names the fire of a Fire now and earns the awaiting stamp when its order parks', () => {
    const payload = JSON.parse(FIXTURE) as Record<string, unknown>
    const call = parseTradeCommand('agentos trade trigger fire trg_1a2b3c4d --json')!
    const fire = {
      n: 2,
      at: '2026-10-04T06:01:00Z',
      manual: true,
      status: 'parked',
      reasonCode: null,
      reason: null,
      priceUsd: 3790,
      orderId: 'ord_9',
      txHash: null,
      explorerUrl: null,
    }
    const parked = parseTradeResult(call, JSON.stringify({ ...payload, fire }))
    expect(parked.summary.startsWith('fire #2 awaiting approval')).toBe(true)
    expect(parked.awaiting).toBe(true)
    expect(parked.orderId).toBe('ord_9')
    const skipped = parseTradeResult(
      call,
      JSON.stringify({
        ...payload,
        fire: { ...fire, status: 'skipped', orderId: null, reason: 'paused: nothing to sell' },
      }),
    )
    expect(skipped.error).toBe('paused: nothing to sell')
  })

  it('reads a list, an empty one, and one with a proposal as awaiting', () => {
    const payload = JSON.parse(FIXTURE) as { trigger: Record<string, unknown> }
    const list = {
      version: 1,
      kind: 'triggers',
      fetchedAt: '2026-10-04T06:00:00Z',
      warnings: [],
      triggers: [payload.trigger, { ...payload.trigger, id: 'trg_2', status: 'awaiting_approval' }],
      totals: { count: 2, armed: 1, awaiting: 1, triggered: 0 },
    }
    const call = parseTradeCommand('agentos trade trigger list --json')!
    const out = parseTradeResult(call, JSON.stringify(list))
    expect(out.detail).toBe('2 triggers')
    expect(out.summary).toBe('1 armed · 1 awaiting approval')
    expect(out.awaiting).toBe(true)
    const empty = {
      ...list,
      triggers: [],
      totals: { count: 0, armed: 0, awaiting: 0, triggered: 0 },
    }
    expect(parseTradeResult(call, JSON.stringify(empty)).summary).toBe('none yet')
  })

  it('reads a result cut past valid JSON, and follows the trigger in the live list', () => {
    const call = parseTradeCommand(
      'agentos trade trigger create ETH --sell --pct 50 --below 3800 --json',
    )!
    const cut = FIXTURE.replace('"armed"', '"awaiting_approval"').slice(0, 700)
    const out = parseTradeResult(call, cut)
    expect(out.detail).toBe('Stop-loss ETH')
    expect(out.summary).toBe('awaiting approval · trg_1a2b3c4d')
    expect(out.awaiting).toBe(true)
    expect(withLiveTrigger(out, undefined)).toBe(out)
    const armed = withLiveTrigger(out, 'armed')
    expect(armed.awaiting).toBe(false)
    expect(armed.triggerLive).toBe('armed')
    expect(armed.summary).toBe('armed · trg_1a2b3c4d')
  })

  it('reports an operator-only refusal plainly', () => {
    const call = parseTradeCommand('agentos trade trigger pause trg_1a2b3c4d --json')!
    const out = parseTradeResult(
      call,
      'exit_code=1\n{"error": {"code": "trading.operator_required", "message": "only the operator may pause"}}',
    )
    expect(out.error).toBe('only the operator may pause')
  })

  it('recognises a trigger call from its card line alone, in all three shapes', () => {
    expect(triggerCallFromResult(`{"version": 1, "kind": "trigger"}\n${MARKER}`)).toMatchObject({
      kind: 'trigger',
      title: 'Trigger status',
    })
    const live =
      '[inline artifact published and already rendered for the user: ' +
      'trigger-cards/triggers-all-20261004T060000Z.json. Do not call publish_artifact for it.]'
    expect(triggerCallFromResult(live)).toMatchObject({ kind: 'trigger_list', title: 'Triggers' })
    expect(
      triggerCallFromResult(
        '[generated artifact omitted: trigger-x-1.json (application/vnd.agentos.trigger+json)]',
      ),
    ).not.toBeNull()
    expect(
      triggerCallFromResult('publish_artifact path=a.json mime=application/vnd.agentos.dca+json'),
    ).toBeNull()
    expect(cardCallFromResult(`{}\n${MARKER}`)?.kind).toBe('trigger')
    // The live rewrite ends in "]" after the JSON; the row still reads the card.
    const single =
      '[inline artifact published and already rendered for the user: ' +
      'trigger-cards/trigger-stop-loss-eth-1.json. Do not call publish_artifact for it.]'
    const text = `${FIXTURE}\n${single}`
    const out = parseTradeResult(triggerCallFromResult(text)!, text)
    expect(out.detail).toBe('ETH · under $3,800')
  })
})

describe('brackets (agentos trade protect / trade bracket)', () => {
  const FIXTURE = readFileSync(
    'src/renderer/src/views/trading/desk/__fixtures__/bracket/bracket.json',
    'utf8',
  )
  const MARKER =
    'publish_artifact path=/tmp/trigger-cards/bracket-protect-eth-20261004T060000Z.json mime=application/vnd.agentos.trigger+json'

  it('titles a protect by its two lines, percent or price', () => {
    const rows: [string, string, string][] = [
      ['agentos trade protect ETH --tp +20% --sl -10% --json', 'Protect', 'ETH · +20 % / −10 %'],
      [
        'agentos trade protect eth --tp 20% --sl 10% --pct 50 --json',
        'Protect',
        'ETH · +20 % / −10 %',
      ],
      [
        'agentos trade protect ETH --tp 4560 --sl 3420 --chain base --json',
        'Protect',
        'ETH · $4,560 / $3,420 · Base',
      ],
      [
        'agentos trade protect ETH --tp +20% --trail 10 --tp-pct 50 --json',
        'Protect',
        'ETH · +20 % / trail 10 %',
      ],
      [
        'agentos trade protect USDC --tp 0.5 --sl 0.1 --alert --json',
        'Watch',
        'USDC · $0.5 / $0.1',
      ],
    ]
    for (const [command, title, detail] of rows) {
      expect(parseTradeCommand(command), command).toMatchObject({
        kind: 'bracket_create',
        title,
        detail,
      })
    }
  })

  it('titles every bracket subcommand and names its bracket', () => {
    const rows: [string, string, string, string][] = [
      [
        'agentos trade bracket show brk_1a2b3c4d --json',
        'bracket',
        'Bracket status',
        'brk_1a2b3c4d',
      ],
      ['agentos trade bracket list --json', 'bracket_list', 'Brackets', ''],
      ['agentos trade bracket list --all --json', 'bracket_list', 'Brackets', 'all'],
      [
        'agentos trade bracket approve brk_1a2b3c4d --json',
        'bracket_approve',
        'Approve bracket',
        'brk_1a2b3c4d',
      ],
      [
        'agentos trade bracket reject brk_1a2b3c4d --reason "no" --json',
        'bracket_reject',
        'Reject bracket',
        'brk_1a2b3c4d',
      ],
      [
        'agentos trade bracket pause brk_1a2b3c4d --json',
        'bracket_pause',
        'Pause bracket',
        'brk_1a2b3c4d',
      ],
      [
        'agentos trade bracket resume brk_1a2b3c4d --json',
        'bracket_resume',
        'Resume bracket',
        'brk_1a2b3c4d',
      ],
      [
        'agentos trade bracket stop brk_1a2b3c4d --json',
        'bracket_stop',
        'Stop bracket',
        'brk_1a2b3c4d',
      ],
      [
        'agentos trade bracket fire brk_1a2b3c4d --leg tp --wait --json',
        'bracket_fire',
        'Fire now',
        'brk_1a2b3c4d',
      ],
    ]
    for (const [command, kind, title, detail] of rows) {
      expect(parseTradeCommand(command), command).toMatchObject({ kind, title, detail })
    }
    expect(isBracketKind('bracket_fire')).toBe(true)
    expect(isBracketKind('trigger_fire')).toBe(false)
  })

  it('reads a bracket card as subject, state, plan and moves — never its JSON', () => {
    const call = parseTradeCommand('agentos trade bracket show brk_1a2b3c4d --json')!
    const out = parseTradeResult(call, `${FIXTURE}\n${MARKER}`)
    expect(out.detail).toBe('ETH · $3,420 – $4,560')
    expect(out.summary).toBe('armed · sell 100 % of ETH → USDC · ETH $3,800 · +20 % / −10 %')
    expect(out.error).toBeNull()
    expect(out.triggerId).toBe('brk_1a2b3c4d')
    expect(out.summary).not.toContain('{')
  })

  it('names the fire of a Sell now and earns the awaiting stamp when its order parks', () => {
    const payload = JSON.parse(FIXTURE) as Record<string, unknown>
    const call = parseTradeCommand('agentos trade bracket fire brk_1a2b3c4d --json')!
    const fire = {
      n: 1,
      at: '2026-10-04T06:01:00Z',
      manual: true,
      status: 'parked',
      reasonCode: null,
      reason: null,
      priceUsd: 3800,
      orderId: 'ord_9',
      txHash: null,
      explorerUrl: null,
    }
    const parked = parseTradeResult(call, JSON.stringify({ ...payload, fire }))
    expect(parked.summary.startsWith('fire #1 awaiting approval')).toBe(true)
    expect(parked.awaiting).toBe(true)
    expect(parked.orderId).toBe('ord_9')
  })

  it('reads a list, an empty one, and a result cut past valid JSON', () => {
    const payload = JSON.parse(FIXTURE) as { bracket: Record<string, unknown> }
    const list = {
      version: 1,
      kind: 'brackets',
      fetchedAt: '2026-10-04T06:00:00Z',
      warnings: [],
      brackets: [payload.bracket, { ...payload.bracket, id: 'brk_2', status: 'awaiting_approval' }],
      totals: { count: 2, armed: 1, awaiting: 1, triggered: 0 },
    }
    const call = parseTradeCommand('agentos trade bracket list --json')!
    const out = parseTradeResult(call, JSON.stringify(list))
    expect(out.detail).toBe('2 brackets')
    expect(out.summary).toBe('1 armed · 1 awaiting approval')
    expect(out.awaiting).toBe(true)
    const empty = {
      ...list,
      brackets: [],
      totals: { count: 0, armed: 0, awaiting: 0, triggered: 0 },
    }
    expect(parseTradeResult(call, JSON.stringify(empty)).summary).toBe('none yet')

    const create = parseTradeCommand('agentos trade protect ETH --tp +20% --sl -10% --json')!
    const cut = FIXTURE.replace('"armed"', '"awaiting_approval"').slice(0, 600)
    const truncated = parseTradeResult(create, cut)
    expect(truncated.detail).toBe('Protect ETH')
    expect(truncated.summary).toBe('awaiting approval · brk_1a2b3c4d')
    expect(truncated.awaiting).toBe(true)
    // Its pill follows the live bracket list like a trigger row's.
    const armed = withLiveTrigger(truncated, 'armed')
    expect(armed.awaiting).toBe(false)
    expect(armed.summary).toBe('armed · brk_1a2b3c4d')
  })

  it('reports an operator-only refusal plainly', () => {
    const call = parseTradeCommand('agentos trade bracket approve brk_1a2b3c4d --json')!
    const out = parseTradeResult(
      call,
      'exit_code=1\n{"error": {"code": "trading.operator_required", "message": "only the operator may approve"}}',
    )
    expect(out.error).toBe('only the operator may approve')
  })

  it('recognises a bracket call from its card line alone', () => {
    expect(triggerCallFromResult(`{"version": 1, "kind": "bracket"}\n${MARKER}`)).toMatchObject({
      kind: 'bracket',
      title: 'Bracket status',
    })
    const live =
      '[inline artifact published and already rendered for the user: ' +
      'trigger-cards/brackets-all-20261004T060000Z.json. Do not call publish_artifact for it.]'
    expect(triggerCallFromResult(live)).toMatchObject({ kind: 'bracket_list', title: 'Brackets' })
    const single =
      '[inline artifact published and already rendered for the user: ' +
      'trigger-cards/bracket-protect-eth-1.json. Do not call publish_artifact for it.]'
    expect(cardCallFromResult(single)?.kind).toBe('bracket')
    // A plain trigger card is still a trigger.
    expect(
      triggerCallFromResult(
        '[inline artifact published and already rendered for the user: trigger-cards/trigger-x-1.json.]',
      )?.kind,
    ).toBe('trigger')
    const text = `${FIXTURE}\n${single}`
    expect(parseTradeResult(triggerCallFromResult(text)!, text).detail).toBe(
      'ETH · $3,420 – $4,560',
    )
  })
})
