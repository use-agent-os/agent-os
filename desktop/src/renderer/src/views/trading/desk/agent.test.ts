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
    expect(files['AGENTS.md']).toMatch(/`everything`.*`--pct 100`/s)
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
    expect(tools).toMatch(
      /`--client-id <id>` on `swap`, `send` and `lp collect\|remove\|add` is\s+the order's idempotency key/,
    )
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
  it('bridges the same asset between Base and Robinhood Chain through `trade bridge create`, and always waits (v28)', () => {
    // Until v27 the desk refused every bridge. The engine now bridges ETH
    // and USDC↔USDG through Relay; from the desk the order always parks for
    // the user, and `--wait` only returns `confirmed` once the funds landed.
    const agents = files['AGENTS.md'] ?? ''
    expect(TRADING_AGENT_VERSION).toBeGreaterThanOrEqual(28)
    expect(agents).toContain('## Bridging')
    const section = agents.slice(agents.indexOf('## Bridging'), agents.indexOf('## The fast path'))
    expect(section).toMatch(/A bridge moves the SAME asset from one chain to the other/)
    expect(section).toMatch(/It is not a swap and not a send/)
    expect(section).toMatch(/is\s+a bridge, whatever the verb/)
    expect(section).toContain('`deposit ETH into Robinhood Chain`')
    expect(section).toContain('ETH and USDC↔USDG')
    expect(section).toMatch(/answered as unsupported/)
    expect(section).toMatch(/Leave a little ETH on the origin chain for gas/)
    expect(section).toMatch(/Quote only when the user asks for an estimate/)
    expect(section).toContain(
      '`agentos trade bridge create --chain base --to-chain robinhood --token ETH --amount 0.01 --note "user: bridge 0.01 ETH to Robinhood" --client-id bridge-eth-20260920T1015 --wait --wait-seconds 600 --json`',
    )
    expect(section).toMatch(/ALWAYS answers `status: "awaiting_approval"`/)
    expect(section).toMatch(/Never approve it yourself/)
    expect(section).toMatch(/only then reads `confirmed`/)
    for (const field of [
      'bridge.requestId',
      'destinationExplorerUrl',
      'receivedOut',
      'bridge.feesUsd',
    ])
      expect(section, field).toContain(field)
    expect(section).toMatch(/`agentos trade bridge status <orderId\|requestId> --json`/)
    expect(section).toMatch(/at most a few times in a turn, never\s+a polling loop/)
    expect(section).toMatch(
      /`failed` with a refund reason means the funds came back on the origin\s+chain/,
    )
    // The old refusal is gone everywhere.
    expect(agents).not.toMatch(/The desk cannot bridge/)
    expect(agents).not.toMatch(/Do not recommend, name or link a bridge/)
    const tools = files['TOOLS.md'] ?? ''
    expect(tools).not.toMatch(/No command bridges/)
    expect(tools).toContain(
      'agentos trade bridge quote --chain base --to-chain robinhood --token ETH',
    )
    expect(tools).toContain(
      'agentos trade bridge create --chain base --to-chain robinhood --token USDC --amount 25',
    )
    expect(tools).toContain('agentos trade bridge status <orderId|requestId> --json')
    expect(tradingAgentSpec().description).toMatch(/bridges between Base and Robinhood Chain/)
  })
  it('bumped the version with the text, so every desk rewrites its files', () => {
    expect(TRADING_AGENT_VERSION).toBeGreaterThanOrEqual(9)
  })
  it('creates a DCA as an engine mandate, never a cron job, and never approves it (v18)', () => {
    // A DCA used to be a cron job whose prompt asked the agent to count its
    // own budget. It is now a mandate the engine runs: one command, parked
    // for the user's approval, read back in one line.
    const agents = files['AGENTS.md']
    const tools = files['TOOLS.md']
    expect(TRADING_AGENT_VERSION).toBeGreaterThanOrEqual(18)
    expect(agents).toContain('## DCA')
    // The hard rule keeps its words and names its one exception.
    expect(agents).toMatch(/Never create a cron job or `cron --script` job that trades/)
    expect(agents).toMatch(/The two exceptions are a DCA mandate the user asked for/)
    expect(agents).toMatch(/A\s+DCA or a trigger is never a cron job and never one swap per turn/)
    expect(tools).toMatch(
      /Never schedule a trade \(`agentos cron …`,\s+`cron --script`\); missions are started from the desk\. The two\s+exceptions are a DCA mandate \(`agentos trade dca create`\) and a price\s+trigger \(`agentos trade trigger create`\): each parks for the user's\s+approval/,
    )
    // The reading rules.
    expect(agents).toMatch(/"DCA \$10 ETH every day, max \$300" → `--usd 10 --every 1d --cap 300`/)
    expect(agents).toMatch(/"30 buys".*→ `--runs 30`/s)
    expect(agents).toMatch(/"only under 3000".*→ `--max-price 3000`/s)
    expect(agents).toMatch(/`agentos trade dca list --json`/)
    expect(agents).toMatch(/\*\*Approve & start\*\*/)
    expect(agents).toMatch(/Always report the mandate id/)
    expect(agents).toMatch(/`agentos trade dca approve`/)
    // Every command with the CLI's own flags.
    for (const cmd of [
      'agentos trade dca create <token> --usd 10 --every 1d (--cap 300 | --runs 30 | both) [--max-price 3000] [--quote USDC] [--chain base|robinhood] [--wallet ADDR|label] [--slippage 1] [--name "DCA ETH"] [--start now|next] --json',
      'agentos trade dca list [--all] [--wallet …] --json',
      'agentos trade dca show <id> --json',
      'agentos trade dca pause|resume|stop <id> [--reason "…"] --json',
      'agentos trade dca run <id> [--wait --wait-seconds N] --json',
      'agentos trade dca update <id> [--usd X] [--cap X] [--runs N] [--every 12h] [--max-price X] [--name …] --json',
      'agentos trade dca approve|reject|pause|resume|stop|run|update',
      '`trading.dca.invalid`',
      '`trading.dca.bad_state`',
    ]) {
      expect(tools).toContain(cmd)
    }
    // English only, whatever the user writes in: the quoted example
    // phrases included (Key, 2026-10-05: no Vietnamese in the prompt).
    for (const name of ['AGENTS.md', 'TOOLS.md']) {
      expect(files[name]).not.toMatch(/[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]/i)
    }
  })
})

