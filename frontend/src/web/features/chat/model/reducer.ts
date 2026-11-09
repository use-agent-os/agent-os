// The transcript reducer (overhaul-control-ui D7). Pure: (state, input) ->
// state. History pages and live session events fold into the same turns; the
// kept pure helpers merge and de-duplicate history pages.
import {
  applyHistoryDelta,
  mergeHistoryMessagePages,
  type HistoryPagingState,
  type HistoryResponse,
} from '@/views/chat/transcript/history'
import { artifactCategory, type Artifact } from '@/views/chat/transcript/artifacts'
import { parseSubagentCompletion } from '@/views/chat/transcript/tools'
import type { ChatMessage } from '@/views/chat/types'
import type {
  CompactionSummaryRow,
  HistoryPage,
  HistoryRow,
  Item,
  TranscriptInput,
  TranscriptState,
  Turn,
  TurnUsage,
} from './types'

/** The note the runtime seals an interrupted turn with (engine/agent.py). */
export const TURN_INTERRUPTED_NOTE =
  '[The user interrupted this turn before it finished. Tool calls may have partly run.]'
/** The result the runtime gives a tool call cut off by a stop (engine/agent.py). */
export const CANCELLED_TOOL_RESULT =
  'Cancelled: the user stopped the turn before this call finished. It may have partly run; check its effects before relying on them.'

/** Artifact categories rendered as in-transcript cards rather than chips. */
export const CARD_CATEGORIES = new Set([
  'chart',
  'cards',
  'lp',
  'markets',
  'dca',
  'trigger',
  'flowrun',
])

const PLAN_TOOL = 'exit_plan_mode'
const ASK_TOOL = 'ask_user'
const SPAWN_TOOL = 'sessions_spawn'

export function initialTranscript(): TranscriptState {
  return {
    rows: [],
    summaries: [],
    hasMore: false,
    oldestCursor: null,
    newestCursor: null,
    liveTurns: [],
    turns: [],
    epoch: 0,
    activeGroups: [],
    needsResync: false,
    queued: 0,
    notices: [],
    seq: 0,
  }
}

// ── small helpers ───────────────────────────────────────────────────────────

