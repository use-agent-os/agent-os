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
  /** The user asked to stop this turn. */
  abortRequested?: boolean
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
  in_flight_run?: InFlightRun | null
}

export interface InFlightRun {
  run_id?: string
  status?: string
  started_at?: string | number
  text?: string
}

export type TranscriptInput =
  | { type: 'historyPage'; page: HistoryPage; mode: 'replace' | 'older' | 'delta' }
  | { type: 'event'; name: string; payload: Record<string, unknown>; at: number }
  | {
      type: 'optimisticSend'
      text: string
      attachments?: unknown[]
      idempotencyKey: string
      at: number
    }
  | { type: 'sendAccepted'; idempotencyKey: string }
  | { type: 'sendFailed'; idempotencyKey: string; error: string; notSent: boolean }
  | { type: 'queue'; count: number }
  | { type: 'abortRequested' }
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
}
