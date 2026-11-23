// The Trading page's safety rails: only reads reach the gateway, a read never
// asks the engine to go to the chain, explorer links come from fixed bases
// with validated hex, and untrusted names are cut to size.
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { beforeEach, describe, expect, it } from 'vitest'
import { FakeRpc } from '@/test/fake-rpc'
import { read, TRADING_READ_METHODS, type TradingReadMethod } from './data'
import {
  addressUrl,
  BALANCE_MASK,
  clampSym,
  clampText,
  reloadBalancePrivacy,
  txUrl,
  useBalancePrivacy,
} from './safety'

const FEATURE_DIR = 'src/web/features/trading'
const HASH = `0x${'ab'.repeat(32)}`
const ADDR = `0x${'1f'.repeat(20)}`

function featureSources(): { file: string; text: string }[] {
  return readdirSync(FEATURE_DIR, { withFileTypes: true })
    .filter((e) => e.isFile() && /\.tsx?$/.test(e.name) && !/\.test\.tsx?$/.test(e.name))
    .map((e) => path.join(FEATURE_DIR, e.name))
    .map((file) => ({ file, text: readFileSync(file, 'utf8') }))
}

describe('trading read guard', () => {
  it('lists only the reads checked against the gateway', () => {
    expect([...TRADING_READ_METHODS].sort()).toEqual(
      [
        'trading.bracket.list',
        'trading.chart',
        'trading.dca.list',
        'trading.history',
        'trading.lp.book',
        'trading.markets',
        'trading.orders.list',
        'trading.portfolio',
        'trading.status',
        'trading.trigger.list',
        'wallet.balances',
        'wallet.list',
        'wallet.status',
      ].sort(),
    )
  })

  it('refuses a method off the list without calling the gateway', async () => {
    const rpc = new FakeRpc().handle('trading.swap', {})
    for (const method of ['trading.swap', 'wallet.send', 'wallet.unlock', 'trading.sync']) {
      await expect(read(rpc, method as TradingReadMethod)).rejects.toThrow(/not a trading read/)
    }
    expect(rpc.calls).toEqual([])
  })

  it('strips the flags that would make a read go to the chain', async () => {
    const rpc = new FakeRpc().handle('wallet.balances', {}).handle('trading.lp.book', {})
    await read(rpc, 'trading.lp.book', { wallet: ADDR, refresh: true, checkRpc: true, full: true })
    expect(rpc.callsTo('trading.lp.book')).toEqual([{ wallet: ADDR }])
    await read(rpc, 'wallet.balances', { address: ADDR, refresh: true })
    expect(rpc.callsTo('wallet.balances')).toEqual([{ address: ADDR }])
  })

  it('never names a trading or wallet method outside the allow-list, nor passes refresh', () => {
    const allowed = new Set<string>(TRADING_READ_METHODS)
    const events =
      /^(trading\.(changed|order\.finished|(dca|trigger|bracket)\.changed|trigger\.fired))$/
    // Engine error codes the page reads (an ambiguous symbol's candidates), not methods.
    const errorCodes = /^trading\.invalid$/
    const offenders: string[] = []
    for (const { file, text } of featureSources()) {
      for (const m of text.matchAll(/['"`]((?:trading|wallet)\.[a-zA-Z_.]+)['"`]/g)) {
        const name = m[1] as string
        if (!allowed.has(name) && !events.test(name) && !errorCodes.test(name))
          offenders.push(`${file}: ${name}`)
      }
      if (/refresh\s*:\s*true/.test(text)) offenders.push(`${file}: refresh: true`)
      // Every gateway call goes through read(); nothing else touches the client.
      if (file !== path.join(FEATURE_DIR, 'data.ts') && /\brpc\.call\b|\.call\(\s*['"`]/.test(text))
        offenders.push(`${file}: direct rpc.call`)
      // Inside data.ts the one call is read()'s, on the checked method and stripped params.
      if (file === path.join(FEATURE_DIR, 'data.ts')) {
        const calls = [...text.matchAll(/\.call\b/g)].length
        if (calls !== 1 || !/\brpc\.call<T>\(method, clean\)/.test(text))
          offenders.push(`${file}: ${calls} gateway calls, expected only read()'s`)
      }
    }
    expect(offenders).toEqual([])
  })
})

describe('explorer links', () => {
  it('builds from the fixed base of a known network', () => {
    expect(txUrl(8453, HASH)).toBe(`https://basescan.org/tx/${HASH}`)
    expect(addressUrl(4663, ADDR)).toBe(`https://robin.etherscan.io/address/${ADDR}`)
  })

  it('rejects unknown networks', () => {
    expect(txUrl(1, HASH)).toBeNull()
    expect(addressUrl(0, ADDR)).toBeNull()
  })

  it.each([
    ['empty', ''],
    ['null', null],
    ['no prefix', 'ab'.repeat(32)],
    ['non-hex', `0x${'zz'.repeat(32)}`],
    ['too short', `0x${'ab'.repeat(31)}`],
    ['too long', `0x${'ab'.repeat(33)}`],
    ['path injection', `0x${'ab'.repeat(32)}/../../evil`],
    ['script', 'javascript:alert(1)'],
  ])('rejects a malformed hash (%s)', (_, value) => {
    expect(txUrl(8453, value)).toBeNull()
  })

  it('rejects a hash where an address belongs and the reverse', () => {
    expect(addressUrl(8453, HASH)).toBeNull()
    expect(txUrl(8453, ADDR)).toBeNull()
    expect(addressUrl(8453, `0x${'g1'.repeat(20)}`)).toBeNull()
  })
})

describe('clampText', () => {
  it('cuts long names with an ellipsis and folds whitespace', () => {
    expect(clampText('a'.repeat(100), 10)).toBe(`${'a'.repeat(9)}…`)
    expect(clampText('  two\n\tlines  ')).toBe('two lines')
    expect(clampText(null)).toBe('')
  })

  it('drops bidi overrides and zero-width characters from names and symbols', () => {
    expect(clampText('USDC\u202eHTEW')).toBe('USDCHTEW')
    expect(clampText('U\u200bSDC\ufeff\u2066x\u2069')).toBe('USDCx')
    expect(clampSym('\u202eUSDC\u200d')).toBe('USDC')
    expect(clampSym(null)).toBe('')
  })

  it('never cuts a character in half', () => {
    expect(clampText('😀'.repeat(40), 4)).toBe('😀😀😀…')
  })
})

describe('balance privacy', () => {
  beforeEach(() => {
    localStorage.clear()
    reloadBalancePrivacy()
  })

  it('persists the choice and masks with a fixed-width mark', () => {
    expect(useBalancePrivacy.getState().hidden).toBe(false)
    useBalancePrivacy.getState().toggle()
    expect(localStorage.getItem('agentos-web.trading.balancesHidden')).toBe('true')
    useBalancePrivacy.setState({ hidden: false })
    reloadBalancePrivacy()
    expect(useBalancePrivacy.getState().hidden).toBe(true)
    expect(BALANCE_MASK).toBe('******')
  })
})
