// The transcript reducer (overhaul-control-ui D7). Pure: (state, input) ->
// state. History pages and live session events fold into the same turns; the
// kept pure helpers merge and de-duplicate history pages.
import {
  applyHistoryDelta,
  mergeHistoryMessagePages,
  messagePageIdentity,
  type HistoryPagingState,
  type HistoryResponse,
} from '@/views/chat/transcript/history'
import { artifactCategory, type Artifact } from '@/views/chat/transcript/artifacts'
import { parseSubagentCompletion } from '@/views/chat/transcript/tools'
import { stripTimePrefix } from '@/views/chat/logic'
import type { ChatMessage } from '@/views/chat/types'
import type {
  CompactionSummaryRow,
  HistoryMode,
  HistoryPage,
  HistoryRow,
  InFlightRun,
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
    streamFloor: 0,
    failures: {},
    pendingInputs: [],
    endedRuns: [],
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

/** A user row's text as typed: without the gateway's time prefix. */
function userText(row: HistoryRow): string {
  return stripTimePrefix(str(row.text))
}

/** A persisted failure: the `system` row a failed turn leaves ("Error: …"). */
function errorRowText(row: HistoryRow): string | null {
  if (row.role === 'error') return str(row.text)
  if (row.role !== 'system') return null
  const text = str(row.text)
  return /^\s*error:/i.test(text) ? text.replace(/^\s*error:\s*/i, '').trim() : null
}

function isErrorRow(row: HistoryRow): boolean {
  return errorRowText(row) !== null
}

/** The run (task) id a frame or snapshot names. */
function runIdOf(p: Record<string, unknown> | null | undefined): string {
  return p ? str(p.run_id) || str(p.task_id) : ''
}

/** Runs a view remembers as ended; older ones cannot be named any more. */
const ENDED_RUNS_KEPT = 32

function withEndedRun(ended: string[], runId: string): string[] {
  if (!runId || ended.includes(runId)) return ended
  return [...ended, runId].slice(-ENDED_RUNS_KEPT)
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
          text: userText(row),
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

    const failure = errorRowText(row)
    if (failure !== null) {
      // A failed turn's persisted reason ends that turn as failed.
      if (current) {
        current.status = 'failed'
        current.failure = failure || current.failure
        current.endedAt = at ?? current.endedAt
      }
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

/** The frames that end a run's task (published once the run settled). */
export const TASK_TERMINALS: ReadonlySet<string> = new Set([
  'task.succeeded',
  'task.cancelled',
  'task.failed',
  'task.timeout',
  'task.abandoned',
])

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
  // Already folded into an adopted in-flight snapshot (a replay after it).
  const streamSeq = Number(p.stream_seq)
  const hasSeq = p.stream_seq != null && Number.isFinite(streamSeq)
  if (hasSeq && streamSeq <= state.streamFloor) return state

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

  // A run's lifecycle end. It is final: the live turn of that run ends (the
  // backstop for a terminal frame that never reached us), and a snapshot
  // that still names the run later is stale.
  if (TASK_TERMINALS.has(name)) {
    const runId = runIdOf(p)
    const endedRuns = withEndedRun(state.endedRuns, runId)
    const index = state.liveTurns.findIndex(
      (t) => t.status === 'live' && Boolean(runId) && t.runId === runId,
    )
    let target = index
    // A turn whose run id is not known yet: only failures end it, as before;
    // never one still being sent (it is the next run, not this one).
    if (target < 0 && name !== 'task.succeeded') {
      const last = state.liveTurns.length - 1
      const live = currentLive(state)
      if (live && !live.runId && live.userMessage?.delivery !== 'sending') target = last
    }
    if (target < 0) return endedRuns === state.endedRuns ? state : { ...state, endedRuns }
    const status =
      name === 'task.succeeded' ? 'done' : name === 'task.cancelled' ? 'aborted' : 'failed'
    const failure = status === 'failed' ? str(p.error) || undefined : undefined
    return {
      ...state,
      endedRuns,
      liveTurns: state.liveTurns.map((t, i) =>
        i === target ? endTurn(t, status, at, failure) : t,
      ),
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
    if (kind === 'done' || kind === 'error') {
      // No turn to end, but the run is over: a snapshot naming it is stale.
      const endedRuns = withEndedRun(state.endedRuns, runIdOf(p))
      return endedRuns === state.endedRuns ? state : { ...state, endedRuns }
    }
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
      since: at,
    }
    next.liveTurns = [...state.liveTurns, live]
  }

  let items = live.items
  let turn: Turn = hasSeq ? { ...live, lastSeq: Math.max(live.lastSeq ?? 0, streamSeq) } : live
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
      if (runIdOf(p) && live.runId && live.runId !== runIdOf(p)) {
        return { ...state, endedRuns: withEndedRun(state.endedRuns, runIdOf(p)) }
      }
      const message = str(p.terminal_message || p.error_message || p.message) || undefined
      const cancelled = /cancel/i.test(str(p.code))
      turn = endTurn(
        { ...turn, items },
        cancelled ? 'aborted' : 'failed',
        at,
        cancelled ? undefined : message,
      )
      return {
        ...next,
        endedRuns: withEndedRun(state.endedRuns, runIdOf(p) || str(turn.runId)),
        liveTurns: replaceLast(next, turn),
      }
    }
    case 'done': {
      if (runIdOf(p) && live.runId && live.runId !== runIdOf(p)) {
        // Another run's end: this turn goes on.
        return { ...state, endedRuns: withEndedRun(state.endedRuns, runIdOf(p)) }
      }
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
      return {
        ...next,
        endedRuns: withEndedRun(state.endedRuns, runIdOf(p) || str(turn.runId)),
        liveTurns: replaceLast(next, turn),
      }
    }
    default:
      break
  }
  turn = { ...turn, items }
  return { ...next, liveTurns: replaceLast(next, turn) }
}

