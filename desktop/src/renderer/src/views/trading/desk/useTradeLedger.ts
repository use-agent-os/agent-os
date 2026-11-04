import { useCallback, useEffect, useMemo, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { copyLpText } from '@/views/chat/transcript/lp'
import type { StreamEventPayload } from '@/views/chat/types'
import type { TranscriptEventSeams } from '@/views/chat/useTranscript'
import { t, type MessageKey } from '~/i18n'
import { TRADING_KEYS } from '~/stores/trading'
import { providerMark } from '../ProviderMark'
import {
  providerLabel,
  type BracketListPayload,
  type MandateListPayload,
  type TradingStatus,
  type TriggerListPayload,
} from '../types'
import {
  commandFromToolInput,
  LEDGER_GROUP_MIN,
  cardCallFromResult,
  isBracketKind,
  isDcaKind,
  isTriggerKind,
  parseTradeCommand,
  parseTradeResult,
  txExplorerUrl,
  withLiveMandate,
  withLiveTrigger,
  type TradeCall,
  type TradeOutcome,
} from './ledger'

/**
 * Trade ledger rows. The shared transcript renders every tool call as a
 * <details> block; for the desk's own commands we lay a one-line ledger row
 * in front of it and fold the raw block behind a "raw" toggle. Live calls
 * are read from the stream seams (full payloads); rows rebuilt from history
 * are read from the block itself (best effort, previews are truncated).
 * Runs of three or more consecutive rows fold into one group.
 */

const ROW_CLASS = 'trd-ledger'
const GROUP_CLASS = 'trd-lgroup'
const ROW_ATTR = 'data-trade-row'

interface LiveCall {
  /** Known once the arguments arrive: tool_use_start carries only the name. */
  call: TradeCall | null
  outcome: TradeOutcome | null
  /** Null when the start was never seen (a result replayed on reconnect). */
  startedAt: number | null
  finishedAt: number | null
}

function toolId(payload: StreamEventPayload | undefined): string {
  return String((payload as { tool_use_id?: string } | undefined)?.tool_use_id ?? '')
}

function toolName(payload: StreamEventPayload | undefined): string {
  const p = payload as { name?: string; tool_name?: string } | undefined
  return String(p?.name || p?.tool_name || '')
}

function toolInput(payload: StreamEventPayload | undefined): unknown {
  const p = payload as { input?: unknown; arguments?: unknown } | undefined
  return p?.input ?? p?.arguments
}

function toolResultText(payload: StreamEventPayload | undefined): string {
  const p = payload as { content?: unknown; result?: unknown; output?: unknown } | undefined
  // Same precedence as the shared renderer: result, then content, then output.
  const raw = p?.result ?? p?.content ?? p?.output
  if (typeof raw === 'string') return raw
  if (Array.isArray(raw)) {
    return raw
      .map((part) =>
        typeof part === 'string'
          ? part
          : typeof (part as { text?: unknown }).text === 'string'
            ? String((part as { text: string }).text)
            : '',
      )
      .join('\n')
  }
  if (raw && typeof raw === 'object') return JSON.stringify(raw)
  return ''
}

/**
 * The row's mark. A call that names its route wears that route's logo; the
 * letter behind it is what the folded group's header shows, where there is
 * room for a glyph but not for a plate.
 */
function glyphFor(call: TradeCall, outcome: TradeOutcome | null): string {
  const p = outcome?.provider
  if (p === 'aggregator') return 'A'
  if (p === 'uniswap') return 'U'
  switch (call.kind) {
    case 'swap':
    case 'quote':
      return '⇄'
    case 'send':
      return '→'
    case 'balances':
    case 'portfolio':
    case 'wallet':
      return '◎'
    case 'order':
    case 'orders':
    case 'approve':
    case 'reject':
      return '☐'
    case 'allowances':
    case 'revoke':
      return '⛨'
    case 'decode':
      return '⌕'
    case 'network':
      return '◉'
    case 'lp_collect':
    case 'lp_remove':
    case 'lp_add':
      return '◇'
    case 'bridge':
    case 'bridge_quote':
    case 'bridge_status':
      return '⇉'
    default:
      return isDcaKind(call.kind)
        ? '↻'
        : isTriggerKind(call.kind) || isBracketKind(call.kind)
          ? '⌖'
          : '›'
  }
}

/** A mandate's live status as the pill on its ledger row: the word key and its tone. */
const MANDATE_STAMPS: Record<string, { key: MessageKey; tone: string }> = {
  active: { key: 'trading.dca.state.active', tone: 'ok' },
  paused: { key: 'trading.dca.state.paused', tone: 'muted' },
  completed: { key: 'trading.dca.state.done', tone: 'ok' },
  stopped: { key: 'trading.dca.state.stopped', tone: 'muted' },
  rejected: { key: 'trading.dca.state.rejected', tone: 'muted' },
  expired: { key: 'trading.dca.state.expired', tone: 'muted' },
}

/** A trigger's live status as the pill on its ledger row: the word key and its tone. */
const TRIGGER_STAMPS: Record<string, { key: MessageKey; tone: string }> = {
  armed: { key: 'trading.trigger.state.armed', tone: 'ok' },
  triggered: { key: 'trading.trigger.state.triggered', tone: 'ok' },
  paused: { key: 'trading.trigger.state.paused', tone: 'muted' },
  done: { key: 'trading.trigger.state.done', tone: 'ok' },
  stopped: { key: 'trading.trigger.state.stopped', tone: 'muted' },
  rejected: { key: 'trading.trigger.state.rejected', tone: 'muted' },
  expired: { key: 'trading.trigger.state.expired', tone: 'muted' },
}

/** A mandate's live status by id, or undefined when no loaded list names it. */
export type MandateStatusOf = (mandateId: string) => string | undefined

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  node.className = className
  if (text !== undefined) node.textContent = text
  return node
}

