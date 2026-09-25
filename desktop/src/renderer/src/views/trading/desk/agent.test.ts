import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ensureTradingSessionKey,
  readTradingSessionKey,
  writeTradingSessionKey,
} from '~/stores/trading-ui'
import {
  type AgentRpc,
  isTradingAgentKey,
  syncTradingAgent,
  TRADING_AGENT_ID,
  TRADING_AGENT_VERSION,
  tradingAgentFiles,
  tradingAgentSpec,
} from './agent'
import { mintTradingSessionKey } from './mode-logic'

describe('trading agent spec', () => {
  it('narrows to a named profile plus an allowlist, never an allowlist alone', () => {
    const spec = tradingAgentSpec()
    expect(spec.id).toBe(TRADING_AGENT_ID)
    expect(spec.tools.profile).toBe('minimal')
    expect(spec.tools.allow).toContain('exec_command')
    expect(spec.tools.allow).toContain('ask_user')
  })
  it('keeps the desk away from editing, code and messaging tools', () => {
    const allow: readonly string[] = tradingAgentSpec().tools.allow
    for (const tool of [
      'write_file',
      'edit_file',
      'apply_patch',
      'execute_code',
      'git_commit',
      'message',
      'cron',
      'sessions_spawn',
    ]) {
      expect(allow).not.toContain(tool)
    }
  })
  it('owns the persona files and stamps each with the spec version', () => {
    const files = tradingAgentFiles()
    expect(Object.keys(files).sort()).toEqual(
      ['AGENTS.md', 'BOOTSTRAP.md', 'IDENTITY.md', 'SOUL.md', 'TOOLS.md'].sort(),
    )
    for (const content of Object.values(files)) {
      expect(content).toContain(`trading agent v${TRADING_AGENT_VERSION}`)
    }
    expect(files['AGENTS.md']).toContain('wallet-trading')
    expect(files['IDENTITY.md']).toContain('Name: Trading desk')
    // USER.md and MEMORY.md are the agent's own; the desktop never rewrites them.
    expect(files).not.toHaveProperty('USER.md')
    expect(files).not.toHaveProperty('MEMORY.md')
  })
  it('makes the primary the only wallet an order touches unless the user names another', () => {
    // A desk once answered "swap 50% ETH" by reading the second wallet's
    // balance too and holding on both; the rule now names one wallet.
    const files = tradingAgentFiles()
    expect(files['AGENTS.md']).toContain('## Which wallet')
    expect(files['AGENTS.md']).toMatch(/Never look up another wallet's balance/)
    expect(files['AGENTS.md']).toMatch(/a small balance does not/)
    expect(files['AGENTS.md']).toMatch(/Size is the user's call/)
    expect(files['SOUL.md']).toMatch(/The wallet is never ambiguous/)
    expect(files['TOOLS.md']).toMatch(/No `--wallet` means the\s+primary/)
    expect(files['TOOLS.md']).not.toMatch(/wallet balances \[ADDR\]/)
  })
})

describe('tradingAgentFiles · reading an order', () => {
  // A desk once answered "swap 0.1$ ETH to USDC" with "do you mean $0.10 or
  // 0.1 ETH?" and took six tool calls to place a swap the engine prices,
  // checks and guards on its own. The files now carry the conventions and
  // the one-command path.
  const files = tradingAgentFiles()
  it('reads dollar, token and share sizes without asking', () => {
    expect(files['AGENTS.md']).toContain('## Reading an order')
    expect(files['AGENTS.md']).toMatch(/`0\.1\$ ETH`.*`--usd 0\.1`/s)
    expect(files['AGENTS.md']).toMatch(/`hết`.*`--pct 100`/s)
    expect(files['AGENTS.md']).toMatch(/states the default you will take/)
  })
  it('places a clear chat order with one swap command', () => {
    expect(files['AGENTS.md']).toContain('## The fast path')
    expect(files['AGENTS.md']).toMatch(
      /do\s+not need a separate quote, a wallet listing or a balance read/,
    )
    expect(files['TOOLS.md']).toMatch(/--usd 0\.1 --note/)
    expect(files['TOOLS.md']).toMatch(/Do not open it or run `--help`/)
    expect(files['SOUL.md']).toMatch(/Never ask the user to\s+confirm what they just said/)
  })
  it('carries every trade command the agent may need, with real error codes', () => {
    // AGENTS.md tells the agent not to open the skill, so TOOLS.md must
    // list send, revoke, allowances, decode and network itself; and the
    // engine never raised `trading.provider_blocked`.
    const tools = files['TOOLS.md']
    for (const cmd of [
      'agentos trade send --chain base --token USDC --to 0xADDR --amount 25',
      'agentos trade allowances',
      'agentos trade revoke --chain base --token 0xTOKEN --spender 0xSPENDER',
      'agentos trade decode --chain base 0xTXHASH',
      'agentos trade network --json',
      '--kind swap|send|revoke',
    ]) {
      expect(tools).toContain(cmd)
    }
    expect(tools).toMatch(/--to 0xADDR=10/)
    expect(tools).toMatch(/a send \*\*always\*\* parks as `awaiting_approval`/)
    expect(tools).toContain('`trading.provider`')
    for (const name of Object.keys(files)) {
      expect(files[name]).not.toContain('provider_blocked')
    }
  })
  it('carries the hard rules: foreground only, no scheduled trades, one client id per order', () => {
    // A detached command outlives the guardrails; a cron job of the agent's
    // own making trades unattended; a bare retry after a timeout trades twice.
    const agents = files['AGENTS.md']
    const tools = files['TOOLS.md']
    expect(agents).toContain('## Hard rules')
    expect(agents).toMatch(/no `&`, no `nohup`, no `setsid`/)
    expect(agents).toMatch(/Never create a cron job or `cron --script` job that trades/)
    expect(agents).toMatch(/reuse the same `--client-id <id>`/)
    expect(agents).toMatch(/instead of trading twice/)
    expect(tools).toMatch(/no `&`, `nohup`, `setsid`/)
    expect(tools).toMatch(/`--client-id <id>` on `swap` and `send` is the order's idempotency key/)
    // Both command lines the agent copies carry the flag.
    expect(tools).toMatch(/agentos trade swap .*--client-id <id> --wait/)
    expect(tools).toMatch(/agentos trade send .*--client-id <id> --wait/)
    expect(agents).toMatch(/agentos trade swap .*--client-id \S+ --wait/)
  })
  it('names every outcome and error code, and what to do with each', () => {
    // A desk that meets `trading.no_route` bare either retries forever or
    // gives up on a transient; the table says which codes are final.
    const tools = files['TOOLS.md']
    expect(tools).toMatch(/- Outcomes\. An order ends in one of: `confirmed`/)
    for (const code of [
      'trading.no_route',
      'trading.unpriced',
      'trading.token_not_tradeable',
      'trading.quote_expired',
      'trading.price_moved',
      'trading.gas_too_high',
      'trading.tx_pending',
      'trading.provider',
      'trading.insufficient_balance',
      'trading.slippage_too_high',
      'trading.invalid',
      'TOKEN_AMBIGUOUS',
      'TOKEN_UNVERIFIED',
      'trading.operator_required',
    ]) {
      expect(tools).toContain(`\`${code}\``)
    }
    expect(tools).toMatch(/One retry at most per order, always with the same --client-id\./)
    expect(tools).toMatch(
      /`trading\.tx_pending` — .*\n.*`agentos trade order <id> --wait --wait-seconds 600 --json`/,
    )
  })
  it('sizes Robinhood Chain orders in tokens and treats a refused Stock Token as final', () => {
    // Live on Robinhood Chain: `--usd` answers `trading.unpriced`, bare
    // `USDC` resolves to lookalikes, and AAPL/TSLA are refused by the venue.
    const agents = files['AGENTS.md']
    expect(agents).toContain('- Chain: Base unless the user names Robinhood Chain.')
    expect(agents).toMatch(/Robinhood Chain: size orders in token units \(`--amount`\)/)
    expect(agents).toMatch(/Never pass the bare symbol `USDC` on\s+Robinhood/)
    expect(agents).toMatch(/do not retry, do not retry by address; tell\s+the user and stop/)
    expect(agents).not.toMatch(/Stock Token tickers go\s+straight into/)
    expect(agents).not.toMatch(/USDG\/Stock Tokens/)
    expect(files['TOOLS.md']).not.toMatch(/a Stock\s+Token ticker on Robinhood/)
  })
  it('reads the impact ceiling from the engine instead of a hard-coded 5%', () => {
    const agents = files['AGENTS.md']
    expect(agents).toMatch(
      /Read `limits\.agentMaxPriceImpactPct` from\s+`agentos trade status --json` once per conversation \(call it MAXI\)/,
    )
    expect(agents).toMatch(/Between MAXI\/5 and MAXI, say so in the report/)
    expect(agents).toMatch(/more than 3×MAXI, do\s+not send/)
    expect(agents).toMatch(/quote first and hold above MAXI/)
    expect(agents).not.toMatch(/Between 1% and\s+5%/)
  })
  it('reports the guard verdict from the result and never estimates the daily cap', () => {
    const agents = files['AGENTS.md']
    expect(agents).toMatch(/`guard\.decision` and `guard\.reason` from the result/)
    expect(agents).toMatch(
      /quote the daily cap\s+only if you ran `agentos trade limits` in this turn — never estimate it/,
    )
    expect(agents).not.toMatch(/daily cap used and remaining/)
    expect(agents).toMatch(
      /`--pct 100` on ETH fails with `trading\.invalid` when the balance is at\s+or below the 0\.001 ETH gas reserve/,
    )
  })
  it('answers a bridge request in one message and runs nothing for it', () => {
    // No command moves funds between chains. Asked to "bridge" ETH to
    // Robinhood Chain, a desk opened the skill to look for one; asked to
    // "chuyển 0.001 ETH qua Robinhood chain", it read a send there and
    // asked the user for a recipient address.
    const agents = files['AGENTS.md']
    expect(agents).toContain('## Bridging')
    expect(agents).toMatch(
      /The desk cannot bridge: no command moves funds from one chain to another/,
    )
    expect(agents).toMatch(/is a bridge, whatever\s+the verb/)
    expect(agents).toContain('`chuyển 0.01 ETH qua Robinhood chain`')
    expect(agents).toMatch(/It is not a send, and it has no\s+recipient to ask for/)
    expect(agents).toMatch(/Run nothing for it, not even `agentos trade status`/)
    expect(agents).toMatch(/no send to a bridge address or to the\s+wallet's own address/)
    expect(agents).toMatch(/Do not recommend, name or link a bridge/)
    expect(files['TOOLS.md']).toMatch(/No command bridges: nothing moves funds from one chain/)
  })
  it('bumped the version with the text, so every desk rewrites its files', () => {
    expect(TRADING_AGENT_VERSION).toBeGreaterThanOrEqual(9)
  })
})

describe('syncTradingAgent', () => {
  function rpcWith(agents: Array<{ id: string }>) {
    const calls: Array<[string, Record<string, unknown>]> = []
    const call = vi.fn(async (method: string, params: Record<string, unknown>) => {
      calls.push([method, params])
      if (method === 'agents.list') return { agents }
      return {}
    })
    const rpc: AgentRpc = { call: call as AgentRpc['call'] }
    return { rpc, calls }
  }

  it('creates the agent when the registry lacks it, then writes its files', async () => {
    const { rpc, calls } = rpcWith([{ id: 'main' }])
    await syncTradingAgent(rpc)
    expect(calls.map(([m]) => m)).toEqual([
      'agents.list',
      'agents.create',
      ...Array(5).fill('agents.files.set'),
    ])
    expect(calls[1]?.[1]).toMatchObject({ id: 'trading', tools: { profile: 'minimal' } })
    expect(calls[2]?.[1]).toMatchObject({ agentId: 'trading', name: 'AGENTS.md' })
  })

  it('refreshes the policy of an existing agent instead of recreating it', async () => {
    const { rpc, calls } = rpcWith([{ id: 'main' }, { id: 'trading' }])
    await syncTradingAgent(rpc)
    expect(calls[1]?.[0]).toBe('agents.update')
    expect(calls[1]?.[1]).toMatchObject({ id: 'trading', enabled: true })
    expect(calls.some(([m]) => m === 'agents.create')).toBe(false)
  })
})

describe('desk session keys', () => {
  beforeEach(() => localStorage.clear())

  it('belong to the trading agent', () => {
    expect(isTradingAgentKey(mintTradingSessionKey())).toBe(true)
    expect(isTradingAgentKey('agent:main:webchat:trading-old')).toBe(false)
  })

  it('a key minted before the desk had its own agent reads as absent', () => {
    writeTradingSessionKey('agent:main:webchat:trading-old')
    expect(readTradingSessionKey()).toBe('')
    const fresh = ensureTradingSessionKey(mintTradingSessionKey)
    expect(isTradingAgentKey(fresh)).toBe(true)
    expect(readTradingSessionKey()).toBe(fresh)
  })
})