// ── reconciliation ──────────────────────────────────────────────────────────

function rowIdOf(row: HistoryRow): string {
  return str(row.message_id || row.id)
}

/** The id of the newest row the view holds: a message sent now comes after it. */
function newestRowId(rows: readonly HistoryRow[]): string | null | undefined {
  // Nothing loaded yet: no anchor, history matches by text from the newest.
  if (rows.length === 0) return undefined
  for (let i = rows.length - 1; i >= 0; i--) {
    const id = rowIdOf(rows[i]!)
    if (id) return id
  }
  return undefined
}

/**
 * The history row of a live turn's user message. With an anchor, only rows
 * after it count and the oldest such row wins (two sends of the same text
 * match in order); without one, the newest row with that text.
 */
function matchUserRow(
  rows: readonly HistoryRow[],
  used: ReadonlySet<number>,
  text: string,
  anchor: string | null | undefined,
): number {
  const isMatch = (i: number) =>
    !used.has(i) && rows[i]!.role === 'user' && userText(rows[i]!) === text
  let start = -1
  if (anchor === null) start = 0
  else if (anchor !== undefined) {
    const at = rows.findIndex((r) => rowIdOf(r) === anchor)
    if (at >= 0) start = at + 1
  }
  if (start >= 0) {
    for (let i = start; i < rows.length; i++) if (isMatch(i)) return i
    return -1
  }
  for (let i = rows.length - 1; i >= 0; i--) if (isMatch(i)) return i
  return -1
}

/** Only local notes (slash-command output): history never holds them. */
function isNoteTurn(turn: Turn): boolean {
  return !turn.userMessage && turn.items.length > 0 && turn.items.every((i) => i.kind === 'note')
}

/**
 * Drop live turns history now holds; keep a still-running one (history hides
 * a turn in progress) and let it supersede history's copy of its user row.
 * A turn that failed live while history kept no error row for it leaves its
 * reason behind in `failures`, keyed by its user row.
 */