function clock(ts: number): string {
  return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(ts)
}

/** The chain's explorer base URL by chain id, or null when it is not known. */
export type ExplorerFor = (chainId: number) => string | null | undefined

/**
 * A row's tx hash. With an explorer page it is a link — a new tab, which the
 * shell hands to the default browser — and without one (a truncated result on
 * a chain the desk has not loaded) it copies the hash, so it never does nothing.
 */
export function txHashNode(hash: string, url: string | null): HTMLElement {
  const short = hash.slice(0, 8) + '…'
  if (url) {
    const link = el('a', 'trd-ledger__link app-no-drag', short)
    link.href = url
    link.target = '_blank'
    link.rel = 'noopener noreferrer'
    link.title = `${t('trading.ledger.openTx')} · ${hash}`
    link.dataset.tx = 'link'
    // The row sits inside a folded group's <details>: the click opens the link, nothing else.
    link.addEventListener('click', (e) => e.stopPropagation())
    return link
  }
  const button = el('button', 'trd-ledger__link app-no-drag', short)
  button.type = 'button'
  button.title = `${t('trading.ledger.copyTx')} · ${hash}`
  button.dataset.tx = 'copy'
  button.addEventListener('click', (e) => {
    e.preventDefault()
    e.stopPropagation()
    void copyLpText(hash).then(
      () => {
        button.textContent = t('trading.ledger.copied')
        setTimeout(() => (button.textContent = short), 1_500)
      },
      () => {},
    )
  })
  return button
}