describe('tradingAgentFiles · price triggers (v20)', () => {
  const files = tradingAgentFiles()
  const agents = files['AGENTS.md'] ?? ''
  const tools = files['TOOLS.md'] ?? ''

  it('reads a conditional request as an engine trigger, never a poll or a cron', () => {
    expect(TRADING_AGENT_VERSION).toBeGreaterThanOrEqual(20)
    expect(agents).toContain('## Triggers')
    for (const phrase of [
      '"sell if it drops under"',
      '"cut my loss"',
      '"tell me when"',
      '"take profit at"',
      '"buy when it dips to"',
      '"stop loss 10 %"',
      '"trailing stop"',
      '"alert me when"',
    ]) {
      expect(agents, phrase).toContain(phrase)
    }
    expect(agents).toMatch(/Never\s+poll the price yourself, never schedule a cron for it/)
    // The hard rule names the trigger as its second exception.
    expect(agents).toMatch(/a price trigger the\s+user asked for \(`agentos trade trigger create`/)
  })

  it('maps each spoken rule to its flags', () => {
    const rules: [string, string][] = [
      ['"sell all my ETH if it drops under 3800"', '`--sell --pct 100 --below 3800`'],
      ['"stop loss 10 %"', '`--sell --pct 100 --below -10%`'],
      ['"take profit at +20 %"', '`--sell --pct 50 --above +20%`'],
      ['"buy $50 of ETH when it is back at 3500"', '`--buy --usd 50 --below 3500`'],
      ['"trailing stop 10 %"', '`--sell --pct 100 --trail 10`'],
      ['"tell me when ETH reaches 5000"', '`--alert --above 5000`'],
    ]
    for (const [said, flags] of rules) {
      expect(agents, said).toContain(`${said} → ${flags}`)
    }
    expect(agents).toContain(
      '`agentos trade trigger create ETH --sell --pct 100 --below 3800 --json`',
    )
    expect(agents).toMatch(/the default is `--pct 100`/)
  })

  it('reports the Approve & arm card with the id, and leaves the controls to the user', () => {
    expect(agents).toMatch(/\*\*Approve & arm\*\*/)
    expect(agents).toMatch(/trigger id \(`trg_…`\), then stop/)
    expect(agents).toContain('`agentos trade trigger list --json`')
    expect(agents).toMatch(/Never state a trigger's status from memory/)
    expect(agents).toMatch(/`agentos trade trigger approve`/)
    expect(agents).toMatch(/`trading\.operator_required`/)
  })

  it('carries every trigger command with the CLI’s own flags', () => {
    for (const cmd of [
      'agentos trade trigger create <token> (--below <price|pct%> | --above <price|pct%> | --trail <pct>) (--sell (--pct 50 | --amount 0.05 | --usd 100) | --buy --usd 50 | --alert) [--quote USDC] [--chain base|robinhood] [--wallet ADDR|label] [--slippage 1] [--name "…"] [--for 7d] --json',
      'agentos trade trigger list [--all] [--wallet …] --json',
      'agentos trade trigger show <id> --json',
      'agentos trade trigger approve|reject|pause|resume|stop|fire <id> --json',
      'agentos trade trigger approve|reject|pause|resume|stop|fire`,',
      '`trading.trigger.invalid`',
      '`trading.trigger.bad_state`',
      '`trading.trigger.not_found`',
    ]) {
      expect(tools, cmd).toContain(cmd)
    }
  })
})

describe('tradingAgentFiles · where a trigger is approved, and USDC (v21)', () => {
  const agents = tradingAgentFiles()['AGENTS.md'] ?? ''
  const section = agents.slice(agents.indexOf('## Triggers'), agents.indexOf('## Bridging'))

  it('points at the chat card and the approvals area, never the BOOK', () => {
    expect(TRADING_AGENT_VERSION).toBeGreaterThanOrEqual(21)
    expect(section.length).toBeGreaterThan(0)
    // The live desk heard "Approve & arm trong Book": the button is not there.
    expect(section).toMatch(
      /\*\*Approve & arm\*\*\s+button is on the trigger card in this chat and on the\s+same proposal in the approvals area above the composer\s+—\s+not in the BOOK/,
    )
  })

  it('needs no --quote for a USDC sell or buy: the engine trades it against the native coin', () => {
    expect(section).toMatch(/A stablecoin sell or buy — USDC itself as the token/)
    expect(section).toMatch(
      /needs no `--quote`: the engine sells it to \/ buys it with the chain's\s+native coin \(ETH\)/,
    )
    expect(section).toMatch(
      /`agentos trade trigger create USDC --sell --pct 100 --above 0\.5\s+--json`/,
    )
    expect(section).toMatch(/Never pass `--quote USDC` for a USDC trigger/)
  })
})

describe('tradingAgentFiles · brackets (v22; v23: no --note; v24: English-only examples)', () => {
  const files = tradingAgentFiles()
  const agents = files['AGENTS.md'] ?? ''
  const tools = files['TOOLS.md'] ?? ''
  const section = agents.slice(agents.indexOf('## Brackets'), agents.indexOf('## Bridging'))

  it('bumped the version and names brackets in the description', () => {
    expect(TRADING_AGENT_VERSION).toBeGreaterThanOrEqual(24)
    expect(tradingAgentSpec().description).toMatch(/brackets \(take-profit \+ stop-loss as one\)/)
    expect(agents).toContain(`trading agent v${TRADING_AGENT_VERSION}`)
  })

  it('reads an exit above and an exit below on one position as ONE bracket, never two triggers', () => {
    expect(section.startsWith('## Brackets')).toBe(true)
    for (const phrase of [
      '"protect my ETH"',
      '"protect the position"',
      '"take profit 20 %, stop loss 10 %"',
      '"take profit at 4,500 and stop at 3,400"',
      '"sell half at +20 %, stop at −10 %"',
      '"tell me if ETH leaves 3,400–4,500"',
    ]) {
      expect(section, phrase).toContain(phrase)
    }
    expect(section).toMatch(/is \*\*one bracket\*\*, never two\s+triggers/)
    expect(section).toContain('`agentos trade protect ETH --tp +20% --sl -10% --json`')
    expect(section).toMatch(/`--pct 100`/)
    expect(section).toMatch(/"take profit on half".*→ `--tp-pct 50`/)
    expect(section).toMatch(/→ `--trail 10`/)
    expect(section).toMatch(/→ `--alert`/)
    // Live 2026-10-05: the desk agent guessed `--note` from the swap examples
    // and lost a tool call to "No such option '--note'".
    expect(section).toMatch(/`protect` takes no `--note`, `--client-id` or `--wait`/)
    expect(section).toContain('`--name "…"`')
    // The Triggers section hands the two-sided case over.
    const triggers = agents.slice(agents.indexOf('## Triggers'), agents.indexOf('## Brackets'))
    expect(triggers).toMatch(/it is one bracket \(see "Brackets"\)\. A trigger is one-sided\./)
  })

  it('parks from the agent: one Approve & arm for both legs, the brk_ id, then stop', () => {
    expect(section).toMatch(/always answers `status: "awaiting_approval"`/)
    expect(section).toMatch(/one \*\*Approve & arm\*\* button for both legs/)
    expect(section).toMatch(/not in the BOOK/)
    expect(section).toMatch(/bracket id \(`brk_…`\), then stop/)
    expect(section).toMatch(/A single-sided request .* stays a trigger/s)
    expect(section).toContain('`agentos trade bracket list --json`')
    expect(section).toMatch(/Never state a bracket's status from memory/)
    expect(agents).toMatch(/`agentos trade bracket approve`/)
    // The hard rule's exception covers a bracket, by name.
    expect(agents).toMatch(
      /A\s+bracket \(`agentos trade protect`, see "Brackets"\) is two price triggers/,
    )
  })

  it('carries every bracket command with the CLI’s own flags', () => {
    for (const cmd of [
      'agentos trade protect <token> --tp <price|pct%> (--sl <price|pct%> | --trail <pct>) [--pct 100 | --amount 0.05 | --usd 100] [--tp-pct 50] [--alert] [--quote USDC] [--chain base|robinhood] [--wallet ADDR|label] [--slippage 1] [--name "…"] [--for 7d] --json',
      'agentos trade bracket list [--all] [--wallet …] --json',
      'agentos trade bracket show <id> --json',
      'agentos trade bracket approve|reject|pause|resume|stop <id> --json',
      'agentos trade bracket fire <id> [--leg tp|sl] --json',
      'agentos trade bracket approve|reject|pause|resume|stop|fire`,',
      '`trading.bracket.invalid`',
      '`trading.bracket.bad_state`',
      '`trading.bracket.not_found`',
    ]) {
      expect(tools, cmd).toContain(cmd)
    }
  })
})

describe('tradingAgentFiles · markets (v25)', () => {
  const files = tradingAgentFiles()
  const agents = files['AGENTS.md'] ?? ''
  const tools = files['TOOLS.md'] ?? ''
  const phrases = [
    '"pairs of X"',
    '"what trades against X"',
    '"markets for X"',
    '"tokens priced in X"',
    '"pools of X on every DEX"',
  ]

  it('bumped the version and names markets in the description', () => {
    expect(TRADING_AGENT_VERSION).toBeGreaterThanOrEqual(26)
    expect(agents).toContain(`trading agent v${TRADING_AGENT_VERSION}`)
    expect(tradingAgentSpec().description).toMatch(/markets \(every pool a token trades in/)
  })

  it('runs `agentos trade markets` through the shell tool the allowlist already admits', () => {
    // The allowlist names tools, not command lines: `trade markets` is one
    // more `agentos trade …` run through exec_command, like `trade lp`.
    expect(tradingAgentSpec().tools.allow).toContain('exec_command')
    expect(tools).toContain('agentos trade markets')
  })

  it('maps every market question to one markets command, and leaves one pool to lp pool', () => {
    for (const file of [agents, tools]) {
      for (const phrase of phrases) expect(file, phrase).toContain(phrase)
      expect(file).toContain('`agentos trade markets X --chain robinhood --json`')
      expect(file).toMatch(/A liquidity question about ONE pool[^.]*stays\s+`lp pool`/)
    }
    expect(agents).toMatch(/The card it\s+publishes is the answer/)
  })

  // Live test 2026-10-06: "show me the AI/NVDA pool" got `pool_key_unknown`
  // from `lp pool` (a Bankr pool), and the agent published NVDA/USDG's card
  // instead — a different pool, presented as the answer.
  it('answers a launchpad pool lp pool cannot key with the markets card of its quote token', () => {
    for (const file of [agents, tools]) {
      expect(file).toContain('`trading.lp.pool_key_unknown`')
      expect(file).toMatch(
        /`trading\.lp\.pool_key_unknown` is answered\s+with\s+`agentos trade markets <quote token> --chain robinhood --json`/,
      )
      expect(file).toMatch(/Never publish a\s+different\s+pool's card in its place/)
    }
  })

  it('carries the command with the CLI’s own flags, its default chain and its errors', () => {
    expect(tools).toContain(
      'agentos trade markets <token> [--chain base|robinhood] [--side all|quote|base] [--min-tvl 10000] [--limit 50] [--lookalikes] [--deep] --json',
    )
    expect(tools).toMatch(/default chain is robinhood/)
    expect(tools).toMatch(/do not call `publish_artifact` for\s+it/)
    expect(tools).toMatch(/`partial: true` means the source rate-limited the read/)
    for (const code of [
      '`trading.markets.unavailable`',
      '`trading.not_found`',
      '`trading.invalid`',
    ]) {
      expect(tools, code).toContain(code)
    }
  })

  it('no longer calls Stock Tokens untradeable: they route through Uniswap', () => {
    expect(agents).toMatch(/Stock Tokens \(AAPL, TSLA, NVDA …\) route through\s+Uniswap/)
    expect(agents).not.toMatch(/the venue refuses them for legal reasons/)
    expect(tools).toMatch(/the message says to add a Uniswap key/)
  })

  it('stays English only', () => {
    for (const file of [agents, tools]) {
      expect(file).not.toMatch(/[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]/i)
    }
  })
})

// Live test 2026-10-06 (second round): summarising NVDA's card, the agent
// called USDG and WETH NVDA's "quote-side markets" — the pools where NVDA is
// bought and sold, shown under *NVDA priced in*.
describe('tradingAgentFiles · naming the markets sections (v27)', () => {
  const files = tradingAgentFiles()
  const agents = files['AGENTS.md'] ?? ''
  const tools = files['TOOLS.md'] ?? ''

  it('bumped the version', () => {
    expect(TRADING_AGENT_VERSION).toBe(28)
    expect(agents).toContain('trading agent v28')
  })

  it('says which section is which, and never calls USDG/WETH quote-side markets', () => {
    for (const file of [agents, tools]) {
      expect(file).toMatch(/\*X priced in\*\)? (?:are|is)?[^.]*?X itself is bought\s+and\s+sold/)
      expect(file).toMatch(/\*Priced in X\*\)?[^.]*?other tokens quoted in X/)
      expect(file).toMatch(/Never call USDG or\s+WETH\s+"quote-side markets" of X\./)
    }
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

  it('never reports a mandate status from memory (v19)', () => {
    const agents = tradingAgentFiles()['AGENTS.md'] ?? ''
    expect(agents).toContain('Never state a mandate')
    expect(agents).toContain('in the same turn first')
    expect(TRADING_AGENT_VERSION).toBeGreaterThanOrEqual(19)
  })

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