function reconcile(
  liveTurns: Turn[],
  rows: readonly HistoryRow[],
  failures: Record<string, string>,
): { liveTurns: Turn[]; failures: Record<string, string> } {
  const used = new Set<number>()
  const kept: Turn[] = []
  let nextFailures = failures
  for (const turn of liveTurns) {
    if (!turn.userMessage) {
      if (turn.status === 'live' || isNoteTurn(turn)) kept.push(turn)
      continue
    }
    if (turn.userMessage.delivery === 'not_sent') {
      kept.push(turn)
      continue
    }
    const match = matchUserRow(rows, used, turn.userMessage.text, turn.userMessage.anchor)
    if (match < 0) {
      kept.push(turn)
      continue
    }
    used.add(match)
    const after = rows.slice(match + 1)
    const answered = after.some((r) => r.role === 'assistant' || isErrorRow(r))
    if (turn.status === 'live' && !answered) {
      kept.push({ ...turn, historyRowId: rowIdOf(rows[match]!) || undefined })
      continue
    }
    if (turn.status === 'failed' && turn.failure) {
      const nextUser = after.findIndex((r) => r.role === 'user')
      const own = nextUser < 0 ? after : after.slice(0, nextUser)
      const id = rowIdOf(rows[match]!)
      if (id && !own.some(isErrorRow) && nextFailures[id] !== turn.failure) {
        nextFailures = { ...nextFailures, [id]: turn.failure }
      }
    }
  }
  return { liveTurns: kept, failures: nextFailures }
}

/**
 * A history turn as shown: a failure seen live is restored, and a tool call
 * that never got a result in a finished turn ends as failed instead of
 * spinning forever.
 */
function settleHistoryTurn(turn: Turn, failure: string | undefined): Turn {
  let out = turn
  if (failure && out.status === 'done') out = { ...out, status: 'failed', failure }
  if (out.items.some((i) => i.kind === 'toolCall' && i.state === 'running')) {
    out = {
      ...out,
      items:
        out.status === 'aborted'
          ? cancelRunningTools(out.items)
          : out.items.map((i) =>
              i.kind === 'toolCall' && i.state === 'running'
                ? { ...i, state: 'failed' as const, isError: true }
                : i,
            ),
    }
  }
  return out
}

// The history fold only changes with its rows, summaries and the failures map;
// a live frame reuses it (and its turn objects, so memoized turns skip render).
const foldCache = new WeakMap<
  readonly HistoryRow[],
  {
    summaries: readonly CompactionSummaryRow[]
    failures: Record<string, string>
    turns: Turn[]
  }
>()

function historyTurns(state: TranscriptState): Turn[] {
  const hit = foldCache.get(state.rows)
  if (hit && hit.summaries === state.summaries && hit.failures === state.failures) return hit.turns
  const turns = foldHistory(state.rows, state.summaries).map((turn) =>
    settleHistoryTurn(turn, turn.userMessage ? state.failures[turn.userMessage.id] : undefined),
  )
  foldCache.set(state.rows, { summaries: state.summaries, failures: state.failures, turns })
  return turns
}

function deriveTurns(state: TranscriptState): Turn[] {
  const history = historyTurns(state)
  // A turn still running live supersedes history's partial copy of it.
  const superseded = new Set(
    state.liveTurns
      .filter((t) => t.status === 'live' && t.historyRowId)
      .map((t) => `h:${t.historyRowId}`),
  )
  const shown = superseded.size ? history.filter((t) => !superseded.has(t.id)) : history
  const live = state.liveTurns.filter((t) => !isNoteTurn(t))
  const merged = [...shown, ...live]
  const notes = state.liveTurns.filter(isNoteTurn)
  if (notes.length === 0) return merged
  // Notes sit where they were written among the turns around them.
  for (const note of notes) {
    const at = note.startedAt ?? Number.POSITIVE_INFINITY
    const index = merged.findIndex((t) => (t.startedAt ?? Number.NEGATIVE_INFINITY) > at)
    if (index < 0) merged.push(note)
    else merged.splice(index, 0, note)
  }
  return merged
}