/** Build (or rebuild) the ledger row for one call. */
function renderRow(
  row: HTMLElement,
  call: TradeCall,
  outcome: TradeOutcome | null,
  timing: { running: boolean },
  onFocusApproval: (orderId: string | null) => void,
  details: HTMLElement,
  explorerFor: ExplorerFor,
): void {
  row.textContent = ''
  row.dataset.kind = call.kind
  row.dataset.state = timing.running
    ? 'running'
    : outcome?.error
      ? 'error'
      : outcome?.awaiting
        ? 'awaiting'
        : outcome?.confirmed
          ? 'confirmed'
          : 'done'

  const glyph = glyphFor(call, outcome)
  const mark = el('span', 'trd-ledger__mark', glyph)
  mark.setAttribute('aria-hidden', 'true')
  mark.dataset.glyph = glyph
  if (outcome?.provider) {
    mark.dataset.provider = outcome.provider
    const logo = providerMark(outcome.provider)
    // Static, bundled SVG from assets/providers: not user content.
    if (logo) mark.innerHTML = logo
  }
  row.appendChild(mark)

  const main = el('div', 'trd-ledger__main')
  const titleBits = [call.title]
  if (outcome?.provider) titleBits.push(providerLabel(outcome.provider))
  const title = el('div', 'trd-ledger__title', titleBits.join(' · '))
  const detail = outcome?.detail || call.detail
  if (detail) title.appendChild(el('span', 'trd-ledger__detail', detail))
  main.appendChild(title)
  const summaryText = timing.running
    ? t('trading.ledger.running')
    : outcome?.error
      ? outcome.error
      : outcome?.summary || ''
  if (summaryText) main.appendChild(el('div', 'trd-ledger__summary trd-mono', summaryText))
  row.appendChild(main)

  const side = el('div', 'trd-ledger__side')
  if (outcome?.awaiting) {
    const stamp = el('button', 'trd-ledger__stamp app-no-drag', t('trading.ledger.awaiting'))
    stamp.type = 'button'
    stamp.dataset.tone = 'warn'
    stamp.addEventListener('click', (e) => {
      e.preventDefault()
      e.stopPropagation()
      onFocusApproval(outcome.orderId)
    })
    side.appendChild(stamp)
  }
  // A DCA row whose mandate the live list knows: its pill is the mandate's
  // state now (awaiting approval is the stamp above), never the one recorded.
  const mandateStamp =
    !outcome?.awaiting && outcome?.mandateLive ? MANDATE_STAMPS[outcome.mandateLive] : undefined
  if (mandateStamp) {
    const stamp = el('span', 'trd-ledger__stamp', t(mandateStamp.key))
    stamp.dataset.tone = mandateStamp.tone
    stamp.dataset.mandate = outcome?.mandateLive ?? ''
    side.appendChild(stamp)
  }
  // A trigger row the same way: the trigger's state now, not the one recorded.
  const triggerStamp =
    !outcome?.awaiting && outcome?.triggerLive ? TRIGGER_STAMPS[outcome.triggerLive] : undefined
  if (triggerStamp) {
    const stamp = el('span', 'trd-ledger__stamp', t(triggerStamp.key))
    stamp.dataset.tone = triggerStamp.tone
    stamp.dataset.trigger = outcome?.triggerLive ?? ''
    side.appendChild(stamp)
  }
  if (outcome?.confirmed) {
    const stamp = el('span', 'trd-ledger__stamp', t('trading.ledger.confirmed'))
    stamp.dataset.tone = 'ok'
    side.appendChild(stamp)
  }
  if (outcome?.txHash) {
    side.appendChild(txHashNode(outcome.txHash, txExplorerUrl(outcome, explorerFor)))
  }
  // The market clock: when the figures in this result were read.
  if (outcome?.marketAt) {
    side.appendChild(
      el(
        'span',
        'trd-ledger__clock trd-mono',
        `${t('trading.ledger.market')} ${clock(outcome.marketAt)}`,
      ),
    )
  }
  // Duration only when measured: the shared renderer prints one when it timed
  // the call itself; a rebuilt row has none, and none is not zero.
  const measured = details.querySelector('.chat-tools-status')?.textContent?.trim() ?? ''
  if (!timing.running && /^\d/.test(measured)) {
    side.appendChild(el('span', 'trd-ledger__dur trd-mono', measured))
  }
  const raw = el('button', 'trd-ledger__raw app-no-drag', t('trading.ledger.raw'))
  raw.type = 'button'
  raw.setAttribute('aria-expanded', details.hidden ? 'false' : 'true')
  raw.addEventListener('click', (e) => {
    e.preventDefault()
    e.stopPropagation()
    details.hidden = !details.hidden
    if (!details.hidden && details instanceof HTMLDetailsElement) details.open = true
    raw.setAttribute('aria-expanded', details.hidden ? 'false' : 'true')
  })
  side.appendChild(raw)
  row.appendChild(side)
}