export function toMs(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Math.abs(value) < 10_000_000_000 ? value * 1000 : value
  }
  if (typeof value === 'string' && value.trim()) {
    const n = Number(value)
    if (Number.isFinite(n)) return toMs(n)
    const parsed = Date.parse(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : value == null ? '' : String(value)
}

function num(value: unknown): number | undefined {
  const n = Number(value)
  return Number.isFinite(n) ? n : undefined
}

function resultText(value: unknown): string {
  if (typeof value === 'string') return value
  if (value == null) return ''
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function toolIsError(p: Record<string, unknown>): boolean {
  const status = (p.execution_status as { status?: unknown } | undefined)?.status
  if (status === 'error' || status === 'timeout' || status === 'cancelled') return true
  return Boolean(p.is_error ?? p.isError ?? false)
}

function toolIsTruncated(p: Record<string, unknown>): boolean {
  const status = p.execution_status as { truncated?: unknown } | undefined
  return Boolean(status?.truncated ?? p.result_truncated ?? false)
}

/** Strip the runtime's interrupted note; report whether it was there. */
function stripInterrupted(text: string): { text: string; interrupted: boolean } {
  const trimmed = text.trimEnd()
  if (!trimmed.endsWith(TURN_INTERRUPTED_NOTE)) return { text, interrupted: false }
  return { text: trimmed.slice(0, -TURN_INTERRUPTED_NOTE.length).trimEnd(), interrupted: true }
}

function usageFrom(source: Record<string, unknown> | undefined | null): TurnUsage | undefined {
  if (!source) return undefined
  const model = str(source.model) || str(source.routed_model)
  const usage: TurnUsage = {
    model: model || undefined,
    inputTokens: num(source.input_tokens ?? source.inputTokens),
    outputTokens: num(source.output_tokens ?? source.outputTokens),
    costUsd: num(source.cost_usd),
  }
  return Object.values(usage).some((v) => v !== undefined) ? usage : undefined
}

/** A router decision recorded in a turn's usage (live `done` or history row). */
function routerFromUsage(source: Record<string, unknown> | undefined | null) {
  if (!source) return null
  const model = str(source.routed_model)
  const routing = str(source.routing_source)
  if (!model || !routing || routing === 'none') return null
  const tier = str(source.routed_tier) || null
  return { tier, model }
}

// ── item builders shared by the live and the history paths ──────────────────

type Mint = () => string

/**
 * Artifacts sit at the end of a turn — where history keeps them (on the
 * message row) — whenever they arrived live. Everything else goes before that
 * trailing run.
 */
function contentEnd(items: readonly Item[]): number {
  let end = items.length
  while (end > 0 && (items[end - 1]!.kind === 'artifact' || items[end - 1]!.kind === 'card')) end--
  return end
}

function insertContent(items: Item[], item: Item): Item[] {
  const end = contentEnd(items)
  return [...items.slice(0, end), item, ...items.slice(end)]
}

function appendText(items: Item[], text: string, mint: Mint): Item[] {
  if (!text) return items
  const end = contentEnd(items)
  const last = items[end - 1]
  if (last?.kind === 'text') {
    return [...items.slice(0, end - 1), { ...last, text: last.text + text }, ...items.slice(end)]
  }
  return insertContent(items, { kind: 'text', id: mint(), text })
}

function startTool(
  items: Item[],
  toolUseId: string,
  name: string,
  args: unknown,
  at: number | undefined,
  mint: Mint,
): Item[] {
  if (items.some((i) => i.kind === 'toolCall' && i.toolUseId === toolUseId)) return items
  return insertContent(items, {
    kind: 'toolCall',
    id: mint(),
    toolUseId,
    name,
    args,
    isError: false,
    truncated: false,
    state: 'running',
    startedAt: at,
  })
}

/** Cards a settled tool result carries (plan, ask_user, ACP spawn). */
function cardsForTool(
  name: string,
  toolUseId: string,
  args: unknown,
  result: unknown,
  isError: boolean,
  mint: Mint,
): Item[] {
  if (isError) return []
  if (name === PLAN_TOOL) {
    let parsed: unknown = result
    if (typeof parsed === 'string') {
      try {
        parsed = JSON.parse(parsed)
      } catch {
        return []
      }
    }
    const body = parsed as { status?: unknown; plan?: unknown } | null
    const plan =
      body && body.status === 'presented' && typeof body.plan === 'string' ? body.plan.trim() : ''
    return plan ? [{ kind: 'plan', id: mint(), plan, toolUseId }] : []
  }
  if (name === ASK_TOOL) return [{ kind: 'ask', id: mint(), toolUseId, payload: result }]
  if (name === SPAWN_TOOL) {
    const runtime = (args as { runtime?: unknown } | null)?.runtime
    if (runtime === 'acp') return [{ kind: 'acp', id: mint(), toolUseId, args, result }]
  }
  return []
}

function settleTool(
  items: Item[],
  p: Record<string, unknown>,
  at: number | undefined,
  mint: Mint,
): Item[] {
  const toolUseId = str(p.tool_use_id)
  const name = str(p.tool_name || p.name)
  const isError = toolIsError(p)
  const result = resultText(p.result)
  let found = false
  let args: unknown = p.arguments ?? p.input
  const next = items.map((item) => {
    if (item.kind !== 'toolCall' || item.toolUseId !== toolUseId) return item
    found = true
    args = args ?? item.args
    return {
      ...item,
      name: item.name || name,
      args,
      result,
      isError,
      truncated: toolIsTruncated(p),
      state: isError ? ('failed' as const) : ('done' as const),
      endedAt: at,
    }
  })
  let settled = found
    ? next
    : insertContent(items, {
        kind: 'toolCall' as const,
        id: mint(),
        toolUseId,
        name,
        args,
        result,
        isError,
        truncated: toolIsTruncated(p),
        state: isError ? ('failed' as const) : ('done' as const),
        endedAt: at,
      })
  for (const card of cardsForTool(name, toolUseId, args, p.result, isError, mint)) {
    settled = insertContent(settled, card)
  }
  return settled
}

function artifactItem(artifact: Artifact, mint: Mint): Item {
  const category = artifactCategory(artifact)
  return CARD_CATEGORIES.has(category)
    ? { kind: 'card', id: mint(), category, artifact }
    : { kind: 'artifact', id: mint(), artifact }
}

function withRouter(
  items: Item[],
  router: { tier: string | null; model: string },
  mint: Mint,
): Item[] {
  const rest = items.filter((i) => i.kind !== 'routerDecision')
  return [{ kind: 'routerDecision', id: mint(), ...router }, ...rest]
}

/** Stop-cut tool calls end as failed with the runtime's cancelled result. */
function cancelRunningTools(items: Item[]): Item[] {
  return items.map((i) =>
    i.kind === 'toolCall' && i.state === 'running'
      ? { ...i, state: 'failed', isError: true, result: CANCELLED_TOOL_RESULT }
      : i,
  )
}

// ── history fold ────────────────────────────────────────────────────────────

function isCronRow(row: HistoryRow): boolean {
  return row.role === 'assistant' && /cron|automation/i.test(str(row.provenance_kind))
}

/** Fold merged history rows (and compaction summaries) into turns. */
export function foldHistory(
  rows: readonly HistoryRow[],
  summaries: readonly CompactionSummaryRow[] = [],
): Turn[] {
  const turns: Turn[] = []
  let current: Turn | null = null
  let n = 0
  const pending = [...summaries]
    .map((s) => ({ s, at: toMs(s.created_at) ?? Number.POSITIVE_INFINITY }))
    .sort((a, b) => a.at - b.at)

  const standalone = (key: string, item: Item, at: number | null): Turn => ({
    id: `h:${key}`,
    userMessage: null,
    items: [item],
    status: 'done',
    startedAt: at,
    endedAt: at,
  })

  const flushSummaries = (before: number | null) => {
    while (pending.length && (before === null || pending[0]!.at <= before)) {
      const { s, at } = pending.shift()!
      current = null
      const key = str(s.compaction_id || s.id) || `c${n++}`
      turns.push(
        standalone(
          `compaction:${key}`,
          {
            kind: 'compaction',
            id: `h:compaction:${key}:0`,
            summary: str(s.summary_text),
            removedCount: num(s.removed_count),
          },
          Number.isFinite(at) ? at : null,
        ),
      )
    }
  }

  for (const row of rows) {
    const at = toMs(row.timestamp)
    flushSummaries(at)
    const rowId = str(row.message_id || row.id) || `r${n++}`

    if (row.role === 'user') {
      current = {
        id: `h:${rowId}`,
        userMessage: {
          id: rowId,
          text: str(row.text),
          timestamp: at,
          attachments: Array.isArray(row.attachments) ? row.attachments : [],
        },
        items: [],
        status: 'done',
        startedAt: at,
        endedAt: at,
      }
      turns.push(current)
      continue
    }

    if (isCronRow(row)) {
      current = null
      turns.push(
        standalone(rowId, { kind: 'cronResult', id: `h:${rowId}:0`, text: str(row.text) }, at),
      )
      continue
    }

    if (row.role === 'system') {
      const completion =
        parseSubagentCompletion(str(row.text)) ??
        (str(row.provenance_source_tool) === 'subagent_completion'
          ? { type: 'subagent_completion' as const, result: row.text }
          : null)
      if (!completion) continue
      const item: Item = { kind: 'subagentCompletion', id: `h:${rowId}:0`, payload: completion }
      if (current) current.items = [...current.items, item]
      else turns.push(standalone(rowId, item, at))
      continue
    }

    if (!current) {
      current = {
        id: `h:${rowId}`,
        userMessage: null,
        items: [],
        status: 'done',
        startedAt: at,
        endedAt: at,
      }
      turns.push(current)
    }
    const turn: Turn = current
    let k = turn.items.length
    const mint: Mint = () => `${turn.id}:${k++}`

    if (row.role === 'error') {
      turn.status = 'failed'
      turn.failure = str(row.text)
      turn.endedAt = at ?? turn.endedAt
      continue
    }
    if (row.role !== 'assistant') continue

    let items = turn.items
    const router = routerFromUsage(row.usage)
    if (router) items = withRouter(items, router, mint)
    if (row.has_thinking && !items.some((i) => i.kind === 'thinkingRef')) {
      items = [...items, { kind: 'thinkingRef', id: mint(), messageId: rowId }]
    }

    let interrupted = false
    const segments = Array.isArray(row.tool_calls) ? row.tool_calls : null
    if (segments && segments.length > 0) {
      for (const seg of segments) {
        const type = str(seg.type)
        if (type === 'text') {
          const stripped = stripInterrupted(str(seg.text))
          interrupted ||= stripped.interrupted
          items = appendText(items, stripped.text, mint)
        } else if (type === 'tool_use') {
          items = startTool(items, str(seg.tool_use_id), str(seg.name), seg.input, undefined, mint)
        } else if (type === 'tool_result') {
          items = settleTool(items, seg, undefined, mint)
        }
      }
    } else {
      const stripped = stripInterrupted(str(row.text))
      interrupted = stripped.interrupted
      items = appendText(items, stripped.text, mint)
    }
    for (const artifact of row.artifacts ?? []) items = [...items, artifactItem(artifact, mint)]

    turn.items = items
    turn.usage = usageFrom(row.usage) ?? turn.usage
    turn.endedAt = at ?? turn.endedAt
    if (interrupted) turn.status = 'aborted'
  }
  flushSummaries(null)
  return turns
}

// ── live events ─────────────────────────────────────────────────────────────

const LIVE_STARTERS = new Set([
  'text_delta',
  'thinking',
  'tool_use_start',
  'tool_result',
  'artifact',
  'router_decision',
  'run_heartbeat',
  'steering',
])

function mintFor(state: { seq: number }): Mint {
  return () => `l${++state.seq}`
}

function currentLive(state: TranscriptState): Turn | null {
  const last = state.liveTurns.at(-1)
  return last && last.status === 'live' ? last : null
}

function replaceLast(state: TranscriptState, turn: Turn): Turn[] {
  return [...state.liveTurns.slice(0, -1), turn]
}

function endTurn(turn: Turn, status: Turn['status'], at: number, failure?: string): Turn {
  return {
    ...turn,
    status,
    endedAt: at,
    failure: failure ?? turn.failure,
    items: status === 'aborted' ? cancelRunningTools(turn.items) : turn.items,
  }
}

function applyEvent(
  state: TranscriptState,
  name: string,
  p: Record<string, unknown>,
  at: number,
): TranscriptState {
  const epoch = Number(p.epoch)
  if (Number.isFinite(epoch) && epoch < state.epoch) return state

  if (name === 'session.epoch_changed') {
    return {
      ...state,
      epoch: Number.isFinite(epoch) ? epoch : state.epoch + 1,
      liveTurns: [],
      activeGroups: [],
      needsResync: true,
    }
  }

  if (name.startsWith('session.event.task_group.')) {
    const phase = name.slice('session.event.task_group.'.length)
    const group = str(p.group_id)
    if (!group) return state
    const active = state.activeGroups.filter((g) => g !== group)
    if (phase === 'waiting' || phase === 'synthesizing') active.push(group)
    return { ...state, activeGroups: active }
  }

  // Backstops for a run that ends without its terminal frame reaching us.
  if (
    name === 'task.cancelled' ||
    name === 'task.failed' ||
    name === 'task.timeout' ||
    name === 'task.abandoned'
  ) {
    const live = currentLive(state)
    if (!live) return state
    const status = name === 'task.cancelled' ? 'aborted' : 'failed'
    return {
      ...state,
      liveTurns: replaceLast(state, endTurn(live, status, at, str(p.error) || undefined)),
    }
  }

  if (!name.startsWith('session.event.')) return state
  const kind = name.slice('session.event.'.length)
  const next: TranscriptState = { ...state }
  const mint = mintFor(next)

  if (kind === 'warning') {
    return { ...next, notices: [...state.notices, { id: mint(), text: str(p.message) }] }
  }

  if (kind === 'cron_result') {
    const message = (p.message ?? {}) as Record<string, unknown>
    const turn: Turn = {
      id: mint(),
      userMessage: null,
      items: [{ kind: 'cronResult', id: mint(), text: str(message.text) }],
      status: 'done',
      startedAt: at,
      endedAt: at,
    }
    return { ...next, liveTurns: [...state.liveTurns, turn] }
  }

  if (kind === 'compaction') {
    if (str(p.status) !== 'completed') return state
    const turn: Turn = {
      id: mint(),
      userMessage: null,
      items: [
        {
          kind: 'compaction',
          id: mint(),
          summary: str(p.summary),
          removedCount: num(p.removed_count),
        },
      ],
      status: 'done',
      startedAt: at,
      endedAt: at,
    }
    return { ...next, liveTurns: [...state.liveTurns, turn] }
  }

  let live = currentLive(state)
  if (!live) {
    if (!LIVE_STARTERS.has(kind) && !(kind === 'state_change' && str(p.to_state) === 'thinking')) {
      if (kind === 'subagent_completion') {
        const turn: Turn = {
          id: mint(),
          userMessage: null,
          items: [{ kind: 'subagentCompletion', id: mint(), payload: p }],
          status: 'done',
          startedAt: at,
          endedAt: at,
        }
        return { ...next, liveTurns: [...state.liveTurns, turn] }
      }
      return state
    }
    // A run started elsewhere (a channel, an automation, another tab).
    live = {
      id: mint(),
      userMessage: null,
      items: [],
      status: 'live',
      startedAt: at,
      endedAt: null,
    }
    next.liveTurns = [...state.liveTurns, live]
  }

  let items = live.items
  let turn: Turn = live
  switch (kind) {
    case 'text_delta':
      items = appendText(items, str(p.text), mint)
      break
    case 'thinking': {
      const index = items.findIndex((i) => i.kind === 'thinkingRef')
      if (index >= 0) {
        const ref = items[index] as Extract<Item, { kind: 'thinkingRef' }>
        items = items.map((i, j) =>
          j === index ? { ...ref, text: (ref.text ?? '') + str(p.text) } : i,
        )
      } else {
        items = insertContent(items, {
          kind: 'thinkingRef',
          id: mint(),
          messageId: null,
          text: str(p.text),
        })
      }
      break
    }
    case 'tool_use_start':
      items = startTool(items, str(p.tool_use_id), str(p.tool_name), undefined, at, mint)
      break
    case 'tool_result':
      items = settleTool(items, p, at, mint)
      break
    case 'artifact': {
      // Drop the frame's envelope stamps; keep the artifact as history has it.
      const { epoch: _epoch, stream_seq: _seq, ...artifact } = p
      void _epoch
      void _seq
      items = [...items, artifactItem(artifact as Artifact, mint)]
      break
    }
    case 'router_decision': {
      const model = str(p.model)
      if (model) items = withRouter(items, { tier: str(p.tier) || null, model }, mint)
      break
    }
    case 'steering':
      items = insertContent(items, { kind: 'steering', id: mint(), text: str(p.text) })
      break
    case 'subagent_completion':
      items = insertContent(items, { kind: 'subagentCompletion', id: mint(), payload: p })
      break
    case 'error': {
      const message = str(p.terminal_message || p.error_message || p.message) || undefined
      const cancelled = /cancel/i.test(str(p.code))
      turn = endTurn(
        { ...turn, items },
        cancelled ? 'aborted' : 'failed',
        at,
        cancelled ? undefined : message,
      )
      return { ...next, liveTurns: replaceLast(next, turn) }
    }
    case 'done': {
      const outcome = (p.outcome ?? {}) as Record<string, unknown>
      const outcomeKind = str(outcome.kind)
      const aborted =
        str(p.state) === 'aborted' || str(p.reason) === 'aborted' || outcomeKind === 'interrupted'
      const failed = !aborted && (outcomeKind === 'failed' || outcomeKind === 'blocked')
      const hasText = items.some((i) => i.kind === 'text' && i.text.trim())
      if (!hasText)
        items = appendText(items, str(aborted ? p.partial_text || p.text : p.text), mint)
      const router = routerFromUsage(p)
      if (router) items = withRouter(items, router, mint)
      turn = {
        ...endTurn(
          { ...turn, items },
          aborted ? 'aborted' : failed ? 'failed' : 'done',
          at,
          failed ? str(outcome.error_message || outcome.reason) || undefined : undefined,
        ),
        usage: usageFrom(p) ?? turn.usage,
      }
      return { ...next, liveTurns: replaceLast(next, turn) }
    }
    default:
      break
  }
  turn = { ...turn, items }
  return { ...next, liveTurns: replaceLast(next, turn) }
}

// ── reconciliation ──────────────────────────────────────────────────────────

/**
 * Drop live turns history now holds; keep a still-running one (history hides
 * a turn in progress) and let it supersede history's copy of its user row.
 */
function reconcile(liveTurns: Turn[], rows: readonly HistoryRow[]): Turn[] {
  const used = new Set<number>()
  const kept: Turn[] = []
  for (const turn of liveTurns) {
    if (!turn.userMessage) {
      if (turn.status === 'live') kept.push(turn)
      continue
    }
    if (turn.userMessage.delivery === 'not_sent') {
      kept.push(turn)
      continue
    }
    let match = -1
    for (let i = rows.length - 1; i >= 0; i--) {
      if (used.has(i)) continue
      const row = rows[i]!
      if (row.role === 'user' && str(row.text) === turn.userMessage.text) {
        match = i
        break
      }
    }
    if (match < 0) {
      kept.push(turn)
      continue
    }
    used.add(match)
    const answered = rows.slice(match + 1).some((r) => r.role === 'assistant' || r.role === 'error')
    if (turn.status === 'live' && !answered) kept.push(turn)
  }
  return kept
}

function deriveTurns(state: TranscriptState): Turn[] {
  const history = foldHistory(state.rows, state.summaries)
  const liveUsers = new Set(
    state.liveTurns
      .filter((t) => t.userMessage && t.status === 'live')
      .map((t) => t.userMessage!.text),
  )
  // A turn still running live supersedes history's partial copy of it.
  const trimmed = liveUsers.size
    ? history.filter(
        (t, i) =>
          !(
            i >= history.length - liveUsers.size &&
            t.userMessage &&
            liveUsers.has(t.userMessage.text)
          ),
      )
    : history
  return [...trimmed, ...state.liveTurns]
}

function applyHistory(
  state: TranscriptState,
  page: HistoryPage,
  mode: 'replace' | 'older' | 'delta',
): TranscriptState {
  const messages = (page.messages ?? []) as HistoryRow[]
  let rows: HistoryRow[]
  let needsResync = false
  let hasMore = state.hasMore
  let oldestCursor = state.oldestCursor
  let newestCursor = page.newest_cursor ?? state.newestCursor

  if (mode === 'older') {
    rows = mergeHistoryMessagePages(
      messages as unknown as ChatMessage[],
      state.rows as unknown as ChatMessage[],
    ) as unknown as HistoryRow[]
    hasMore = Boolean(page.has_more)
    oldestCursor = page.oldest_cursor ?? oldestCursor
    newestCursor = state.newestCursor
  } else if (mode === 'delta') {
    const merged = applyHistoryDelta(
      {
        loadedMessages: state.rows as unknown as ChatMessage[],
        hasMore: state.hasMore,
        oldestCursor: state.oldestCursor,
        scope: 'latest_window',
        loadingEarlier: false,
        error: '',
        compactionSummaries: state.summaries,
      } satisfies HistoryPagingState,
      page as unknown as HistoryResponse,
    )
    if (!merged) return { ...state, needsResync: true }
    rows = (merged.messages ?? []) as unknown as HistoryRow[]
  } else {
    rows = messages
    hasMore = Boolean(page.has_more)
    oldestCursor = page.oldest_cursor ?? null
    needsResync = false
  }

  const summaries =
    mode === 'replace'
      ? (page.compaction_summaries ?? [])
      : [...state.summaries, ...(page.compaction_summaries ?? [])]
  let liveTurns = mode === 'older' ? state.liveTurns : reconcile(state.liveTurns, rows)

  // Opened mid-run: adopt the in-flight run as the live turn.
  const inFlight = page.in_flight_run
  const next: TranscriptState = { ...state }
  if (mode !== 'older' && inFlight && !liveTurns.some((t) => t.status === 'live')) {
    const mint = mintFor(next)
    const lastUser = [...rows].reverse().find((r) => r.role === 'user')
    liveTurns = [
      ...liveTurns,
      {
        id: mint(),
        userMessage: lastUser
          ? {
              id: str(lastUser.message_id || lastUser.id),
              text: str(lastUser.text),
              timestamp: toMs(lastUser.timestamp),
              attachments: Array.isArray(lastUser.attachments) ? lastUser.attachments : [],
            }
          : null,
        items: appendText([], str(inFlight.text), mint),
        status: 'live',
        startedAt: toMs(inFlight.started_at),
        endedAt: null,
      },
    ]
  }

  const out: TranscriptState = {
    ...next,
    rows,
    summaries,
    hasMore,
    oldestCursor,
    newestCursor,
    liveTurns,
    needsResync: mode === 'replace' ? false : needsResync || state.needsResync,
  }
  return { ...out, turns: deriveTurns(out) }
}

// ── the reducer ─────────────────────────────────────────────────────────────

export function reduce(state: TranscriptState, input: TranscriptInput): TranscriptState {
  switch (input.type) {
    case 'reset':
      return initialTranscript()
    case 'historyPage':
      return applyHistory(state, input.page, input.mode)
    case 'event': {
      const next = applyEvent(state, input.name, input.payload, input.at)
      return next === state ? state : { ...next, turns: deriveTurns(next) }
    }
    case 'optimisticSend': {
      const next: TranscriptState = { ...state }
      const mint = mintFor(next)
      // A previous live turn that never got its terminal frame is over now.
      const prior = state.liveTurns.map((t) =>
        t.status === 'live' ? endTurn(t, 'done', input.at) : t,
      )
      const turn: Turn = {
        id: mint(),
        userMessage: {
          id: mint(),
          text: input.text,
          timestamp: input.at,
          attachments: input.attachments ?? [],
          delivery: 'sending',
          idempotencyKey: input.idempotencyKey,
        },
        items: [],
        status: 'live',
        startedAt: input.at,
        endedAt: null,
      }
      const out = { ...next, liveTurns: [...prior, turn] }
      return { ...out, turns: deriveTurns(out) }
    }
    case 'sendAccepted':
    case 'sendFailed': {
      const liveTurns = state.liveTurns.map((t) => {
        if (t.userMessage?.idempotencyKey !== input.idempotencyKey) return t
        const userMessage = { ...t.userMessage }
        if (input.type === 'sendAccepted') {
          delete userMessage.delivery
          return { ...t, userMessage }
        }
        userMessage.delivery = input.notSent ? 'not_sent' : undefined
        return {
          ...t,
          userMessage,
          status: 'failed' as const,
          failure: input.error,
          endedAt: t.startedAt,
        }
      })
      const out = { ...state, liveTurns }
      return { ...out, turns: deriveTurns(out) }
    }
    case 'queue':
      return { ...state, queued: input.count }
    case 'abortRequested': {
      const live = currentLive(state)
      if (!live) return state
      const out = { ...state, liveTurns: replaceLast(state, { ...live, abortRequested: true }) }
      return { ...out, turns: deriveTurns(out) }
    }
  }
}

/** True while this session has work in flight (a live turn or subagent groups). */
export function isRunning(state: TranscriptState): boolean {
  return state.turns.some((t) => t.status === 'live') || state.activeGroups.length > 0
}

/**
 * A turn reduced to what the user sees, for comparing a live fold with the
 * same turn restored from history: ids, timings and delivery bookkeeping go;
 * streamed reasoning and its history reference compare as "a Thinking block".
 */
export function comparableTurn(turn: Turn): unknown {
  return {
    user: turn.userMessage ? { text: turn.userMessage.text } : null,
    status: turn.status,
    failure: turn.failure ?? null,
    usage: turn.usage ?? null,
    items: turn.items.map((item) => {
      const { id: _id, ...rest } = item
      void _id
      if (rest.kind === 'thinkingRef') return { kind: 'thinkingRef' }
      if (rest.kind === 'toolCall') {
        // Live, a call's arguments arrive with its result; a call cut off by a
        // stop has none until history supplies them.
        const { startedAt: _s, endedAt: _e, args: _a, ...tool } = rest
        void _s
        void _e
        void _a
        return tool
      }
      return rest
    }),
  }
}