/**
 * The gateway says nothing runs (`in_flight_run: null`). Every live turn the
 * view knew of before the question was asked is over, so its terminal frame
 * was missed: end it (a turn from elsewhere goes; history holds it). A send
 * still in flight, or a run learned of after the question, is left alone.
 */
function endStaleLive(liveTurns: Turn[], issuedAt: number): { turns: Turn[]; changed: boolean } {
  let changed = false
  const turns: Turn[] = []
  for (const turn of liveTurns) {
    const known = turn.since ?? turn.startedAt ?? 0
    if (turn.status !== 'live' || turn.userMessage?.delivery === 'sending' || known > issuedAt) {
      turns.push(turn)
      continue
    }
    changed = true
    if (!turn.userMessage) continue
    const at = Number.isFinite(issuedAt) ? issuedAt : (turn.startedAt ?? 0)
    turns.push(endTurn(turn, 'done', at))
  }
  return { turns, changed }
}

/** The items a gateway snapshot of a running turn describes. */
function snapshotItems(base: Item[], run: InFlightRun, mint: Mint): Item[] {
  // Keep what the snapshot does not carry (reasoning, routing, artifacts);
  // its text and tool calls replace the view's.
  let items: Item[] = base.filter((i) => i.kind !== 'text')
  for (const event of Array.isArray(run.events) ? run.events : []) {
    const kind = str(event.kind)
    const toolUseId = str(event.tool_use_id)
    if (!toolUseId) continue
    if (kind === 'tool_use_start') {
      items = startTool(items, toolUseId, str(event.tool_name), undefined, undefined, mint)
    } else if (kind === 'tool_result') {
      const known = items.find((i) => i.kind === 'toolCall' && i.toolUseId === toolUseId)
      if (known && known.kind === 'toolCall' && known.state !== 'running') continue
      items = settleTool(
        items,
        { tool_use_id: toolUseId, tool_name: event.tool_name, is_error: event.is_error },
        undefined,
        mint,
      )
    }
  }
  const plan = typeof run.plan === 'string' ? run.plan.trim() : ''
  if (plan && !items.some((i) => i.kind === 'plan')) {
    items = insertContent(items, { kind: 'plan', id: mint(), plan, toolUseId: 'in-flight' })
  }
  return appendText(items, str(run.text), mint)
}

/**
 * Show a running turn the gateway reports: adopt it when the view has none,
 * or rebuild the view's live turn when the snapshot covers frames it missed.
 * Replayed frames the snapshot already holds are dropped (`streamFloor`).
 */
function applySnapshot(
  state: TranscriptState,
  liveTurns: Turn[],
  rows: readonly HistoryRow[],
  run: InFlightRun,
  issuedAt: number,
): { liveTurns: Turn[]; streamFloor: number } {
  const mint = mintFor(state)
  const through = num(run.through_stream_seq)
  const floor = through === undefined ? state.streamFloor : Math.max(state.streamFloor, through)
  let index = liveTurns.length - 1
  while (index >= 0 && liveTurns[index]!.status !== 'live') index--
  if (index < 0) {
    const lastUser = [...rows].reverse().find((r) => r.role === 'user')
    const historyRowId = lastUser ? rowIdOf(lastUser) || undefined : undefined
    const turn: Turn = {
      id: mint(),
      userMessage: lastUser
        ? {
            id: historyRowId ?? '',
            text: userText(lastUser),
            timestamp: toMs(lastUser.timestamp),
            attachments: Array.isArray(lastUser.attachments) ? lastUser.attachments : [],
          }
        : null,
      items: snapshotItems([], run, mint),
      status: 'live',
      startedAt: toMs(run.started_at),
      endedAt: null,
      since: Number.isFinite(issuedAt) ? issuedAt : 0,
      lastSeq: through,
      historyRowId,
      textTruncated: run.text_truncated === true || undefined,
      runId: runIdOf(run as Record<string, unknown>) || undefined,
    }
    return { liveTurns: [...liveTurns, turn], streamFloor: floor }
  }
  let live = liveTurns[index]!
  const snapshotRun = runIdOf(run as Record<string, unknown>)
  if (snapshotRun && !live.runId && live.userMessage?.delivery !== 'sending') {
    // The run the view's live turn belongs to.
    live = { ...live, runId: snapshotRun }
    liveTurns = liveTurns.map((t, i) => (i === index ? live : t))
  }
  const hasContent = Boolean(str(run.text)) || Boolean(run.events?.length) || run.plan != null
  if (through === undefined || (live.lastSeq ?? -1) >= through || !hasContent) {
    return { liveTurns, streamFloor: state.streamFloor }
  }
  const rebuilt: Turn = {
    ...live,
    items: snapshotItems(live.items, run, mint),
    lastSeq: through,
    textTruncated: run.text_truncated === true || undefined,
  }
  return { liveTurns: liveTurns.map((t, i) => (i === index ? rebuilt : t)), streamFloor: floor }
}