function refreshGroup(group: HTMLElement): void {
  const rows = Array.from(group.querySelectorAll<HTMLElement>(`:scope > .${ROW_CLASS}`))
  let summary = group.querySelector<HTMLElement>(':scope > summary')
  if (!summary) {
    summary = el('summary', 'trd-lgroup__summary')
    group.prepend(summary)
  }
  summary.textContent = ''
  const glyphs = new Set(
    rows.map((r) => {
      const mark = r.querySelector<HTMLElement>('.trd-ledger__mark')
      return mark?.dataset.glyph || mark?.textContent || ''
    }),
  )
  summary.appendChild(el('span', 'trd-lgroup__glyphs', [...glyphs].join(' ')))
  summary.appendChild(
    el('span', 'trd-lgroup__title', `${rows.length} ${t('trading.ledger.calls')}`),
  )
  const awaiting = rows.some((r) => r.dataset.state === 'awaiting')
  if (awaiting) {
    const stamp = el('span', 'trd-ledger__stamp', t('trading.ledger.awaiting'))
    stamp.dataset.tone = 'warn'
    summary.appendChild(stamp)
  }
  const live = rows.some((r) => r.dataset.state === 'running' || r.dataset.state === 'error')
  if ((awaiting || live) && group instanceof HTMLDetailsElement) group.open = true
}

/**
 * Fold ≥3 consecutive rows in a message body into one group. A row and the
 * raw block it fronts are one unit; an existing group counts as a run of
 * rows and absorbs its neighbours, so a call arriving after the fold joins
 * the same group instead of starting a second one.
 */
function foldRuns(body: HTMLElement): void {
  type Unit = { trade: boolean; group: HTMLElement | null; nodes: HTMLElement[] }
  const units: Unit[] = []
  for (const child of Array.from(body.children) as HTMLElement[]) {
    const prev = units[units.length - 1]
    if (child.classList.contains(GROUP_CLASS)) {
      units.push({ trade: true, group: child, nodes: [child] })
    } else if (child.classList.contains(ROW_CLASS)) {
      units.push({ trade: true, group: null, nodes: [child] })
    } else if (
      prev &&
      prev.trade &&
      !prev.group &&
      child.hasAttribute('data-tool-id') &&
      prev.nodes[0]!.getAttribute(ROW_ATTR) === child.getAttribute('data-tool-id')
    ) {
      prev.nodes.push(child)
    } else {
      units.push({ trade: false, group: null, nodes: [child] })
    }
  }
  const counted = units.map((u) => (u.group ? Math.max(LEDGER_GROUP_MIN, 1) : u.trade ? 1 : 0))
  // A run is worth folding when its rows (a group counts as ≥3) reach the minimum.
  let start = -1
  const runs: { start: number; length: number }[] = []
  for (let i = 0; i <= units.length; i++) {
    const trade = i < units.length && units[i]!.trade
    if (trade && start < 0) start = i
    if ((!trade || i === units.length) && start >= 0) {
      const end = i
      const rows = counted.slice(start, end).reduce((a, b) => a + b, 0)
      const hasGroup = units.slice(start, end).some((u) => u.group)
      if (end - start > 1 && (rows >= LEDGER_GROUP_MIN || hasGroup))
        runs.push({ start, length: end - start })
      start = -1
    }
  }
  for (const run of runs.reverse()) {
    const members = units.slice(run.start, run.start + run.length)
    if (members.length === 1 && members[0]!.group) continue
    const existing = members.find((u) => u.group)?.group ?? null
    const group = existing ?? el('details', GROUP_CLASS)
    if (!existing) body.insertBefore(group, members[0]!.nodes[0]!)
    for (const u of members) {
      if (u.group && u.group !== group) {
        for (const node of Array.from(u.group.children) as HTMLElement[]) {
          if (node.tagName !== 'SUMMARY') group.appendChild(node)
        }
        u.group.remove()
      } else if (!u.group) {
        for (const node of u.nodes) group.appendChild(node)
      }
    }
    refreshGroup(group)
  }
  // Keep every group's header current (a member's state may have changed).
  for (const group of Array.from(body.querySelectorAll<HTMLElement>(`:scope > .${GROUP_CLASS}`)))
    refreshGroup(group)
}

