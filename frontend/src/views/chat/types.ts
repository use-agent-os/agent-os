// Chat view — shared types. Foundation for the chat-view migration (Task 1);
// extended by later tasks. Kept intentionally small: a chat message, the
// message role, and the raw stream-event payload shape the gateway emits.

export type Role = 'user' | 'assistant' | 'system'

/**
 * A single transcript message. `text` is the rendered/plain body; `timestamp`
 * is an optional epoch-ms; `transcriptId` is the stable transcript row id when
 * known (legacy carries it as `transcript_id` on the raw payload — see
 * `messageTranscriptId` in logic.ts).
 */
export interface ChatMessage {
  role: Role
  text: string
  timestamp?: number
  transcriptId?: string | null
}

/**
 * The raw payload carried by a streamed chat event. The gateway sends an
 * open-ended object; `seq` and `session_key` are the fields the client keys on
 * (sequence ordering + session routing). Later tasks narrow specific event
 * variants on top of this.
 */
export interface StreamEventPayload {
  seq?: number
  session_key?: string
  [k: string]: unknown
}
