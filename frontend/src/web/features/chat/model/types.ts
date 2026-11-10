// The headless transcript model (overhaul-control-ui D7). One pure reducer
// folds history pages and live session events into the same typed turns, so a
// live turn and the same turn restored from history render alike.
import type { Artifact } from '@/views/chat/transcript/artifacts'

export type TurnStatus = 'live' | 'done' | 'failed' | 'aborted'
export type ToolState = 'running' | 'done' | 'failed'

export interface UserMessage {
  /** History entry id once known; a local id while optimistic. */
  id: string
  text: string
  timestamp: number | null
  attachments: unknown[]
  /** Set while the send is in flight, or when it was not delivered. */
  delivery?: 'sending' | 'not_sent'
  idempotencyKey?: string
  /**
   * The newest history row the view held when this message was sent (null:
   * none). History matches the message only to a user row after it, so an
   * earlier message with the same text is never taken for this one.
   */
  anchor?: string | null
}

export interface TurnUsage {
  model?: string
  inputTokens?: number
  outputTokens?: number
  costUsd?: number
}

interface ItemBase {
  id: string
}

export type Item =
  | (ItemBase & { kind: 'text'; text: string })
  | (ItemBase & {
      kind: 'thinkingRef'
      /** The history entry whose reasoning `chat.thinking` returns. */
      messageId: string | null
      /** Reasoning streamed live (history loads it on expand instead). */
      text?: string
    })
  | (ItemBase & {
      kind: 'toolCall'
      toolUseId: string
      name: string
      args?: unknown
      result?: string
      isError: boolean
      truncated: boolean
      state: ToolState
      startedAt?: number
      endedAt?: number
    })
  | (ItemBase & { kind: 'artifact'; artifact: Artifact })
  | (ItemBase & { kind: 'card'; category: string; artifact: Artifact })
  | (ItemBase & { kind: 'plan'; plan: string; toolUseId: string })
  | (ItemBase & { kind: 'ask'; toolUseId: string; payload: unknown })
  | (ItemBase & { kind: 'acp'; toolUseId: string; args: unknown; result: unknown })
  | (ItemBase & { kind: 'compaction'; summary: string; removedCount?: number })
  | (ItemBase & { kind: 'routerDecision'; tier: string | null; model: string })
  | (ItemBase & { kind: 'steering'; text: string })
  | (ItemBase & { kind: 'subagentCompletion'; payload: Record<string, unknown> })
  | (ItemBase & { kind: 'cronResult'; text: string })
  | (ItemBase & { kind: 'warning'; text: string })
  | (ItemBase & { kind: 'error'; text: string })
  /** Local output of a slash command (`/model`): never sent, never in history. */
  | (ItemBase & { kind: 'note'; text: string })

export type ItemKind = Item['kind']

export interface Turn {
  id: string
  /** Null for standalone entries: compaction dividers, automation results. */
  userMessage: UserMessage | null
  items: Item[]
  status: TurnStatus
  /** The failure reason for a failed turn. */
  failure?: string
  usage?: TurnUsage
  startedAt: number | null
  endedAt: number | null
  /** The user asked to stop this turn (the gateway is winding it down). */
  abortRequested?: boolean
  /** The user asked again while stopping: stop at once, no grace period. */
  forceStop?: boolean
  /** Live: when the view learned the run is accepted or running (ms). */
  since?: number
  /** Live: the highest `stream_seq` folded into this turn. */
  lastSeq?: number
  /** Live: the history row of this turn's user message, once matched. */
  historyRowId?: string
  /** Live: the gateway's snapshot dropped earlier streamed text. */
  textTruncated?: boolean
  /** Live: the gateway's run (task) id, once known. */
  runId?: string
}

/** A row of a `chat.history` page (only the fields the model reads). */
export interface HistoryRow {
  id?: string | null
  message_id?: string | null
  role: string
  text: string
  timestamp?: string | number | null
  provenance_kind?: string | null
  provenance_source_tool?: string | null
  has_thinking?: boolean
  attachments?: unknown[]
  artifacts?: Artifact[]
  usage?: Record<string, unknown>
  model?: string
  input_tokens?: number
  output_tokens?: number
  cost_usd?: number
  tool_calls?: Array<Record<string, unknown>>
  placeholder?: boolean
  entry_id?: string
  [key: string]: unknown
}