function hasRunState(page: HistoryPage): boolean {
  return Object.prototype.hasOwnProperty.call(page, 'in_flight_run')
}

/**
 * A snapshot of a run this view already saw end. The gateway settles a run a
 * moment after its terminal frame, so a reload on that frame can still name
 * it: such a snapshot says nothing (neither "running" nor "idle").
 */
function isEndedSnapshot(state: TranscriptState, run: InFlightRun | null | undefined): boolean {
  const id = runIdOf(run as Record<string, unknown> | null | undefined)
  return Boolean(run && id && state.endedRuns.includes(id))
}

/**
 * The rows of *current* above the first row of *page*: older history the
 * page no longer covers. Empty when the page does not overlap the window (it
 * skipped past it), so the caller falls back to a plain replace.
 */
function rowsAbove(current: HistoryRow[], page: HistoryRow[]): HistoryRow[] {
  const first = page[0]
  if (!first || !current.length) return []
  const identity = messagePageIdentity(first as unknown as ChatMessage)
  if (!identity) return []
  const at = current.findIndex(
    (row) => messagePageIdentity(row as unknown as ChatMessage) === identity,
  )
  return at > 0 ? current.slice(0, at) : []
}

function applyHistory(
  state: TranscriptState,
  page: HistoryPage,
  mode: HistoryMode,
  issuedAt: number,
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

  let summaries =
    mode === 'replace' || mode === 'refresh'
      ? (page.compaction_summaries ?? [])
      : [...state.summaries, ...(page.compaction_summaries ?? [])]
  const kept = mode === 'refresh' ? rowsAbove(state.rows, messages) : []
  if (kept.length) {
    // The reader loaded older pages: they stay, and so does the paging
    // position, so a turn ending does not pull the history out from under
    // them (or shift what they are reading by the rows that left the window).
    rows = [...kept, ...messages]
    hasMore = state.hasMore
    oldestCursor = state.oldestCursor
    const known = new Set(summaries.map((s) => JSON.stringify(s)))
    summaries = [...state.summaries.filter((s) => !known.has(JSON.stringify(s))), ...summaries]
  }

  const next: TranscriptState = { ...state }
  let liveTurns = state.liveTurns
  let failures = state.failures
  let activeGroups = state.activeGroups
  let pendingInputs = state.pendingInputs
  let streamFloor = state.streamFloor
  const runState =
    mode !== 'older' && hasRunState(page) && !isEndedSnapshot(state, page.in_flight_run)
  if (runState && !page.in_flight_run) {
    liveTurns = endStaleLive(liveTurns, issuedAt).turns
    activeGroups = []
  }
  if (mode !== 'older') {
    const reconciled = reconcile(liveTurns, rows, failures)
    liveTurns = reconciled.liveTurns
    failures = reconciled.failures
  }
  // Opened mid-run, or the view missed frames: show the run as reported.
  if (runState && page.in_flight_run) {
    const adopted = applySnapshot(next, liveTurns, rows, page.in_flight_run, issuedAt)
    liveTurns = adopted.liveTurns
    streamFloor = adopted.streamFloor
  }
  if (mode !== 'older' && Array.isArray(page.pending_inputs)) {
    pendingInputs = page.pending_inputs.filter((p) => !p.message_id)
  }

  const out: TranscriptState = {
    ...next,
    rows,
    summaries,
    hasMore,
    oldestCursor,
    newestCursor,
    liveTurns,
    failures,
    activeGroups,
    pendingInputs,
    streamFloor,
    needsResync:
      mode === 'replace' || mode === 'refresh' ? false : needsResync || state.needsResync,
  }
  return { ...out, turns: deriveTurns(out) }
}

