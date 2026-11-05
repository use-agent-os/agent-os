import { formatAmount, formatUsd } from '../logic'
import { providerLabel, type OrderStatus, type ProviderId } from '../types'

/**
 * Trade ledger rows: what a `agentos trade …` / `agentos wallet …` call the
 * agent ran means in one line. Parsing is best-effort and total: a call we
 * cannot read still gets a row from its command line, and a result we cannot
 * parse leaves the raw block untouched.
 */

export type TradeKind =
  | 'quote'
  | 'swap'
  | 'send'
  | 'order'
  | 'orders'
  | 'approve'
  | 'reject'
  | 'allowances'
  | 'revoke'
  | 'decode'
  | 'network'
  | 'portfolio'
  | 'balances'
  | 'history'
  | 'status'
  | 'tokens'
  | 'limits'
  | 'sync'
  | 'wallet'
  | 'lp'
  | 'lp_collect'
  | 'lp_remove'
  | 'lp_add'
  | 'bridge'
  | 'bridge_quote'
  | 'bridge_status'
  | 'dca'
  | 'dca_list'
  | 'dca_create'
  | 'dca_approve'
  | 'dca_reject'
  | 'dca_pause'
  | 'dca_resume'
  | 'dca_run'
  | 'dca_stop'
  | 'dca_update'
  | 'trigger'
  | 'trigger_list'
  | 'trigger_create'
  | 'trigger_approve'
  | 'trigger_reject'
  | 'trigger_pause'
  | 'trigger_resume'
  | 'trigger_stop'
  | 'trigger_fire'
  | 'bracket'
  | 'bracket_list'
  | 'bracket_create'
  | 'bracket_approve'
  | 'bracket_reject'
  | 'bracket_pause'
  | 'bracket_resume'
  | 'bracket_stop'
  | 'bracket_fire'
  | 'other'

export interface TradeCall {
  kind: TradeKind
  /** "Swap", "Quote", "Balances" … */
  title: string
  /** Argument summary from the command line, e.g. "0.01 ETH → USDC · Base". */
  detail: string
  command: string
}

const TRADE_RE = /(?:^|[\s;&|])(?:uv run\s+)?agentos\s+(trade|wallet)\s+([a-z-]+)((?:\s+\S+)*)/i

/** The command text out of an exec_command input (object or JSON string). */
export function commandFromToolInput(input: unknown): string | null {
  if (!input) return null
  if (typeof input === 'string') {
    const trimmed = input.trim()
    if (!trimmed) return null
    if (trimmed.startsWith('{')) {
      try {
        const parsed = JSON.parse(trimmed) as { command?: unknown }
        return typeof parsed.command === 'string' ? parsed.command : null
      } catch {
        // A truncated preview: pull the command field by hand.
        const m = /"command"\s*:\s*"((?:[^"\\]|\\.)*)/.exec(trimmed)
        return m ? m[1]!.replace(/\\"/g, '"').replace(/\\n/g, '\n') : null
      }
    }
    return trimmed
  }
  if (typeof input === 'object' && 'command' in (input as Record<string, unknown>)) {
    const c = (input as { command?: unknown }).command
    return typeof c === 'string' ? c : null
  }
  return null
}