export interface CompactionSummaryRow {
  id?: string
  compaction_id?: string
  summary_text?: string
  removed_count?: number
  created_at?: string | number
  covered_through_id?: string
}

export interface HistoryPage {
  messages?: HistoryRow[]
  has_more?: boolean
  oldest_cursor?: string | null
  newest_cursor?: string | null
  compaction_summaries?: CompactionSummaryRow[]
  kind?: string
  /** Absent on older gateways; `null` means nothing runs (authoritative). */
  in_flight_run?: InFlightRun | null
  pending_inputs?: PendingInput[]
}

export interface InFlightRun {
  run_id?: string
  task_id?: string
  status?: string
  started_at?: string | number
  /** The snapshot covers the session's stream up to this `stream_seq`. */
  through_stream_seq?: number
  text?: string
  /** Earlier text did not fit the snapshot. */
  text_truncated?: boolean
  plan?: unknown
  /** Compact progress records: kind, tool_use_id, tool_name, is_error, … */
  events?: Array<Record<string, unknown>>
}

/** A message queued (or waiting to be steered) on the gateway. */
export interface PendingInput {
  task_id?: string
  text?: string
  text_truncated?: boolean
  queued_at?: number
  attachments?: number
  steer?: boolean
  /** Set when the transcript already lists the message. */
  message_id?: string
}

export type TranscriptInput =
  | {
      type: 'historyPage'
      page: HistoryPage
      mode: 'replace' | 'older' | 'delta'
      /** When the request went out (ms); a run accepted after it is not judged by it. */
      issuedAt?: number
    }
  | { type: 'event'; name: string; payload: Record<string, unknown>; at: number }
  /** Several frames folded at once (coalesced per animation frame). */
  | { type: 'events'; events: { name: string; payload: Record<string, unknown>; at: number }[] }
  /** The run state a `sessions.messages.subscribe` answer reports. */
  | { type: 'runState'; inFlight: InFlightRun | null; issuedAt: number }
  /** The session's stream started over: low `stream_seq` values are new again. */
  | { type: 'streamReset' }
  | {
      type: 'optimisticSend'
      text: string
      attachments?: unknown[]
      idempotencyKey: string
      at: number
    }
  | { type: 'sendAccepted'; idempotencyKey: string; at?: number; runId?: string }
  | { type: 'sendFailed'; idempotencyKey: string; error: string; notSent: boolean }
  | { type: 'queue'; count: number }
  /** Stop asked; `force` for the second ask while the first is pending. */
  | { type: 'abortRequested'; force?: boolean }
  /** `chat.abort` failed: Stop (or Stop now, for `force`) is offered again. */
  | { type: 'abortFailed'; force?: boolean }
  | { type: 'systemNote'; text: string; at: number }
  | { type: 'reset' }

export interface TranscriptState {
  /** Merged history rows, oldest first. */
  rows: HistoryRow[]
  summaries: CompactionSummaryRow[]
  hasMore: boolean
  oldestCursor: string | null
  newestCursor: string | null
  /** Turns built from live events that history has not caught up with yet. */
  liveTurns: Turn[]
  /** Derived: history turns followed by live ones. */
  turns: Turn[]
  epoch: number
  /** Subagent task groups still working for this session. */
  activeGroups: string[]
  /** The view must reload its history window (gap, epoch change, unusable delta). */
  needsResync: boolean
  queued: number
  /** Transient notices (warnings) — never part of a turn. */
  notices: { id: string; text: string }[]
  /** Monotonic counter for local ids. */
  seq: number
  /** Frames at or below this `stream_seq` are covered by an adopted snapshot. */
  streamFloor: number
  /**
   * Failure reasons seen live, by the history row of the turn's user
   * message, for turns history holds without an error row.
   */
  failures: Record<string, string>
  /** Messages queued on the gateway that the transcript does not list yet. */
  pendingInputs: PendingInput[]
  /**
   * Runs whose terminal frame this view saw (newest last, bounded). A
   * terminal frame is final: a later snapshot naming one of them is stale.
   */
  endedRuns: string[]
}
