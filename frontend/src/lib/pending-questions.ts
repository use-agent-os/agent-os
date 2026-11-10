/**
 * Pending operator questions (`ask_user`) for the attention inbox.
 *
 * The gateway has no RPC that lists unanswered questions: `ask_user` ends the
 * turn and the answer is simply the next user message. So the list is built
 * from what this connection sees: a `session.event.tool_result` whose result
 * is a presented question adds an entry for its session, and the next
 * `task.queued` for that session (the answer, or any newer message) clears
 * it. Replayed frames are ignored (they may predate the answer). The gateway
 * only streams `session.event.*` to connections subscribed to
 * a session's messages, which means a question asked in a session no window
 * of this console has open, or before the console connected, is not listed.
 * The chat transcript still shows it when the session is opened.
 *
 * Deleting a session sends no frame that clears its question, so a caller that
 * holds a complete session list prunes with `retainSessions`: a question whose
 * session is missing from a list fetched after the question arrived belongs to
 * a deleted session and can no longer be answered.
 */
import { create } from 'zustand'
import { parseAskQuestions, type AskQuestion } from '@/lib/ask-questions'

export interface PendingQuestion {
  /** Stable: one entry per session; a re-ask in the same session replaces it. */
  id: string
  sessionKey: string
  questions: AskQuestion[]
  toolUseId: string | null
  presentedAt: number
}

interface PendingQuestionsState {
  items: PendingQuestion[]
  present(question: PendingQuestion): void
  clearSession(sessionKey: string): void
  /**
   * Drop questions whose session is not in `live`, a complete session list
   * fetched at `asOf`. Questions presented after `asOf` are kept: the list
   * may predate their session.
   */
  retainSessions(live: ReadonlySet<string>, asOf: number): void
  reset(): void
}

export const usePendingQuestions = create<PendingQuestionsState>((set) => ({
  items: [],
  present: (question) =>
    set((s) => ({
      items: [...s.items.filter((q) => q.sessionKey !== question.sessionKey), question],
    })),
  clearSession: (sessionKey) =>
    set((s) => {
      const items = s.items.filter((q) => q.sessionKey !== sessionKey)
      return items.length === s.items.length ? s : { items }
    }),
  retainSessions: (live, asOf) =>
    set((s) => {
      const items = s.items.filter((q) => q.presentedAt > asOf || live.has(q.sessionKey))
      return items.length === s.items.length ? s : { items }
    }),
  reset: () => set({ items: [] }),
}))

function sessionKeyOf(payload: unknown): string {
  const p = (payload ?? {}) as { key?: unknown; session_key?: unknown; sessionKey?: unknown }
  for (const value of [p.session_key, p.sessionKey, p.key]) {
    if (typeof value === 'string' && value) return value
  }
  return ''
}

function toolUseIdOf(payload: unknown): string | null {
  const p = (payload ?? {}) as { tool_use_id?: unknown; toolUseId?: unknown; id?: unknown }
  for (const value of [p.tool_use_id, p.toolUseId, p.id]) {
    if (typeof value === 'string' && value) return value
  }
  return null
}

export interface QuestionsRpc {
  on(event: string, handler: (...args: unknown[]) => void): () => void
}

/** Feed the store from one client's frames. Returns the disposer. */
export function bindPendingQuestions(
  rpc: QuestionsRpc,
  options: { now?: () => number } = {},
): () => void {
  const now = options.now ?? Date.now
  const store = usePendingQuestions.getState
  const unsubs = [
    rpc.on('session.event.tool_result', (payload, meta) => {
      // A replayed frame (a resubscribe catching up) may be a question that
      // was answered since: only a live frame says "this is waiting now".
      if ((meta as { replayed?: unknown } | undefined)?.replayed === true) return
      const questions = parseAskQuestions(payload)
      const sessionKey = sessionKeyOf(payload)
      if (!questions || !sessionKey) return
      store().present({
        id: `question:${sessionKey}`,
        sessionKey,
        questions,
        toolUseId: toolUseIdOf(payload),
        presentedAt: now(),
      })
    }),
    rpc.on('task.queued', (payload) => {
      const sessionKey = sessionKeyOf(payload)
      if (sessionKey) store().clearSession(sessionKey)
    }),
  ]
  return () => {
    for (const off of unsubs) off()
  }
}