export function useTradeLedger(
  onFocusApproval: (orderId: string | null) => void,
  /** The open session: live calls belong to it, and are dropped when it changes. */
  sessionKey?: string,
): {
  seams: TranscriptEventSeams
  /** Attach to the element the transcript renders into (idempotent). */
  bind: (root: HTMLElement | null) => void
  /** Stop decorating (the chat left the desk); rows already drawn stay until the thread rebuilds. */
  unbind: () => void
} {
  const live = useRef(new Map<string, LiveCall>())
  const rootRef = useRef<HTMLElement | null>(null)
  const observerRef = useRef<MutationObserver | null>(null)
  const focusRef = useRef(onFocusApproval)
  useEffect(() => {
    focusRef.current = onFocusApproval
  }, [onFocusApproval])
  // The desk keeps trading.status cached; a hash without its own link borrows
  // its chain's explorer from there, read at draw time.
  const queryClient = useQueryClient()
  const explorerFor = useRef<ExplorerFor>(() => null)
  useEffect(() => {
    explorerFor.current = (chainId) =>
      queryClient
        .getQueryData<TradingStatus>(TRADING_KEYS.status)
        ?.chains?.find((c) => c.chainId === chainId)?.explorer ?? null
  }, [queryClient])
  // A DCA row's pill follows its mandate in the list the desk already holds
  // (every mandate when loaded, else the live ones), read at draw time.
  const mandateStatusOf = useRef<MandateStatusOf>(() => undefined)
  const triggerStatusOf = useRef<MandateStatusOf>(() => undefined)
  useEffect(() => {
    mandateStatusOf.current = (mandateId) => {
      for (const all of [true, false]) {
        const list = queryClient.getQueryData<MandateListPayload>(TRADING_KEYS.dca(all))
        const found = list?.mandates?.find((m) => m.id === mandateId)
        if (found) return found.status
      }
      return undefined
    }
    triggerStatusOf.current = (triggerId) => {
      // A bracket row carries its `brk_…` id in the trigger slot (one status
      // vocabulary): it follows the bracket list.
      if (triggerId.startsWith('brk_')) {
        for (const all of [true, false]) {
          const list = queryClient.getQueryData<BracketListPayload>(TRADING_KEYS.bracket(all))
          const found = list?.brackets?.find((b) => b.id === triggerId)
          if (found) return found.status
        }
        return undefined
      }
      for (const all of [true, false]) {
        const list = queryClient.getQueryData<TriggerListPayload>(TRADING_KEYS.trigger(all))
        const found = list?.triggers?.find((tr) => tr.id === triggerId)
        if (found) return found.status
      }
      return undefined
    }
  }, [queryClient])
  // Tool ids are per session; another session's rows are rebuilt from its
  // own blocks, and a map that only ever grew held every call ever seen.
  useEffect(() => {
    live.current.clear()
  }, [sessionKey])

  const decorate = useRef((root: HTMLElement) => {
    const blocks = root.querySelectorAll<HTMLElement>('details[data-tool-name="exec_command"]')
    const touched = new Set<HTMLElement>()
    blocks.forEach((details) => {
      const id = details.getAttribute('data-tool-id') || ''
      const known = id ? live.current.get(id) : undefined
      let call = known?.call ?? null
      if (!call) {
        const input = details.querySelector('.chat-tool-input')?.textContent ?? ''
        call =
          parseTradeCommand(commandFromToolInput(input)) ??
          cardCallFromResult(details.querySelector('.chat-tool-result-preview')?.textContent ?? '')
        if (!call) return
      }
      const running = details.classList.contains('chat-tools-collapse--running')
      let outcome = known?.outcome ?? null
      if (!outcome && !running) {
        const preview = details.querySelector('.chat-tool-result-preview')?.textContent ?? ''
        if (preview) outcome = parseTradeResult(call, preview)
      }
      if (outcome?.mandateId)
        outcome = withLiveMandate(outcome, mandateStatusOf.current(outcome.mandateId))
      if (outcome?.triggerId)
        outcome = withLiveTrigger(outcome, triggerStatusOf.current(outcome.triggerId))
      let row = details.previousElementSibling as HTMLElement | null
      if (!row || !row.classList.contains(ROW_CLASS) || row.getAttribute(ROW_ATTR) !== id) {
        row = el('div', ROW_CLASS)
        row.setAttribute(ROW_ATTR, id)
        details.parentElement?.insertBefore(row, details)
        details.hidden = true
      }
      const measured = details.querySelector('.chat-tools-status')?.textContent?.trim() ?? ''
      const signature = `${running}|${outcome?.detail ?? ''}|${outcome?.summary ?? ''}|${outcome?.status ?? ''}|${outcome?.awaiting ?? ''}|${outcome?.mandateLive ?? ''}|${outcome?.triggerLive ?? ''}|${measured}`
      if (row.dataset.sig !== signature) {
        row.dataset.sig = signature
        renderRow(
          row,
          call,
          outcome,
          { running },
          (orderId) => focusRef.current(orderId),
          details,
          (chainId) => explorerFor.current(chainId),
        )
      }
      const body = details.closest<HTMLElement>('.msg-body')
      if (body) touched.add(body)
    })
    touched.forEach((body) => foldRuns(body))
  })

  const bind = useCallback((root: HTMLElement | null) => {
    if (!root || rootRef.current === root) return
    observerRef.current?.disconnect()
    rootRef.current = root
    let scheduled = false
    const run = () => {
      scheduled = false
      decorate.current(root)
    }
    const observer = new MutationObserver(() => {
      if (scheduled) return
      scheduled = true
      requestAnimationFrame(run)
    })
    observer.observe(root, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['class'],
    })
    observerRef.current = observer
    run()
  }, [])

  // The mandate list moving (approved, paused, completed …) touches no DOM,
  // so the observer never hears it: redraw the rows when a DCA list lands.
  useEffect(() => {
    return queryClient.getQueryCache().subscribe((event) => {
      const key = event.query.queryKey
      if (event.type !== 'updated' || key[0] !== 'trading' || key[1] !== 'dca') return
      const root = rootRef.current
      if (root) requestAnimationFrame(() => decorate.current(root))
    })
  }, [queryClient])

  const unbind = useCallback(() => {
    observerRef.current?.disconnect()
    observerRef.current = null
    rootRef.current = null
  }, [])

  useEffect(
    () => () => {
      observerRef.current?.disconnect()
      observerRef.current = null
      rootRef.current = null
    },
    [],
  )

  const seams = useMemo<TranscriptEventSeams>(
    () => ({
      appendToolCall: (payload) => {
        if (toolName(payload) !== 'exec_command') return
        const id = toolId(payload)
        if (!id) return
        // The gateway sends the arguments with the result, so the command is
        // read then; only the start time is known here.
        const call = parseTradeCommand(commandFromToolInput(toolInput(payload)))
        live.current.set(id, { call, outcome: null, startedAt: Date.now(), finishedAt: null })
      },
      appendToolResult: (payload) => {
        const id = toolId(payload)
        if (!id) return
        if (toolName(payload) && toolName(payload) !== 'exec_command') return
        const entry = live.current.get(id) ?? {
          call: null,
          outcome: null,
          startedAt: null,
          finishedAt: null,
        }
        entry.call =
          entry.call ??
          parseTradeCommand(commandFromToolInput(toolInput(payload))) ??
          cardCallFromResult(toolResultText(payload))
        if (!entry.call) {
          live.current.delete(id)
          return
        }
        entry.outcome = parseTradeResult(entry.call, toolResultText(payload))
        entry.finishedAt = Date.now()
        live.current.set(id, entry)
        const root = rootRef.current
        if (root) requestAnimationFrame(() => decorate.current(root))
      },
    }),
    [],
  )

  return { seams, bind, unbind }
}