function flag(args: string, name: string): string | null {
  const m = new RegExp(`--${name}(?:=|\\s+)("[^"]*"|'[^']*'|\\S+)`).exec(args)
  return m ? m[1]!.replace(/^['"]|['"]$/g, '') : null
}

/** Every value of a repeatable flag, e.g. the `--to` entries of a send. */
function flags(args: string, name: string): string[] {
  const re = new RegExp(`--${name}(?:=|\\s+)("[^"]*"|'[^']*'|\\S+)`, 'g')
  const out: string[] = []
  for (const m of args.matchAll(re)) out.push(m[1]!.replace(/^['"]|['"]$/g, ''))
  return out
}

function shortAddr(value: string): string {
  return /^0x[0-9a-fA-F]{40}$/.test(value) ? `${value.slice(0, 6)}…${value.slice(-4)}` : value
}

function chainWord(args: string, name = 'chain'): string {
  const c = (flag(args, name) || '').toLowerCase()
  if (c === 'base' || c === '8453') return 'Base'
  if (c === 'robinhood' || c === '4663') return 'Robinhood'
  return ''
}

/** `trade lp` / `trade dca` flags that take no value; every other `--name` consumes the next word. */
const LP_BOOLEAN_FLAGS = new Set(['--json', '--no-card', '--all', '--help', '--wait'])

/** `trade dca <sub>`: each subcommand is its own row kind (docs/dca.md). */
const DCA_SUBS: Record<string, TradeKind> = {
  create: 'dca_create',
  list: 'dca_list',
  show: 'dca',
  approve: 'dca_approve',
  reject: 'dca_reject',
  pause: 'dca_pause',
  resume: 'dca_resume',
  run: 'dca_run',
  stop: 'dca_stop',
  update: 'dca_update',
}

/** `trade trigger <sub>`: each subcommand is its own row kind (docs/triggers.md). */
const TRIGGER_SUBS: Record<string, TradeKind> = {
  create: 'trigger_create',
  list: 'trigger_list',
  show: 'trigger',
  approve: 'trigger_approve',
  reject: 'trigger_reject',
  pause: 'trigger_pause',
  resume: 'trigger_resume',
  stop: 'trigger_stop',
  fire: 'trigger_fire',
}

/** Every trigger row kind. */
export function isTriggerKind(kind: TradeKind): boolean {
  return kind === 'trigger' || kind.startsWith('trigger_')
}

/**
 * `trade bracket <sub>`: each subcommand is its own row kind; the creating
 * verb is `trade protect` (docs/brackets.md, "CLI").
 */
const BRACKET_SUBS: Record<string, TradeKind> = {
  list: 'bracket_list',
  show: 'bracket',
  approve: 'bracket_approve',
  reject: 'bracket_reject',
  pause: 'bracket_pause',
  resume: 'bracket_resume',
  stop: 'bracket_stop',
  fire: 'bracket_fire',
}

/** Every bracket row kind (take-profit + stop-loss as one object). */
export function isBracketKind(kind: TradeKind): boolean {
  return kind === 'bracket' || kind.startsWith('bracket_')
}

/** One `trade protect` line: "+20 %" / "−10 %" for a percent, "$4,560" for a price. */
function bracketLineWord(value: string, side: 'up' | 'down'): string {
  const v = value.trim()
  const pct = /^([-+−]?)(\d+(?:\.\d+)?)%$/.exec(v)
  if (pct) return `${side === 'up' ? '+' : '−'}${pct[2]} %`
  const n = Number(v.replace(/[$,]/g, ''))
  if (!Number.isFinite(n) || n <= 0) return v
  return n >= 1000
    ? `$${Math.round(n).toLocaleString('en-US')}`
    : `$${n.toLocaleString('en-US', { maximumFractionDigits: 6 })}`
}

/** "+20 % / −10 %", "$4,560 / $3,420", "+20 % / trail 10 %": the two lines of a `trade protect`. */
function bracketLinesWord(args: string): string {
  const tp = flag(args, 'tp')
  const sl = flag(args, 'sl')
  const trail = flag(args, 'trail')
  const up = tp ? bracketLineWord(tp, 'up') : ''
  const down = sl ? bracketLineWord(sl, 'down') : trail ? `trail ${trail.replace(/%$/, '')} %` : ''
  return up || down ? `${up || '—'} / ${down || '—'}` : ''
}

/** `trade trigger create` flags that take no value (beside the shared ones). */
const TRIGGER_BOOLEAN_FLAGS = new Set(['--sell', '--buy', '--alert'])

/**
 * What a `trigger create` command line arms, as its row title says it:
 * "Stop-loss", "Take-profit", "Trailing stop", "Buy the dip", "Buy the
 * breakout", "Price alert".
 */
function triggerCreateTitle(args: string): string {
  const has = (name: string) => new RegExp(`(?:^|\\s)--${name}(?:\\s|=|$)`).test(args)
  if (has('alert')) return 'Price alert'
  if (has('buy')) return has('above') ? 'Buy the breakout' : 'Buy the dip'
  if (has('trail')) return 'Trailing stop'
  if (has('above')) return 'Take-profit'
  return 'Stop-loss'
}

/** "$3,800" for an absolute price, "10 % under now" / "15 % over now" for a percent. */
function triggerPriceWord(value: string, side: 'under' | 'over'): string {
  const v = value.trim()
  const pct = /^([+-]?)(\d+(?:\.\d+)?)%$/.exec(v)
  if (pct) return `${pct[2]} % ${side} now`
  const n = Number(v.replace(/[$,]/g, ''))
  if (!Number.isFinite(n) || n <= 0) return `${side} ${v}`
  const text =
    n >= 1000
      ? `$${Math.round(n).toLocaleString('en-US')}`
      : `$${n.toLocaleString('en-US', { maximumFractionDigits: 6 })}`
  return `${side} ${text}`
}

/** "under $3,800", "10 % under now", "over $5,000", "10 % below peak" from the flags. */
function triggerConditionWord(args: string): string {
  const below = flag(args, 'below')
  if (below) return triggerPriceWord(below, 'under')
  const above = flag(args, 'above')
  if (above) return triggerPriceWord(above, 'over')
  const trail = flag(args, 'trail')
  if (trail) return `${trail.replace(/%$/, '')} % below peak`
  return ''
}

/** The positional words of a `trade trigger` command line. */
function triggerPositionals(args: string): string[] {
  const out: string[] = []
  const words = args.trim().split(/\s+/).filter(Boolean)
  for (let i = 0; i < words.length; i++) {
    const w = words[i]!
    if (!w.startsWith('--')) {
      out.push(w)
      continue
    }
    if (w.includes('=') || LP_BOOLEAN_FLAGS.has(w) || TRIGGER_BOOLEAN_FLAGS.has(w)) continue
    if (words[i + 1] && !words[i + 1]!.startsWith('--')) i++
  }
  return out
}

/** Every DCA row kind. */
export function isDcaKind(kind: TradeKind): boolean {
  return kind === 'dca' || kind.startsWith('dca_')
}

/** `--every` as the row says it after the slash: "1d" → "day", "6h" → "6 h", "30m" → "30 min". */
function dcaEvery(word: string | null): string {
  if (!word) return ''
  const m = /^(\d+(?:\.\d+)?)([smhdw]?)$/i.exec(word.trim())
  if (!m) return word
  const n = Number(m[1])
  const unit = (m[2] || 's').toLowerCase()
  const seconds = n * ({ s: 1, m: 60, h: 3_600, d: 86_400, w: 604_800 } as const)[unit as 's']
  if (seconds === 86_400) return 'day'
  if (seconds === 604_800) return 'week'
  if (seconds === 3_600) return 'hour'
  if (seconds % 604_800 === 0) return `${seconds / 604_800} w`
  if (seconds % 86_400 === 0) return `${seconds / 86_400} d`
  if (seconds % 3_600 === 0) return `${seconds / 3_600} h`
  if (seconds % 60 === 0) return `${seconds / 60} min`
  return `${seconds} s`
}

/** The `trade lp` subcommands that write: each parks an order of its own kind. */
const LP_WRITES: Record<string, TradeKind> = {
  collect: 'lp_collect',
  remove: 'lp_remove',
  add: 'lp_add',
}

/** `agentos trade bridge <sub>`: a quote, the order itself, or a look at its fill. */
const BRIDGE_SUBS: Record<string, TradeKind> = {
  quote: 'bridge_quote',
  create: 'bridge',
  status: 'bridge_status',
}

/** "#48213" for a token id, the word as given otherwise. */
function tokenIdWord(word: string | undefined): string {
  if (!word) return ''
  return /^\d+$/.test(word) ? `#${word}` : word
}

/** The size of an `lp add`: "$50", "1,000 PEPE", "0.5 PEPE + 0.01 WETH"-ish from the flags. */
function lpAddSize(args: string, subject: string): string {
  const usd = flag(args, 'usd')
  if (usd) return `$${usd.replace(/^\$/, '')}`
  const base = flag(args, 'amount-base')
  const quote = flag(args, 'amount-quote')
  const parts = [
    base ? `${base}${subject ? ` ${subject}` : ''}` : '',
    quote ? `${quote} quote` : '',
  ]
  return parts.filter(Boolean).join(' + ')
}

/** The positional words of a `trade lp` command line, with every flag and flag value dropped. */
function lpPositionals(args: string): string[] {
  const out: string[] = []
  const words = args.trim().split(/\s+/).filter(Boolean)
  for (let i = 0; i < words.length; i++) {
    const w = words[i]!
    if (!w.startsWith('--')) {
      out.push(w)
      continue
    }
    if (w.includes('=') || LP_BOOLEAN_FLAGS.has(w)) continue
    if (words[i + 1] && !words[i + 1]!.startsWith('--')) i++
  }
  return out
}

const TITLES: Record<TradeKind, string> = {
  quote: 'Quote',
  swap: 'Swap',
  send: 'Send',
  order: 'Order',
  orders: 'Orders',
  approve: 'Approve',
  reject: 'Reject',
  allowances: 'Allowances',
  revoke: 'Revoke',
  decode: 'Decode',
  network: 'Network',
  portfolio: 'Portfolio',
  balances: 'Balances',
  history: 'History',
  status: 'Desk status',
  tokens: 'Token search',
  limits: 'Limits',
  sync: 'Sync',
  wallet: 'Wallet',
  lp: 'Liquidity read',
  lp_collect: 'Collect fees',
  lp_remove: 'Remove liquidity',
  lp_add: 'Add liquidity',
  bridge_quote: 'Bridge quote',
  bridge: 'Bridge',
  bridge_status: 'Bridge status',
  dca: 'DCA status',
  dca_list: 'DCA list',
  dca_create: 'Start DCA',
  dca_approve: 'Approve DCA',
  dca_reject: 'Reject DCA',
  dca_pause: 'Pause DCA',
  dca_resume: 'Resume DCA',
  dca_run: 'Buy now',
  dca_stop: 'Stop DCA',
  dca_update: 'Update DCA',
  trigger: 'Trigger status',
  trigger_list: 'Triggers',
  trigger_create: 'New trigger',
  trigger_approve: 'Approve trigger',
  trigger_reject: 'Reject trigger',
  trigger_pause: 'Pause trigger',
  trigger_resume: 'Resume trigger',
  trigger_stop: 'Stop trigger',
  trigger_fire: 'Fire now',
  bracket: 'Bracket status',
  bracket_list: 'Brackets',
  bracket_create: 'Protect',
  bracket_approve: 'Approve bracket',
  bracket_reject: 'Reject bracket',
  bracket_pause: 'Pause bracket',
  bracket_resume: 'Resume bracket',
  bracket_stop: 'Stop bracket',
  bracket_fire: 'Fire now',
  other: 'Trade call',
}

/** Recognise a desk command; null for anything else the agent ran. */
export function parseTradeCommand(command: string | null | undefined): TradeCall | null {
  if (!command) return null
  const m = TRADE_RE.exec(command)
  if (!m) return null
  const group = m[1]!.toLowerCase()
  const sub = m[2]!.toLowerCase()
  const args = m[3] ?? ''
  let kind: TradeKind = 'other'
  if (group === 'trade') {
    if (
      (
        [
          'quote',
          'swap',
          'send',
          'order',
          'orders',
          'approve',
          'reject',
          'allowances',
          'revoke',
          'decode',
          'network',
          'portfolio',
          'history',
          'status',
          'tokens',
          'limits',
          'sync',
        ] as const
      ).includes(sub as never)
    ) {
      kind = sub as TradeKind
    } else if (sub === 'lp') {
      kind = LP_WRITES[(lpPositionals(args)[0] ?? '').toLowerCase()] ?? 'lp'
    } else if (sub === 'bridge') {
      kind = BRIDGE_SUBS[(lpPositionals(args)[0] ?? '').toLowerCase()] ?? 'bridge'
    } else if (sub === 'dca') {
      kind = DCA_SUBS[(lpPositionals(args)[0] ?? '').toLowerCase()] ?? 'dca'
    } else if (sub === 'trigger') {
      kind = TRIGGER_SUBS[(triggerPositionals(args)[0] ?? '').toLowerCase()] ?? 'trigger'
    } else if (sub === 'protect') {
      kind = 'bracket_create'
    } else if (sub === 'bracket') {
      kind = BRACKET_SUBS[(triggerPositionals(args)[0] ?? '').toLowerCase()] ?? 'bracket'
    }
  } else if (sub === 'balances') kind = 'balances'
  else kind = 'wallet'

  let detail = ''
  let title = TITLES[kind]
  if (kind === 'quote' || kind === 'swap') {
    const tin = flag(args, 'in')
    const tout = flag(args, 'out')
    const amount = flag(args, 'amount')
    const pct = flag(args, 'pct')
    const legs =
      tin && tout ? `${amount ? `${amount} ` : pct ? `${pct}% ` : ''}${tin} → ${tout}` : ''
    detail = [legs, chainWord(args)].filter(Boolean).join(' · ')
  } else if (kind === 'send') {
    const token = flag(args, 'token') ?? ''
    const to = flags(args, 'to')
    const amount = flag(args, 'amount')
    const usd = flag(args, 'usd')
    const size = amount ? `${amount} ${token}` : usd ? `$${usd} of ${token}` : token
    const who =
      to.length === 1
        ? `→ ${shortAddr(to[0]!.split('=')[0]!)}`
        : to.length > 1
          ? `→ ${to.length} recipients`
          : flag(args, 'file')
            ? '→ a list'
            : ''
    detail = [[size, who].filter(Boolean).join(' '), chainWord(args)].filter(Boolean).join(' · ')
  } else if (kind === 'revoke') {
    const token = flag(args, 'token')
    const spender = flag(args, 'spender')
    detail = [
      [token ? shortAddr(token) : '', spender ? `for ${shortAddr(spender)}` : '']
        .filter(Boolean)
        .join(' '),
      chainWord(args),
    ]
      .filter(Boolean)
      .join(' · ')
  } else if (kind === 'decode') {
    const hash = args
      .trim()
      .split(/\s+/)
      .find((a) => /^0x[0-9a-fA-F]{64}$/.test(a))
    detail = [
      hash ? `${hash.slice(0, 10)}…` : flag(args, 'data') ? 'calldata' : '',
      chainWord(args),
    ]
      .filter(Boolean)
      .join(' · ')
  } else if (kind === 'tokens') {
    const q = args
      .trim()
      .split(/\s+/)
      .filter((a) => !a.startsWith('--') && a !== chainWord(args).toLowerCase())
    detail = [q[q.length - 1] ?? '', chainWord(args)].filter(Boolean).join(' · ')
  } else if (kind === 'order' || kind === 'approve' || kind === 'reject') {
    const id = args
      .trim()
      .split(/\s+/)
      .find((a) => a && !a.startsWith('--'))
    detail = id ?? ''
  } else if (kind === 'wallet') {
    detail = sub
  } else if (kind === 'lp_collect' || kind === 'lp_remove') {
    // `lp remove 48213 --pct 50` → "#48213 · 50%"; a remove without --pct takes it all.
    const id = tokenIdWord(lpPositionals(args)[1])
    const pct = kind === 'lp_remove' ? `${(flag(args, 'pct') ?? '100').replace(/%$/, '')}%` : ''
    detail = [id, pct, chainWord(args)].filter(Boolean).join(' · ')
  } else if (kind === 'bridge' || kind === 'bridge_quote') {
    // `bridge create --chain base --to-chain robinhood --token ETH --amount 0.01`
    // → "0.01 ETH · Base → Robinhood".
    const token = flag(args, 'token') ?? ''
    const amount = flag(args, 'amount')
    const usd = flag(args, 'usd')
    const pct = flag(args, 'pct')
    const size = amount
      ? `${amount} ${token}`
      : usd
        ? `$${usd} of ${token}`
        : pct
          ? `${pct}% of ${token}`
          : token
    const from = chainWord(args)
    const to = chainWord(args, 'to-chain')
    detail = [size.trim(), from && to ? `${from} → ${to}` : from || to].filter(Boolean).join(' · ')
  } else if (kind === 'bridge_status') {
    // `bridge status ord_123 --json` → "ord_123" (an order id or a Relay request id).
    detail = lpPositionals(args)[1] ?? ''
  } else if (kind === 'lp_add') {
    // `lp add PEPE --usd 50 --range mcap:2M-10M` → "PEPE · $50 · Base"; the
    // result names the pair once it is known.
    const subject = lpPositionals(args)[1] ?? ''
    const shown = /^0x[0-9a-fA-F]{40,64}$/.test(subject) ? shortAddr(subject) : subject
    const to = flag(args, 'to-position')
    detail = [shown, lpAddSize(args, shown), to ? `→ ${tokenIdWord(to)}` : '', chainWord(args)]
      .filter(Boolean)
      .join(' · ')
  } else if (kind === 'dca_create') {
    // `dca create ETH --usd 10 --every 1d --cap 300` → "ETH · $10 / day".
    const token = lpPositionals(args)[1] ?? ''
    const shown = /^0x[0-9a-fA-F]{40}$/.test(token) ? shortAddr(token) : token.toUpperCase()
    const usd = flag(args, 'usd')
    const every = dcaEvery(flag(args, 'every'))
    const size = usd ? `$${usd.replace(/^\$/, '')}${every ? ` / ${every}` : ''}` : ''
    detail = [shown, size, chainWord(args)].filter(Boolean).join(' · ')
  } else if (kind === 'dca_list') {
    detail = /(?:^|\s)--all(?:\s|$)/.test(args) ? 'all' : ''
  } else if (isDcaKind(kind)) {
    // Every other DCA subcommand names one mandate: its id is the detail.
    detail = lpPositionals(args)[1] ?? ''
  } else if (kind === 'trigger_create') {
    // `trigger create ETH --sell --pct 50 --below 3800` → "Stop-loss" · "ETH · under $3,800".
    const token = triggerPositionals(args)[1] ?? ''
    const shown = /^0x[0-9a-fA-F]{40}$/.test(token) ? shortAddr(token) : token.toUpperCase()
    title = triggerCreateTitle(args)
    detail = [shown, triggerConditionWord(args), chainWord(args)].filter(Boolean).join(' · ')
  } else if (kind === 'trigger_list') {
    detail = /(?:^|\s)--all(?:\s|$)/.test(args) ? 'all' : ''
  } else if (isTriggerKind(kind)) {
    // Every other trigger subcommand names one trigger: its id is the detail.
    detail = triggerPositionals(args)[1] ?? ''
  } else if (kind === 'bracket_create') {
    // `protect ETH --tp +20% --sl -10%` → "Protect" · "ETH · +20 % / −10 %";
    // a range alert (`--alert`) is a watch, not a protection.
    const token = triggerPositionals(args)[0] ?? ''
    const shown = /^0x[0-9a-fA-F]{40}$/.test(token) ? shortAddr(token) : token.toUpperCase()
    if (/(?:^|\s)--alert(?:\s|=|$)/.test(args)) title = 'Watch'
    detail = [shown, bracketLinesWord(args), chainWord(args)].filter(Boolean).join(' · ')
  } else if (kind === 'bracket_list') {
    detail = /(?:^|\s)--all(?:\s|$)/.test(args) ? 'all' : ''
  } else if (isBracketKind(kind)) {
    // Every other bracket subcommand names one bracket: its id is the detail.
    detail = triggerPositionals(args)[1] ?? ''
  } else if (kind === 'lp') {
    // `lp pool boar --chain base` → "pool boar · Base"; flags and their values
    // (`--budget-seconds 60`, `--wallet=0x…`) never reach the subject.
    detail = [lpPositionals(args).slice(0, 2).join(' '), chainWord(args)]
      .filter(Boolean)
      .join(' · ')
  } else {
    detail = chainWord(args)
  }
  return { kind, title, detail, command: command.trim() }
}

export interface TradeOutcome {
  summary: string
  /** Replaces the call's command-line detail once the result names its subject better. */
  detail?: string
  status: OrderStatus | null
  orderId: string | null
  txHash: string | null
  explorerUrl: string | null
  /** The chain the order ran on, so a hash without a link can still get one. */
  chainId: number | null
  provider: ProviderId | null
  /** Earned, not decorative: the result names a pending approval. */
  awaiting: boolean
  /** Earned: the result carries a confirmed status AND a tx hash. */
  confirmed: boolean
  error: string | null
  /** When the market figures in the result were read, if the result says. */
  marketAt: number | null
  /** A DCA row: the mandate it names, and the status its result recorded (docs/dca.md). */
  mandateId?: string | null
  mandateStatus?: string | null
  /**
   * The mandate's status as the live list has it now, set only by
   * `withLiveMandate`: the row's pill follows it instead of the recorded one.
   */
  mandateLive?: string | null
  /**
   * A trigger row: the trigger it names, and the status its result recorded
   * (docs/triggers.md). A bracket row (docs/brackets.md) carries its `brk_…`
   * id here too: one status vocabulary, one pill.
   */
  triggerId?: string | null
  triggerStatus?: string | null
  /** The trigger's status as the live list has it now, set only by `withLiveTrigger`. */
  triggerLive?: string | null
}

const EMPTY: TradeOutcome = {
  summary: '',
  status: null,
  orderId: null,
  txHash: null,
  explorerUrl: null,
  chainId: null,
  provider: null,
  awaiting: false,
  confirmed: false,
  error: null,
  marketAt: null,
}

type Dict = Record<string, unknown>
const isDict = (v: unknown): v is Dict => typeof v === 'object' && v !== null && !Array.isArray(v)
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const sym = (v: unknown): string =>
  isDict(v) ? (str(v.symbol) ?? '') : typeof v === 'string' ? v : ''

/** The exit-code line exec_command puts first: `exit_code=1\n…`. */
export function exitCodeOf(text: string): number | null {
  const m = /^\s*exit_code=(-?\d+)/.exec(text)
  return m ? Number(m[1]) : null
}

/**
 * `trade status --json` cut past valid JSON (a stored result keeps ~2,000
 * characters; the chain and provider rows run past that). `provider` is the
 * third key, so it survives; `unlocked` comes after the chains and usually
 * does not. Null when the text is not a status document at all.
 */
function statusFromTruncated(text: string): TradeOutcome | null {
  if (!/^\s*(?:exit_code=\d+\s*)?\{\s*"enabled"\s*:/.test(text)) return null
  const provider = jsonString(text, 'provider')
  const unlocked = /"unlocked"\s*:\s*(true|false)/.exec(text)?.[1]
  const bits: string[] = []
  if (provider) bits.push(`via ${providerLabel(provider)}`)
  if (unlocked === 'true') bits.push('vault unlocked')
  if (unlocked === 'false') bits.push('vault locked')
  return { ...EMPTY, summary: bits.join(' · '), provider: provider as ProviderId | null }
}

/** The JSON document inside a tool result, ignoring the exit-code line and stray output. */
function parseJson(text: string): unknown {
  const start = text.search(/[{[]/)
  if (start < 0) return null
  const body = text.slice(start)
  const end = Math.max(body.lastIndexOf('}'), body.lastIndexOf(']'))
  if (end < 0) return null
  try {
    return JSON.parse(body.slice(0, end + 1))
  } catch {
    return null
  }
}

/** A plan or receipt amount pair as one line: "1,240,000 PEPE + 0.00184 WETH". */
function lpAmountsLine(pair: unknown, base: string, quote: string): string {
  if (!isDict(pair)) return ''
  const side = (a: unknown, symbol: string): string | null => {
    const human = isDict(a) ? str(a.human) : null
    return human && Number(human) > 0 ? `${formatAmount(human)} ${symbol}` : null
  }
  const sides = [side(pair.base, base), side(pair.quote, quote)].filter(Boolean)
  return sides.length ? sides.join(' + ') : `0 ${base} + 0 ${quote}`
}

/** "PEPE/WETH" from the plan, else from the order's two tokens. */
function lpPair(o: Dict, plan: Dict | null): [string, string] {
  const base = (plan && sym(plan.token)) || sym(o.tokenIn)
  const quote = (plan && sym(plan.quote)) || sym(o.tokenOut)
  return [base, quote]
}

/** An LP write's subject once the order is known: "#48213", "#48213 · 100%", "PEPE/WETH · $50.00". */
function lpOrderDetail(o: Dict): string {
  const kind = str(o.kind)
  const plan = isDict(o.plan) ? o.plan : null
  const id = str(o.tokenId) ?? (plan ? str(plan.tokenId) : null)
  const chain = plan && isDict(plan.chain) ? (str(plan.chain.name) ?? '') : ''
  if (kind === 'lp_add') {
    const [base, quote] = lpPair(o, plan)
    const usd = num(o.valueUsd) ?? (plan && isDict(plan.expected) ? num(plan.expected.usd) : null)
    const increase = plan?.increase === true
    return [
      base && quote ? `${base}/${quote}` : base,
      usd !== null ? formatUsd(usd) : '',
      id ? (increase ? `→ #${id}` : `#${id}`) : '',
      chain,
    ]
      .filter(Boolean)
      .join(' · ')
  }
  const pct = kind === 'lp_remove' && plan && num(plan.pct) !== null ? `${num(plan.pct)}%` : ''
  return [id ? `#${id}` : '', pct, chain].filter(Boolean).join(' · ')
}

/** What an LP write moves: expected while it waits, what the receipt says once confirmed. */
function lpOrderLegs(o: Dict): string {
  const kind = str(o.kind)
  const plan = isDict(o.plan) ? o.plan : null
  const [base, quote] = lpPair(o, plan)
  const confirmed = o.status === 'confirmed'
  const moved = kind === 'lp_add' ? o.spent : o.received
  const line = lpAmountsLine(
    confirmed && isDict(moved) ? moved : plan ? plan.expected : null,
    base,
    quote,
  )
  if (!line) return ''
  return kind === 'lp_add' ? `deposit ${line}` : `receive ${line}`
}

function orderLegs(o: Dict): string {
  const kind = str(o.kind) ?? 'swap'
  if (kind === 'lp_collect' || kind === 'lp_remove' || kind === 'lp_add') return lpOrderLegs(o)
  const amount = `${formatAmount(str(o.amountIn))} ${sym(o.tokenIn)}`
  if (kind === 'bridge') {
    // "0.01 ETH → Robinhood Chain (≈ 0.0099 ETH)": the asset keeps its name.
    const bridge = isDict(o.bridge) ? o.bridge : null
    const dest = bridge && isDict(bridge.destinationChain) ? bridge.destinationChain : null
    const destId = bridge ? num(bridge.destinationChainId) : null
    const to = (dest ? str(dest.name) : null) ?? (destId !== null ? `chain ${destId}` : '')
    const confirmed = o.status === 'confirmed'
    const out = confirmed ? (str(o.receivedOut) ?? str(o.expectedOut)) : str(o.expectedOut)
    const landed = out ? ` (${confirmed ? '' : '≈ '}${formatAmount(out)} ${sym(o.tokenOut)})` : ''
    return `${amount} → ${to}${landed}`.trim()
  }
  if (kind === 'send') return `${amount} → ${shortAddr(str(o.recipient) ?? '')}`.trim()
  if (kind === 'revoke') {
    const who = str(o.recipientLabel) ?? shortAddr(str(o.recipient) ?? '')
    return `revoke ${sym(o.tokenIn)} for ${who}`.trim()
  }
  return `${amount} → ${
    str(o.expectedOut) ? `${formatAmount(str(o.expectedOut))} ` : ''
  }${sym(o.tokenOut)}`.trim()
}

function fromOrder(o: Dict): TradeOutcome {
  const status = str(o.status) as OrderStatus | null
  const txHash = str(o.txHash)
  const legs = orderLegs(o)
  const kind = str(o.kind)
  const lpWrite = kind === 'lp_collect' || kind === 'lp_remove' || kind === 'lp_add'
  // An LP write goes to the PositionManager, never through a swap route: the
  // order's default provider field names nothing here.
  const provider = lpWrite ? null : ((str(o.provider) as ProviderId | null) ?? null)
  const bits = [legs]
  if (status) bits.push(statusWordFor(status))
  if (str(o.reason) && (status === 'rejected' || status === 'failed' || status === 'expired'))
    bits.push(String(o.reason))
  return {
    ...EMPTY,
    summary: bits.filter(Boolean).join(' · '),
    ...(lpWrite && lpOrderDetail(o) ? { detail: lpOrderDetail(o) } : {}),
    status,
    orderId: str(o.orderId),
    txHash,
    explorerUrl: str(o.explorerUrl),
    chainId: num(o.chainId),
    provider,
    awaiting: status === 'awaiting_approval',
    confirmed: status === 'confirmed' && Boolean(txHash),
    error: status === 'failed' ? (str(o.reason) ?? 'failed') : null,
  }
}

function statusWordFor(status: OrderStatus): string {
  switch (status) {
    case 'awaiting_approval':
      return 'awaiting approval'
    case 'submitted':
      return 'sent'
    default:
      return status
  }
}

/** A projection marker the transcript substitutes for a result body it did not keep. */
const PROJECTION_MARKER = /^\s*\[[a-z_]+_projection\]\s*$/i

/**
 * The LP card announcement `agentos trade lp …` prints last on stdout, the
 * note publish_inline_artifacts swaps in for it on a live result, or the
 * omission marker a history projection leaves in its place.
 */
const LP_CARD_MARKER = new RegExp(
  [
    // The raw announcement, as the CLI prints it.
    /publish_artifact\s+path=\S+\s+mime=application\/vnd\.agentos\.lp\+json/.source,
    // A live result: publish_inline_artifacts rewrote the announcement into this
    // note (no mime left), so the card directory is the only LP signal.
    /\[inline artifact published and already rendered for the user:\s*(?:\S*\/)?lp-cards\/[^\s\]]+/
      .source,
    // A history projection that dropped the body.
    /\[generated artifact omitted:[^\]\n]*application\/vnd\.agentos\.lp\+json/.source,
  ].join('|'),
  'i',
)

/**
 * A liquidity read recognised by its result alone (the command was wrapped
 * past recognition): the result announces an LP card.
 */
export function lpCallFromResult(text: string): TradeCall | null {
  if (!LP_CARD_MARKER.test(text)) return null
  return { kind: 'lp', title: TITLES.lp, detail: '', command: '' }
}

/**
 * The DCA card announcement `agentos trade dca … --json` prints last, in the
 * same three shapes as the LP one: raw, rewritten by publish_inline_artifacts
 * (only the `dca-cards/` directory survives), or dropped by a projection.
 */
const DCA_CARD_MARKER = new RegExp(
  [
    /publish_artifact\s+path=\S+\s+mime=application\/vnd\.agentos\.dca\+json/.source,
    /\[inline artifact published and already rendered for the user:\s*(?:\S*\/)?dca-cards\/[^\s\]]+/
      .source,
    /\[generated artifact omitted:[^\]\n]*application\/vnd\.agentos\.dca\+json/.source,
  ].join('|'),
  'i',
)

/**
 * A DCA call recognised by its result alone (the command was wrapped past
 * recognition): the result announces a DCA card. `mandates` reads as the list.
 */
export function dcaCallFromResult(text: string): TradeCall | null {
  if (!DCA_CARD_MARKER.test(text)) return null
  const list = /"kind"\s*:\s*"mandates"/.test(text) || /dca-cards\/mandates-/.test(text)
  const kind: TradeKind = list ? 'dca_list' : 'dca'
  return { kind, title: TITLES[kind], detail: '', command: '' }
}

/**
 * The trigger card announcement `agentos trade trigger … --json` prints last,
 * in the same three shapes as the DCA one (docs/triggers.md, "CLI").
 */
const TRIGGER_CARD_MARKER = new RegExp(
  [
    /publish_artifact\s+path=\S+\s+mime=application\/vnd\.agentos\.trigger\+json/.source,
    /\[inline artifact published and already rendered for the user:\s*(?:\S*\/)?trigger-cards\/[^\s\]]+/
      .source,
    /\[generated artifact omitted:[^\]\n]*application\/vnd\.agentos\.trigger\+json/.source,
  ].join('|'),
  'i',
)

/**
 * A trigger call recognised by its result alone (the command was wrapped
 * past recognition): the result announces a trigger card. `triggers` reads
 * as the list.
 */
export function triggerCallFromResult(text: string): TradeCall | null {
  if (!TRIGGER_CARD_MARKER.test(text)) return null
  // A bracket rides the trigger mime (docs/brackets.md): its envelope kind or
  // its card file (`bracket-…` / `brackets-…`) says which.
  const brackets = /"kind"\s*:\s*"brackets"/.test(text) || /trigger-cards\/brackets-/.test(text)
  const bracket = /"kind"\s*:\s*"bracket"/.test(text) || /trigger-cards\/bracket-/.test(text)
  const list = /"kind"\s*:\s*"triggers"/.test(text) || /trigger-cards\/triggers-/.test(text)
  const kind: TradeKind = brackets
    ? 'bracket_list'
    : bracket
      ? 'bracket'
      : list
        ? 'trigger_list'
        : 'trigger'
  return { kind, title: TITLES[kind], detail: '', command: '' }
}

/** Any card-announcing trade call recognised from its result: an LP read, a DCA or a trigger. */
export function cardCallFromResult(text: string): TradeCall | null {
  return lpCallFromResult(text) ?? dcaCallFromResult(text) ?? triggerCallFromResult(text)
}

const TRIGGER_STATUS_WORDS: Record<string, string> = {
  awaiting_approval: 'awaiting approval',
  armed: 'armed',
  triggered: 'triggered',
  paused: 'paused',
  done: 'done',
  stopped: 'stopped',
  rejected: 'rejected',
  expired: 'expired',
}

/** "$3,790" from a thousand up, cents under. */
function triggerUsd(value: number | null): string {
  if (value === null) return ''
  return Math.abs(value) >= 1000
    ? `$${Math.round(value).toLocaleString('en-US')}`
    : formatUsd(value)
}

/** One trigger as a row: "ETH · under $3,800" as the subject, state and price as the summary. */
function triggerLine(tr: Dict): { detail: string; bits: string[] } {
  const token = sym(tr.token)
  const condition = isDict(tr.condition) ? tr.condition : null
  const market = isDict(tr.market) ? tr.market : null
  const label = condition ? str(condition.label) : null
  const bits: string[] = []
  const status = str(tr.status)
  if (status) bits.push(TRIGGER_STATUS_WORDS[status] ?? status)
  const action = isDict(tr.action) ? str(tr.action.label) : null
  if (action) bits.push(action)
  const price = market ? num(market.priceUsd) : null
  if (price !== null) {
    const dist = market ? num(market.distancePct) : null
    const d =
      dist === null || status !== 'armed'
        ? ''
        : dist === 0
          ? ' · at the line'
          : ` · ${dist < 0 ? '−' : '+'}${Math.abs(dist).toFixed(1)} %`
    bits.push(`${token} ${triggerUsd(price)}${d}`)
  }
  return { detail: [token, label].filter(Boolean).join(' · '), bits }
}

/**
 * A trigger read-out or write in one line, never its JSON. A trigger payload
 * with 20 fires runs past the ~2,000 characters a stored tool result keeps,
 * so a result that no longer parses is read by hand from the fields that
 * come first (`kind`, `id`, `name`, `status`).
 */
function parseTriggerResult(call: TradeCall, text: string, data: unknown): TradeOutcome {
  if (isDict(data) && isDict(data.error)) {
    const message = str(data.error.message) ?? 'error'
    return { ...EMPTY, summary: message, error: message }
  }
  const d = isDict(data) ? data : null
  const kind = (d && str(d.kind)) ?? field(text, /"kind"\s*:\s*"(triggers?)"/)
  if (!kind) {
    const code = exitCodeOf(text)
    const first =
      text
        .split('\n')
        .find((l) => l.trim() && !/^\s*exit_code=/.test(l))
        ?.slice(0, 140) ?? ''
    if (/^\s*[{[]/.test(first)) {
      const message = jsonString(text, 'message')
      if (message) return { ...EMPTY, summary: message, error: message }
    }
    if (code !== null && code !== 0) {
      const line = first || `exit ${code}`
      return { ...EMPTY, summary: line, error: line }
    }
    return { ...EMPTY, summary: PROJECTION_MARKER.test(first) ? call.detail : '' }
  }
  if (kind === 'triggers') {
    const rows = d && Array.isArray(d.triggers) ? d.triggers.filter(isDict) : null
    const totals = d && isDict(d.totals) ? d.totals : null
    const count = totals ? num(totals.count) : rows ? rows.length : null
    const armed = totals
      ? num(totals.armed)
      : rows
        ? rows.filter((tr) => tr.status === 'armed').length
        : null
    const pending = totals
      ? (num(totals.awaiting) ?? 0)
      : rows
        ? rows.filter((tr) => tr.status === 'awaiting_approval').length
        : 0
    const fired = totals
      ? (num(totals.triggered) ?? 0)
      : rows
        ? rows.filter((tr) => tr.status === 'triggered').length
        : 0
    const bits: string[] = []
    if (count !== null) bits.push(count === 0 ? 'none yet' : `${armed ?? 0} armed`)
    if (pending) bits.push(`${pending} awaiting approval`)
    if (fired) bits.push(`${fired} triggered`)
    return {
      ...EMPTY,
      detail: count !== null ? `${count} trigger${count === 1 ? '' : 's'}` : '',
      summary: bits.join(' · '),
      awaiting: pending > 0,
    }
  }
  const tr = d && isDict(d.trigger) ? d.trigger : null
  if (!tr) {
    // Truncated: the trigger's first fields survive, the rest is gone.
    const id = field(text, /"id"\s*:\s*"(trg_[0-9a-zA-Z]+)"/)
    const name = field(text, /"trigger"\s*:\s*\{[^{}]*?"name"\s*:\s*"([^"]+)"/)
    const status = field(text, /"trigger"\s*:\s*\{[^{}]*?"status"\s*:\s*"([a-z_]+)"/)
    const bits = [status ? (TRIGGER_STATUS_WORDS[status] ?? status) : '', id ?? '']
    return {
      ...EMPTY,
      ...(name ? { detail: name } : {}),
      summary: bits.filter(Boolean).join(' · ') || 'result truncated',
      awaiting: status === 'awaiting_approval',
      triggerId: id ?? namedTrigger(call),
      triggerStatus: status,
    }
  }
  const line = triggerLine(tr)
  const fire = d && isDict(d.fire) ? d.fire : null
  const bits = [...line.bits]
  let orderId: string | null = null
  let txHash: string | null = null
  let explorerUrl: string | null = null
  let awaiting = tr.status === 'awaiting_approval'
  if (fire) {
    const status = str(fire.status)
    const n = num(fire.n)
    const word = status === 'parked' ? 'awaiting approval' : (status ?? '')
    bits.unshift(
      [n !== null ? `fire #${n}` : 'fire', word, str(fire.reason) ?? ''].filter(Boolean).join(' '),
    )
    orderId = str(fire.orderId)
    txHash = str(fire.txHash)
    explorerUrl = str(fire.explorerUrl)
    if (status === 'parked') awaiting = true
  }
  const chain = isDict(tr.chain) ? num(tr.chain.id) : null
  return {
    ...EMPTY,
    detail: line.detail || str(tr.name) || '',
    summary: bits.filter(Boolean).join(' · '),
    orderId,
    txHash,
    explorerUrl,
    chainId: chain,
    awaiting,
    confirmed: Boolean(fire && fire.status === 'filled' && txHash),
    error:
      fire && (fire.status === 'failed' || fire.status === 'skipped')
        ? (str(fire.reason) ?? str(fire.status) ?? 'failed')
        : null,
    triggerId: str(tr.id) ?? namedTrigger(call),
    triggerStatus: str(tr.status),
  }
}

/** "+20.3 % / −9.8 %": the moves left to the two lines of a bracket; '' when unknown. */
function bracketMoves(market: Dict | null): string {
  if (!market) return ''
  const up = num(market.upsidePct)
  const down = num(market.downsidePct)
  if (up === null && down === null) return ''
  const pct = (v: number | null, sign: '+' | '−') =>
    v === null ? '—' : v === 0 ? '0 %' : `${sign}${Number(Math.abs(v).toFixed(1))} %`
  return `${pct(up, '+')} / ${pct(down, '−')}`
}

/** One bracket as a row: "ETH · $3,420 – $4,560" as the subject, state and price as the summary. */
function bracketLine(b: Dict): { detail: string; bits: string[] } {
  const token = sym(b.token)
  const lines = isDict(b.lines) ? b.lines : null
  const market = isDict(b.market) ? b.market : null
  const tp = lines ? num(lines.takeProfitUsd) : null
  const sl = lines ? num(lines.stopLossUsd) : null
  const range =
    tp !== null || sl !== null ? `${triggerUsd(sl) || '—'} – ${triggerUsd(tp) || '—'}` : ''
  const bits: string[] = []
  const status = str(b.status)
  if (status) bits.push(TRIGGER_STATUS_WORDS[status] ?? status)
  const action = isDict(b.action) ? str(b.action.label) : null
  if (action) bits.push(action)
  const price = market ? num(market.priceUsd) : null
  if (price !== null) {
    const moves = status === 'armed' ? bracketMoves(market) : ''
    bits.push(`${token} ${triggerUsd(price)}${moves ? ` · ${moves}` : ''}`)
  }
  return { detail: [token, range].filter(Boolean).join(' · '), bits }
}

/**
 * A bracket read-out or write in one line, never its JSON. A bracket payload
 * carries both legs in full, far past what a stored tool result keeps, so a
 * cut result is read by hand from the fields that come first.
 */
function parseBracketResult(call: TradeCall, text: string, data: unknown): TradeOutcome {
  if (isDict(data) && isDict(data.error)) {
    const message = str(data.error.message) ?? 'error'
    return { ...EMPTY, summary: message, error: message }
  }
  const d = isDict(data) ? data : null
  const kind = (d && str(d.kind)) ?? field(text, /"kind"\s*:\s*"(brackets?)"/)
  if (kind !== 'bracket' && kind !== 'brackets') {
    // Not a bracket document: an error line, a projection, or nothing to say.
    return parseTriggerResult(call, text, data)
  }
  if (kind === 'brackets') {
    const rows = d && Array.isArray(d.brackets) ? d.brackets.filter(isDict) : null
    const totals = d && isDict(d.totals) ? d.totals : null
    const count = totals ? num(totals.count) : rows ? rows.length : null
    const of = (key: string, status: string) =>
      totals ? (num(totals[key]) ?? 0) : rows ? rows.filter((b) => b.status === status).length : 0
    const armed = of('armed', 'armed')
    const pending = of('awaiting', 'awaiting_approval')
    const fired = of('triggered', 'triggered')
    const bits: string[] = []
    if (count !== null) bits.push(count === 0 ? 'none yet' : `${armed} armed`)
    if (pending) bits.push(`${pending} awaiting approval`)
    if (fired) bits.push(`${fired} triggered`)
    return {
      ...EMPTY,
      detail: count !== null ? `${count} bracket${count === 1 ? '' : 's'}` : '',
      summary: bits.join(' · '),
      awaiting: pending > 0,
    }
  }
  const b = d && isDict(d.bracket) ? d.bracket : null
  if (!b) {
    const id = field(text, /"id"\s*:\s*"(brk_[0-9a-zA-Z]+)"/)
    const name = field(text, /"bracket"\s*:\s*\{[^{}]*?"name"\s*:\s*"([^"]+)"/)
    const status = field(text, /"bracket"\s*:\s*\{[^{}]*?"status"\s*:\s*"([a-z_]+)"/)
    const bits = [status ? (TRIGGER_STATUS_WORDS[status] ?? status) : '', id ?? '']
    return {
      ...EMPTY,
      ...(name ? { detail: name } : {}),
      summary: bits.filter(Boolean).join(' · ') || 'result truncated',
      awaiting: status === 'awaiting_approval',
      triggerId: id ?? namedBracket(call),
      triggerStatus: status,
    }
  }
  const line = bracketLine(b)
  const fire = d && isDict(d.fire) ? d.fire : null
  const bits = [...line.bits]
  let awaiting = b.status === 'awaiting_approval'
  let orderId: string | null = null
  let txHash: string | null = null
  let explorerUrl: string | null = null
  if (fire) {
    const status = str(fire.status)
    const n = num(fire.n)
    const word = status === 'parked' ? 'awaiting approval' : (status ?? '')
    bits.unshift(
      [n !== null ? `fire #${n}` : 'fire', word, str(fire.reason) ?? ''].filter(Boolean).join(' '),
    )
    orderId = str(fire.orderId)
    txHash = str(fire.txHash)
    explorerUrl = str(fire.explorerUrl)
    if (status === 'parked') awaiting = true
  }
  const chain = isDict(b.chain) ? num(b.chain.id) : null
  return {
    ...EMPTY,
    detail: line.detail || str(b.name) || '',
    summary: bits.filter(Boolean).join(' · '),
    orderId,
    txHash,
    explorerUrl,
    chainId: chain,
    awaiting,
    confirmed: Boolean(fire && fire.status === 'filled' && txHash),
    error:
      fire && (fire.status === 'failed' || fire.status === 'skipped')
        ? (str(fire.reason) ?? str(fire.status) ?? 'failed')
        : null,
    triggerId: str(b.id) ?? namedBracket(call),
    triggerStatus: str(b.status),
  }
}

/** The bracket id a `trade bracket <sub> <id>` command names, when it names one. */
function namedBracket(call: TradeCall): string | null {
  return /^brk_[0-9a-zA-Z]+$/.test(call.detail) ? call.detail : null
}

/** The trigger id a `trade trigger <sub> <id>` command names, when it names one. */
function namedTrigger(call: TradeCall): string | null {
  return /^trg_[0-9a-zA-Z]+$/.test(call.detail) ? call.detail : null
}

/**
 * A trigger row read against the live trigger list: once the list knows the
 * trigger, the row's pill and status word follow it instead of the one the
 * result recorded ("awaiting approval" on a Stop-loss row the user has since
 * armed). A row that fronts a fire's order keeps the order's pill.
 */
export function withLiveTrigger(
  outcome: TradeOutcome,
  live: string | null | undefined,
): TradeOutcome {
  if (!live || !outcome.triggerId || outcome.orderId || outcome.error) return outcome
  const recorded = outcome.triggerStatus ? TRIGGER_STATUS_WORDS[outcome.triggerStatus] : null
  const now = TRIGGER_STATUS_WORDS[live] ?? live
  let summary = outcome.summary
  if (recorded && recorded !== now) {
    const bits = summary.split(' · ')
    const at = bits.indexOf(recorded)
    if (at >= 0) {
      bits[at] = now
      summary = bits.join(' · ')
    }
  }
  return { ...outcome, summary, awaiting: live === 'awaiting_approval', triggerLive: live }
}

const DCA_STATUS_WORDS: Record<string, string> = {
  awaiting_approval: 'awaiting approval',
  active: 'active',
  paused: 'paused',
  completed: 'done',
  stopped: 'stopped',
  rejected: 'rejected',
  expired: 'expired',
}

/** "$120" for whole dollars, "$120.40" otherwise. */
function dcaUsd(value: number | null): string {
  if (value === null) return ''
  return Math.abs(value - Math.round(value)) < 0.005
    ? `$${Math.round(value).toLocaleString('en-US')}`
    : formatUsd(value)
}

/** One mandate as a row: "ETH ← USDC · $10 / day" as the subject, state and money as the summary. */
function dcaMandateLine(m: Dict): { detail: string; bits: string[] } {
  const token = sym(m.token)
  const quote = sym(m.quote)
  const schedule = isDict(m.schedule) ? m.schedule : null
  const budget = isDict(m.budget) ? m.budget : null
  const every = schedule ? num(schedule.everySeconds) : null
  const perRun = budget ? num(budget.usdPerRun) : null
  const pair = token && quote ? `${token} ← ${quote}` : token
  const size =
    perRun !== null ? `${dcaUsd(perRun)}${every !== null ? ` / ${dcaEvery(`${every}`)}` : ''}` : ''
  const bits: string[] = []
  const status = str(m.status)
  if (status) bits.push(DCA_STATUS_WORDS[status] ?? status)
  if (budget && num(budget.spentUsd) !== null && num(budget.capUsd) !== null)
    bits.push(`${dcaUsd(num(budget.spentUsd))} of ${dcaUsd(num(budget.capUsd))}`)
  const runs = isDict(m.runs) ? m.runs : null
  if (runs && num(runs.done) !== null) {
    const max = num(runs.max)
    bits.push(max !== null ? `${num(runs.done)}/${max} buys` : `${num(runs.done)} buys`)
  }
  return { detail: [pair, size].filter(Boolean).join(' · '), bits }
}

/**
 * A DCA read-out or write in one line, never its JSON. The mandate payload
 * runs to several KB (up to 50 runs of history), past the ~2,000 characters a
 * stored tool result keeps, so a result that no longer parses is read by hand
 * from the fields that come first (`kind`, `id`, `name`, `status`).
 */
function parseDcaResult(call: TradeCall, text: string, data: unknown): TradeOutcome {
  if (isDict(data) && isDict(data.error)) {
    const message = str(data.error.message) ?? 'error'
    return { ...EMPTY, summary: message, error: message }
  }
  const d = isDict(data) ? data : null
  const kind = (d && str(d.kind)) ?? field(text, /"kind"\s*:\s*"(mandates?)"/)
  if (!kind) {
    const code = exitCodeOf(text)
    const first =
      text
        .split('\n')
        .find((l) => l.trim() && !/^\s*exit_code=/.test(l))
        ?.slice(0, 140) ?? ''
    if (/^\s*[{[]/.test(first)) {
      const message = jsonString(text, 'message')
      if (message) return { ...EMPTY, summary: message, error: message }
    }
    if (code !== null && code !== 0) {
      const line = first || `exit ${code}`
      return { ...EMPTY, summary: line, error: line }
    }
    return { ...EMPTY, summary: PROJECTION_MARKER.test(first) ? call.detail : '' }
  }
  if (kind === 'mandates') {
    const rows = d && Array.isArray(d.mandates) ? d.mandates.filter(isDict) : null
    const totals = d && isDict(d.totals) ? d.totals : null
    const count = totals ? num(totals.count) : rows ? rows.length : null
    const active = totals
      ? num(totals.active)
      : rows
        ? rows.filter((m) => m.status === 'active').length
        : null
    const pending = rows ? rows.filter((m) => m.status === 'awaiting_approval').length : 0
    const bits: string[] = []
    if (count !== null) bits.push(count === 0 ? 'none yet' : `${active ?? 0} active`)
    if (pending) bits.push(`${pending} awaiting approval`)
    if (totals && num(totals.spentUsd) !== null && num(totals.capUsd) !== null && count)
      bits.push(`${dcaUsd(num(totals.spentUsd))} of ${dcaUsd(num(totals.capUsd))}`)
    return {
      ...EMPTY,
      detail: count !== null ? `${count} mandate${count === 1 ? '' : 's'}` : '',
      summary: bits.join(' · '),
      awaiting: pending > 0,
    }
  }
  const m = d && isDict(d.mandate) ? d.mandate : null
  if (!m) {
    // Truncated: the mandate's first fields survive, the rest is gone.
    const id = field(text, /"id"\s*:\s*"(dca_[0-9a-zA-Z]+)"/)
    const name = field(text, /"mandate"\s*:\s*\{[^{}]*?"name"\s*:\s*"([^"]+)"/)
    const status = field(text, /"mandate"\s*:\s*\{[^{}]*?"status"\s*:\s*"([a-z_]+)"/)
    const bits = [status ? (DCA_STATUS_WORDS[status] ?? status) : '', id ?? '']
    return {
      ...EMPTY,
      ...(name ? { detail: name } : {}),
      summary: bits.filter(Boolean).join(' · ') || 'result truncated',
      awaiting: status === 'awaiting_approval',
      mandateId: id ?? namedMandate(call),
      mandateStatus: status,
    }
  }
  const line = dcaMandateLine(m)
  const run = d && isDict(d.run) ? d.run : null
  const bits = [...line.bits]
  let orderId: string | null = null
  let txHash: string | null = null
  let explorerUrl: string | null = null
  let awaiting = m.status === 'awaiting_approval'
  if (run) {
    const status = str(run.status)
    const n = num(run.n)
    const word = status === 'parked' ? 'awaiting approval' : (status ?? '')
    bits.unshift(
      [n !== null ? `buy #${n}` : 'buy', word, str(run.reason) ?? ''].filter(Boolean).join(' '),
    )
    orderId = str(run.orderId)
    txHash = str(run.txHash)
    explorerUrl = str(run.explorerUrl)
    if (status === 'parked') awaiting = true
  }
  const chain = isDict(m.chain) ? num(m.chain.id) : null
  return {
    ...EMPTY,
    detail: line.detail || str(m.name) || '',
    summary: bits.filter(Boolean).join(' · '),
    orderId,
    txHash,
    explorerUrl,
    chainId: chain,
    awaiting,
    confirmed: Boolean(run && run.status === 'filled' && txHash),
    error: run && run.status === 'failed' ? (str(run.reason) ?? 'failed') : null,
    mandateId: str(m.id) ?? namedMandate(call),
    mandateStatus: str(m.status),
  }
}

/** The mandate id a `trade dca <sub> <id>` command names, when it names one. */
function namedMandate(call: TradeCall): string | null {
  return /^dca_[0-9a-zA-Z]+$/.test(call.detail) ? call.detail : null
}

/**
 * A DCA row read against the live mandate list. The result recorded the
 * mandate as it was when the command ran ("awaiting approval" on a Start
 * DCA row); once the list knows it, the row's pill and status word follow
 * the mandate instead. A row that fronts one buy's order (a parked or filled
 * run) keeps the order's pill, and an unknown mandate stays as recorded.
 */
export function withLiveMandate(
  outcome: TradeOutcome,
  live: string | null | undefined,
): TradeOutcome {
  if (!live || !outcome.mandateId || outcome.orderId || outcome.error) return outcome
  const recorded = outcome.mandateStatus ? DCA_STATUS_WORDS[outcome.mandateStatus] : null
  const now = DCA_STATUS_WORDS[live] ?? live
  let summary = outcome.summary
  if (recorded && recorded !== now) {
    const bits = summary.split(' · ')
    const at = bits.indexOf(recorded)
    if (at >= 0) {
      bits[at] = now
      summary = bits.join(' · ')
    }
  }
  return { ...outcome, summary, awaiting: live === 'awaiting_approval', mandateLive: live }
}

const LP_STATUS_WORDS: Record<string, string> = {
  'in-range': 'in range',
  'above-range': 'above range',
  'below-range': 'below range',
  closed: 'closed',
}

/** A string field out of JSON too truncated to parse. */
function field(text: string, pattern: RegExp): string | null {
  const m = pattern.exec(text)
  return m ? m[1]! : null
}

const LP_CHAIN_NAMES: Record<string, string> = {
  base: 'Base',
  '8453': 'Base',
  robinhood: 'Robinhood Chain',
  '4663': 'Robinhood Chain',
}

/** Chain names from keys, deduplicated, in the order given; unknown keys are dropped. */
function lpChainNames(keys: string[]): string[] {
  const names = keys.map((k) => LP_CHAIN_NAMES[k.trim().toLowerCase()]).filter(Boolean)
  return [...new Set(names)] as string[]
}

/**
 * A chain list for the call chip: one chain keeps its full name, several use
 * the short form ("Base + Robinhood") so the chip stays readable in a narrow
 * ledger before CSS has to ellipsize it.
 */
function chainList(names: string[]): string {
  if (names.length <= 1) return names[0] ?? ''
  return names.map((n) => n.replace(/\s+chain$/i, '')).join(' + ')
}

/**
 * The chains a `positions` read scanned: the envelope's `chains` when the
 * JSON parses, the `asOfBlocks` keys when only a prefix survived, else the
 * command's `--chain` flags (none means both). An unknown command yields ''.
 */
function positionsChains(d: Record<string, unknown> | null, text: string, command: string): string {
  if (d && Array.isArray(d.chains)) {
    const named = d.chains
      .map((c) => (isDict(c) ? (str(c.name) ?? lpChainNames([str(c.key) ?? ''])[0]) : ''))
      .filter((n): n is string => Boolean(n))
    if (named.length) return chainList(named)
  }
  const blocks = d && isDict(d.asOfBlocks) ? Object.keys(d.asOfBlocks) : null
  const blockKeys =
    blocks ??
    [...(field(text, /"asOfBlocks"\s*:\s*\{([^{}]*)\}/) ?? '').matchAll(/"([^"]+)"\s*:/g)].map(
      (m) => m[1]!,
    )
  if (blockKeys.length) return chainList(lpChainNames(blockKeys))
  if (!command) return ''
  const flagged = flags(command, 'chain')
  return chainList(lpChainNames(flagged.length ? flagged : ['base', 'robinhood']))
}

/**
 * An LP read-out in one line — "pool boar (Base)" as the subject, the figures
 * that matter as the summary — and never the JSON itself: the card below the
 * call is the read-out; the raw body stays behind the "raw" toggle.
 */
function parseLpResult(text: string, data: unknown, command: string): TradeOutcome {
  if (isDict(data) && isDict(data.error)) {
    const message = str(data.error.message) ?? 'error'
    return { ...EMPTY, summary: message, error: message }
  }
  const d = isDict(data) ? data : null
  const kind = (d && str(d.kind)) ?? field(text, /"kind"\s*:\s*"(pool|ranges|position|positions)"/)
  if (!kind) {
    const code = exitCodeOf(text)
    if (code !== null && code !== 0) {
      const first =
        text
          .split('\n')
          .find((l) => l.trim() && !/^\s*exit_code=/.test(l))
          ?.slice(0, 140) ?? `exit ${code}`
      return { ...EMPTY, summary: first, error: first }
    }
    return { ...EMPTY, summary: '' }
  }

  const chainName = (c: unknown): string => (isDict(c) ? (str(c.name) ?? str(c.key) ?? '') : '')
  let chains = ''
  if (kind === 'positions') {
    // A wallet scan spans chains and each row carries its own: the label comes
    // from the envelope or the command line, never from whichever row is first.
    chains = positionsChains(d, text, command)
  } else {
    chains = d ? chainName(d.chain) : ''
    if (!chains && d && isDict(d.position)) chains = chainName(d.position.chain)
    if (!chains) chains = field(text, /"chain"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"/) ?? ''
  }
  const where = chains ? ` (${chains})` : ''

  let subject = ''
  const bits: string[] = []
  if (kind === 'pool' || kind === 'ranges') {
    const symbol =
      (d && isDict(d.token) ? str(d.token.symbol) : null) ??
      field(text, /"token"\s*:\s*\{[^}]*?"symbol"\s*:\s*"([^"]+)"/) ??
      ''
    subject = `${kind} ${symbol}`.trim()
    const pool = d && isDict(d.pool) ? d.pool : null
    if (kind === 'pool') {
      if (pool && num(pool.tvlUsd) !== null)
        bits.push(`TVL ${formatUsd(num(pool.tvlUsd), { compact: true })}`)
      if (pool && num(pool.mcapUsd) !== null)
        bits.push(`mcap ${formatUsd(num(pool.mcapUsd), { compact: true })}`)
    } else if (d && Array.isArray(d.segments)) {
      bits.push(`${d.segments.length} range${d.segments.length === 1 ? '' : 's'}`)
    }
  } else if (kind === 'position') {
    const position = d && isDict(d.position) ? d.position : null
    const id = (position && str(position.tokenId)) ?? field(text, /"tokenId"\s*:\s*"([^"]+)"/)
    subject = `position${id ? ` #${id}` : ''}`
    if (position) {
      const status = str(position.status)
      if (status && LP_STATUS_WORDS[status]) bits.push(LP_STATUS_WORDS[status]!)
      if (num(position.valueUsd) !== null)
        bits.push(formatUsd(num(position.valueUsd), { compact: true }))
    }
  } else {
    const wallets = d && Array.isArray(d.wallets) ? d.wallets.length : null
    subject = `positions${wallets !== null ? ` · ${wallets} wallet${wallets === 1 ? '' : 's'}` : ''}`
    const totals = d && isDict(d.totals) ? d.totals : null
    const count = totals ? num(totals.count) : null
    if (count !== null) bits.push(count === 0 ? 'none found' : `${count} open`)
    const out = totals ? num(totals.outOfRange) : null
    if (out) bits.push(`${out} out of range`)
    if (totals && num(totals.valueUsd) !== null)
      bits.push(formatUsd(num(totals.valueUsd), { compact: true }))
  }
  if (d && d.partialScan === true) bits.push('partial scan')
  return { ...EMPTY, detail: `${subject}${where}`, summary: bits.join(' · ') }
}

/**
 * The string value of the first `key` out of text too truncated to parse; null
 * when that first one is null (a nested namesake further on is not it).
 */
function jsonString(text: string, key: string): string | null {
  const m = new RegExp(`"${key}"\\s*:\\s*(?:"((?:[^"\\\\]|\\\\.)*)"|null)`).exec(text)
  return m?.[1] !== undefined ? m[1].replace(/\\(.)/g, '$1') : null
}

/** The `{base, quote}` Amount pair under `key` ("received", "spent"), as far as it survived. */
function truncatedPair(text: string, key: string, stop: string | null): Dict | null {
  const at = text.search(new RegExp(`"${key}"\\s*:\\s*\\{`))
  if (at < 0) return null
  let body = text.slice(at)
  const end = stop ? body.search(new RegExp(`"${stop}"\\s*:`)) : -1
  if (end > 0) body = body.slice(0, end)
  const side = (which: string): Dict | null => {
    const w = body.search(new RegExp(`"${which}"\\s*:\\s*\\{`))
    if (w < 0) return null
    const human = /^[^}]*?"human"\s*:\s*"([^"]*)"/.exec(body.slice(w))
    return human ? { human: human[1]! } : null
  }
  const base = side('base')
  const quote = side('quote')
  return base || quote ? { base, quote } : null
}

/**
 * An LP write whose order JSON did not survive: the stored tool result stops
 * at ~2,000 characters and the order runs to 3–4 KB, so `JSON.parse` fails.
 * The fields that make the row are read by hand — they come early in the
 * order (`orderId`, `kind`, `status`, `reason`, `txHash`), the receipt
 * amounts only when the cut fell after them — and the row says the same
 * thing the parsed path would. Never the JSON itself: when nothing can be
 * read the row says the result was truncated. Null when the text is no JSON
 * at all (a plain error line keeps the ordinary first-line path).
 */
function lpWriteFromTruncated(call: TradeCall, text: string): TradeOutcome | null {
  const body = text.replace(/^\s*exit_code=-?\d+\s*/, '')
  const start = body.search(/\S/)
  if (start < 0 || !/[{[]/.test(body[start]!)) return null
  const error = jsonString(body, 'message')
  if (/^\s*[{[]\s*"error"\s*:/.test(body) && error) return { ...EMPTY, summary: error, error }
  const orderId = jsonString(body, 'orderId')
  const status = jsonString(body, 'status') as OrderStatus | null
  if (!orderId && !status) return { ...EMPTY, summary: 'result truncated' }
  const kind = jsonString(body, 'kind') ?? call.kind
  const reason = jsonString(body, 'reason')
  const txHash = field(body, /"txHash"\s*:\s*"(0x[0-9a-fA-F]{64})"/)
  const explorerUrl = jsonString(body, 'explorerUrl')
  const chainId = Number(field(body, /"chainId"\s*:\s*(\d+)/)) || null
  const symbolOf = (key: string): string =>
    field(body, new RegExp(`"${key}"\\s*:\\s*\\{[^}]*?"symbol"\\s*:\\s*"([^"]+)"`)) ?? ''
  const base = symbolOf('tokenIn')
  const quote = symbolOf('tokenOut')
  const moved =
    status === 'confirmed'
      ? kind === 'lp_add'
        ? truncatedPair(body, 'spent', null)
        : truncatedPair(body, 'received', 'spent')
      : null
  const line = moved ? lpAmountsLine(moved, base, quote) : ''
  const legs = line ? (kind === 'lp_add' ? `deposit ${line}` : `receive ${line}`) : ''
  const bits = [legs]
  if (status) bits.push(statusWordFor(status))
  // The parsed path names the amounts while an order waits; the truncated
  // one lost them, so the order id is what the row can still point at.
  if (status === 'awaiting_approval' && !legs && orderId) bits.push(`#${orderId}`)
  if (reason && (status === 'rejected' || status === 'failed' || status === 'expired'))
    bits.push(reason)
  return {
    ...EMPTY,
    summary: bits.filter(Boolean).join(' · ') || 'result truncated',
    status,
    orderId,
    txHash,
    explorerUrl,
    chainId,
    awaiting: status === 'awaiting_approval',
    confirmed: status === 'confirmed' && Boolean(txHash),
    error: status === 'failed' ? (reason ?? 'failed') : null,
  }
}

const HTTP_URL = /^https?:\/\//i
const TX_HASH = /^0x[0-9a-fA-F]{64}$/

/**
 * Where a ledger row's tx hash links to: the order's own `explorerUrl`, else
 * the chain's explorer (`explorerFor(chainId)`) + `/tx/<hash>`. Null when
 * neither is known (a truncated result on an unknown chain) — or when either
 * is not a web link, which the shell would refuse to open anyway.
 */
export function txExplorerUrl(
  outcome: Pick<TradeOutcome, 'txHash' | 'explorerUrl' | 'chainId'>,
  explorerFor: (chainId: number) => string | null | undefined,
): string | null {
  const hash = outcome.txHash
  if (!hash) return null
  if (outcome.explorerUrl && HTTP_URL.test(outcome.explorerUrl)) return outcome.explorerUrl
  if (!outcome.chainId || !TX_HASH.test(hash)) return null
  const base = explorerFor(outcome.chainId)
  if (!base || !HTTP_URL.test(base)) return null
  return `${base.replace(/\/+$/, '')}/tx/${hash}`
}

/** One line for a result. Unknown shapes fall back to the first line of text. */
export function parseTradeResult(call: TradeCall, text: string): TradeOutcome {
  const data = parseJson(text)
  if (call.kind === 'lp') return parseLpResult(text, data, call.command)
  if (isDcaKind(call.kind)) {
    // The card line after the JSON can end in "]" (the live rewrite), which
    // the plain parse swallows into the document; drop it and try again.
    const bare = isDict(data)
      ? data
      : parseJson(
          text
            .split('\n')
            .filter((l) => !DCA_CARD_MARKER.test(l))
            .join('\n'),
        )
    return parseDcaResult(call, text, bare)
  }
  if (isTriggerKind(call.kind) || isBracketKind(call.kind)) {
    const bare = isDict(data)
      ? data
      : parseJson(
          text
            .split('\n')
            .filter((l) => !TRIGGER_CARD_MARKER.test(l))
            .join('\n'),
        )
    return isBracketKind(call.kind)
      ? parseBracketResult(call, text, bare)
      : parseTriggerResult(call, text, bare)
  }
  if (!isDict(data)) {
    const code = exitCodeOf(text)
    const lines = text
      .trim()
      .split('\n')
      .filter((l) => !/^\s*exit_code=/.test(l))
    // The result body was projected away: describe the call from its
    // arguments instead of leaking the marker, and never style it as an error.
    const firstLine = lines.find((l) => l.trim()) ?? ''
    if (PROJECTION_MARKER.test(firstLine)) {
      return { ...EMPTY, summary: call.detail }
    }
    if (call.kind === 'lp_collect' || call.kind === 'lp_remove' || call.kind === 'lp_add') {
      const lp = lpWriteFromTruncated(call, text)
      if (lp) return lp
    }
    if (call.kind === 'status' && (code === null || code === 0)) {
      const status = statusFromTruncated(text)
      if (status) return status
    }
    const first = (lines[0] ?? '').slice(0, 140)
    if (code !== null && code !== 0)
      return { ...EMPTY, summary: first || `exit ${code}`, error: first || `exit ${code}` }
    return { ...EMPTY, summary: first }
  }
  if (isDict(data.error)) {
    const e = data.error
    return { ...EMPTY, summary: str(e.message) ?? 'error', error: str(e.message) ?? 'error' }
  }
  switch (call.kind) {
    case 'swap':
    case 'send': {
      const orders = Array.isArray(data.orders) ? data.orders.filter(isDict) : []
      if (orders.length === 1) return fromOrder(orders[0]!)
      if (orders.length > 1) {
        const awaiting = orders.some((o) => o.status === 'awaiting_approval')
        const confirmed = orders.every((o) => o.status === 'confirmed' && str(o.txHash))
        const counts = new Map<string, number>()
        for (const o of orders)
          counts.set(String(o.status), (counts.get(String(o.status)) ?? 0) + 1)
        const noun = call.kind === 'send' ? 'recipients' : 'wallets'
        const summary = `${orders.length} ${noun} · ${[...counts.entries()]
          .map(([s, n]) => `${n} ${statusWordFor(s as OrderStatus)}`)
          .join(', ')}`
        // One approval covers the batch: the stamp jumps to its first leg.
        const first = orders.find((o) => o.status === 'awaiting_approval') ?? orders[0]!
        return { ...EMPTY, summary, awaiting, confirmed, orderId: str(first.orderId) }
      }
      break
    }
    case 'order':
    case 'approve':
    case 'reject':
    case 'revoke': {
      if (isDict(data.order)) return fromOrder(data.order)
      break
    }
    case 'lp_collect':
    case 'lp_remove':
    case 'lp_add': {
      // The order JSON, as `trade swap`/`trade send` print it: bare, under
      // `order`, or as the one entry of `orders`.
      const orders = Array.isArray(data.orders) ? data.orders.filter(isDict) : []
      const o = isDict(data.order) ? data.order : orders.length ? orders[0]! : data
      if (str(o.orderId) || str(o.status)) return fromOrder(o)
      break
    }
    case 'bridge': {
      // `bridge create` prints `{"order": …}`; a bare order is read the same.
      const o = isDict(data.order) ? data.order : data
      if (str(o.orderId) || str(o.status)) return fromOrder(o)
      break
    }
    case 'bridge_status': {
      // `bridge status` prints the order and, beside it, Relay's view of the
      // fill: while the order is `submitted` that word is the whole news.
      const o = isDict(data.order) ? data.order : null
      if (!o) break
      const base = fromOrder(o)
      const fill =
        (isDict(data.bridge) ? str(data.bridge.fillStatus) : null) ??
        (isDict(o.bridge) ? str(o.bridge.fillStatus) : null)
      return fill && base.status === 'submitted'
        ? { ...base, summary: `${base.summary} · fill ${fill}` }
        : base
    }
    case 'bridge_quote': {
      const q = isDict(data.quote) ? data.quote : data
      const out = str(q.expectedOut) ?? str(q.amountOut)
      const fees = num(q.feesUsd)
      const eta = num(q.timeEstimateSeconds)
      const bits = [
        out ? `≈ ${formatAmount(out)} ${sym(q.tokenOut)}`.trim() : '',
        fees !== null ? `fees ${formatUsd(fees)}` : '',
        eta !== null ? `~${eta}s` : '',
      ].filter(Boolean)
      if (bits.length) return { ...EMPTY, summary: bits.join(' · ') }
      break
    }
    case 'allowances': {
      const rows = Array.isArray(data.allowances) ? data.allowances.filter(isDict) : []
      const unlimited = num(data.unlimitedCount) ?? rows.filter((a) => a.unlimited).length
      const exposure = rows.reduce((s, a) => s + (num(a.exposureUsd) ?? 0), 0)
      const bits = [`${rows.length} live`]
      if (unlimited) bits.push(`${unlimited} unlimited`)
      if (exposure > 0) bits.push(`${formatUsd(exposure)} at stake`)
      return { ...EMPTY, summary: bits.join(' · '), error: null }
    }
    case 'decode': {
      const call_ = isDict(data.call) ? data.call : null
      const tx = isDict(data.tx) ? data.tx : null
      const bits: string[] = []
      const fn = call_ ? str(call_.function) : null
      bits.push(fn ? fn : call_ ? `${str(call_.selector) ?? '?'} (unknown)` : 'call')
      if (tx && str(tx.status)) bits.push(String(tx.status))
      const transfers = Array.isArray(data.transfers) ? data.transfers.length : 0
      if (transfers) bits.push(`${transfers} transfer${transfers === 1 ? '' : 's'}`)
      return {
        ...EMPTY,
        summary: bits.join(' · '),
        txHash: tx ? str(tx.hash) : null,
        explorerUrl: str(data.explorerUrl),
        chainId: num(data.chainId) ?? (tx ? num(tx.chainId) : null),
      }
    }
    case 'network': {
      const rows = Array.isArray(data.chains) ? data.chains.filter(isDict) : []
      const parts = rows.map((c) => {
        const name = str(c.name) ?? str(c.key) ?? ''
        const age = num(c.blockAgeS)
        const ok = c.healthy === true
        return `${name} ${ok ? '✓' : '✗'}${age !== null ? ` ${age}s` : ''}`
      })
      const down = rows.filter((c) => c.healthy !== true).length
      return {
        ...EMPTY,
        summary: parts.join(' · '),
        error: down ? `${down} chain${down === 1 ? '' : 's'} unhealthy` : null,
      }
    }
    case 'quote': {
      const legs = `${formatAmount(str(data.amountIn))} ${sym(data.tokenIn)} → ${formatAmount(
        str(data.amountOut),
      )} ${sym(data.tokenOut)}`
      const impact = num(data.priceImpactPct)
      const bits = [legs]
      if (impact !== null) bits.push(`impact ${impact.toFixed(2)}%`)
      const guard = isDict(data.guard) ? str(data.guard.decision) : null
      if (guard === 'needs_approval') bits.push('would need approval')
      if (guard === 'blocked_daily_cap') bits.push('over the daily cap')
      return {
        ...EMPTY,
        summary: bits.join(' · '),
        provider: (str(data.provider) as ProviderId | null) ?? null,
      }
    }
    case 'portfolio': {
      const totals = isDict(data.totals) ? data.totals : null
      const value = totals ? num(totals.valueUsd) : null
      const holdings = Array.isArray(data.holdings) ? data.holdings.length : null
      const bits: string[] = []
      if (value !== null) bits.push(formatUsd(value))
      if (holdings !== null) bits.push(`${holdings} holdings`)
      return { ...EMPTY, summary: bits.join(' · '), marketAt: num(data.updatedAt) }
    }
    case 'balances': {
      const rows = Array.isArray(data.balances) ? data.balances.filter(isDict) : []
      const total = rows.reduce((s, b) => s + (num(b.valueUsd) ?? 0), 0)
      const nonZero = rows.filter((b) => Number(b.amount) > 0).length
      return {
        ...EMPTY,
        summary: `${nonZero} balances · ${formatUsd(total)}`,
        marketAt: num(data.updatedAt),
      }
    }
    case 'orders': {
      const rows = Array.isArray(data.orders) ? data.orders.filter(isDict) : []
      const pending = rows.filter((o) => o.status === 'awaiting_approval').length
      return {
        ...EMPTY,
        summary: `${rows.length} orders${pending ? ` · ${pending} awaiting approval` : ''}`,
        awaiting: pending > 0,
      }
    }
    case 'history': {
      const rows = Array.isArray(data.entries) ? data.entries.length : 0
      return { ...EMPTY, summary: `${rows} entries` }
    }
    case 'tokens': {
      const rows = Array.isArray(data.tokens) ? data.tokens.filter(isDict) : []
      const names = rows
        .slice(0, 3)
        .map((t) => `${sym(t)}${t.verified ? '' : '?'}`)
        .join(', ')
      return { ...EMPTY, summary: `${rows.length} matches${names ? ` · ${names}` : ''}` }
    }
    case 'status': {
      const provider = str(data.provider)
      const bits: string[] = []
      if (provider) bits.push(`via ${providerLabel(provider)}`)
      if (data.unlocked === true) bits.push('vault unlocked')
      if (data.unlocked === false) bits.push('vault locked')
      return { ...EMPTY, summary: bits.join(' · '), provider: provider as ProviderId | null }
    }
    case 'limits': {
      const left =
        num(data.dailyCapUsd) !== null && num(data.spentTodayUsd) !== null
          ? formatUsd((num(data.dailyCapUsd) ?? 0) - (num(data.spentTodayUsd) ?? 0))
          : null
      return { ...EMPTY, summary: left ? `${left} left today` : '' }
    }
    case 'sync':
      return { ...EMPTY, summary: data.started ? 'sync started' : '' }
    case 'wallet': {
      if (isDict(data.wallet))
        return {
          ...EMPTY,
          summary: `${str(data.wallet.label) ?? ''} ${str(data.wallet.address) ?? ''}`.trim(),
        }
      if (Array.isArray(data.wallets))
        return { ...EMPTY, summary: `${data.wallets.length} wallets` }
      if (typeof data.unlocked === 'boolean')
        return { ...EMPTY, summary: data.unlocked ? 'vault unlocked' : 'vault locked' }
      break
    }
    default:
      break
  }
  const first = text.trim().split('\n')[0] ?? ''
  return { ...EMPTY, summary: first.slice(0, 140) }
}

/** Runs of this many consecutive trade rows fold into one group. */
export const LEDGER_GROUP_MIN = 3

/** Split a sequence of row flags into runs; used to fold ≥3 consecutive rows. */
export function ledgerRuns(isTrade: readonly boolean[]): { start: number; length: number }[] {
  const runs: { start: number; length: number }[] = []
  let start = -1
  isTrade.forEach((flagged, i) => {
    if (flagged && start < 0) start = i
    if ((!flagged || i === isTrade.length - 1) && start >= 0) {
      const end = flagged ? i + 1 : i
      const length = end - start
      if (length >= LEDGER_GROUP_MIN) runs.push({ start, length })
      start = -1
    }
  })
  return runs
}