/** `sessions.messages.subscribe` reported the run state. */
function applyRunState(
  state: TranscriptState,
  inFlight: InFlightRun | null,
  issuedAt: number,
): TranscriptState {
  if (isEndedSnapshot(state, inFlight)) return state
  if (!inFlight) {
    const ended = endStaleLive(state.liveTurns, issuedAt)
    if (!ended.changed && state.activeGroups.length === 0) return state
    // Reload so the turns that ended unseen show what history kept of them.
    const out: TranscriptState = {
      ...state,
      liveTurns: ended.turns,
      activeGroups: [],
      needsResync: state.needsResync || ended.changed,
    }
    return { ...out, turns: deriveTurns(out) }
  }
  const next: TranscriptState = { ...state }
  const adopted = applySnapshot(next, state.liveTurns, state.rows, inFlight, issuedAt)
  if (adopted.liveTurns === state.liveTurns && adopted.streamFloor === state.streamFloor) {
    return state
  }
  const out: TranscriptState = {
    ...next,
    liveTurns: adopted.liveTurns,
    streamFloor: adopted.streamFloor,
  }
  return { ...out, turns: deriveTurns(out) }
}

// ── the reducer ─────────────────────────────────────────────────────────────

export function reduce(state: TranscriptState, input: TranscriptInput): TranscriptState {
  switch (input.type) {
    case 'reset':
      return initialTranscript()
    case 'historyPage':
      return applyHistory(state, input.page, input.mode, input.issuedAt ?? Number.POSITIVE_INFINITY)
    case 'runState':
      return applyRunState(state, input.inFlight, input.issuedAt)
    case 'streamReset':
      return state.streamFloor === 0 ? state : { ...state, streamFloor: 0 }
    case 'event': {
      const next = applyEvent(state, input.name, input.payload, input.at)
      return next === state ? state : { ...next, turns: deriveTurns(next) }
    }
    case 'events': {
      let next = state
      for (const e of input.events) next = applyEvent(next, e.name, e.payload, e.at)
      return next === state ? state : { ...next, turns: deriveTurns(next) }
    }
    case 'optimisticSend': {
      const next: TranscriptState = { ...state }
      const mint = mintFor(next)
      // A previous live turn that never got its terminal frame is over now.
      // (Sends while a turn runs go through the composer's queue instead.)
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
          anchor: newestRowId(state.rows),
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
          return {
            ...t,
            userMessage,
            since: input.at ?? t.startedAt ?? 0,
            runId: input.runId || t.runId,
          }
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
    case 'abortRequested':
    case 'abortFailed': {
      const live = currentLive(state)
      if (!live) return state
      const requested = input.type === 'abortRequested'
      let turn: Turn
      if (input.force) {
        if (!live.abortRequested || Boolean(live.forceStop) === requested) return state
        turn = { ...live, forceStop: requested || undefined }
      } else {
        if (Boolean(live.abortRequested) === requested) return state
        turn = requested
          ? { ...live, abortRequested: true }
          : { ...live, abortRequested: undefined, forceStop: undefined }
      }
      const out = { ...state, liveTurns: replaceLast(state, turn) }
      return { ...out, turns: deriveTurns(out) }
    }
    case 'systemNote': {
      const next: TranscriptState = { ...state }
      const mint = mintFor(next)
      const turn: Turn = {
        id: mint(),
        userMessage: null,
        items: [{ kind: 'note', id: mint(), text: input.text }],
        status: 'done',
        startedAt: input.at,
        endedAt: input.at,
      }
      const out = { ...next, liveTurns: [...state.liveTurns, turn] }
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
