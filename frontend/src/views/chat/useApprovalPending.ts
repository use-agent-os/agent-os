import { useEffect } from 'react'
import { useApprovals, type Approval } from '@/services/approval-monitor'

/**
 * Chat-view approval-pending gate .
 *
 * Legacy chat.js listened for a window `agentos:approvals-pending` CustomEvent
 * that the approval monitor dispatched with the current pending list, then
 * called `_setStreamIdlePausedForApproval(hasPendingForCurrentSession)`
 *. In the React port the monitor no longer dispatches that
 * event — it publishes the pending list to the shared `useApprovals` zustand
 * store (services/approval-monitor.ts, which explicitly retired the CustomEvent).
 * So this hook subscribes to that SAME store (no divergent approval state) and
 * feeds the same `hasPendingForCurrentSession` boolean into the controller's
 * `setStreamIdlePausedForApproval` — the verbatim port of
 * `_setStreamIdlePausedForApproval` (stream.ts:525-542):
 *
 * pending for THIS session → pause the stream-idle timer + flip run-status
 * to `approval_pending` ("Waiting for approval",
 * active_task {status:'approval_pending', terminal_reason:'tool_approval'}).
 * cleared (resolved in the approvals view → monitor re-polls → store updates
 * with the item gone) → resume: run-status back to `running` + restart the
 * idle timer (only while a stream is live) — i.e. resolving advances the
 * stream,.
 *
 * The approve/deny/bypass BUTTONS are NOT a chat-view surface — they live in the
 * migrated approvals view (frontend/src/views/approvals/ApprovalsPage.tsx →
 * ApprovalCard → approvalMonitor.resolve). The chat thread never rendered an
 * inline approve/deny card (confirmed against chat.js); its only approval
 * affordance is this idle-pause + run-status reflection.
 */
export function computeHasPendingForSession(pending: Approval[], sessionKey: string): boolean {
  // a pending item matches when its sessionKey (snake or
  // camel alias) equals the current session key — or, for a prompt a
  // subagent or coding harness raised, when the current session is one of
  // its ancestors (`audienceSessionKeys`): the parent's turn is waiting on it.
  return pending.some((item) => {
    const own = item.sessionKey || (item as { session_key?: string }).session_key || ''
    if (own === sessionKey) return true
    if (!sessionKey) return false
    const audience =
      item.audienceSessionKeys ||
      (item as { audience_session_keys?: string[] }).audience_session_keys ||
      []
    return Array.isArray(audience) && audience.includes(sessionKey)
  })
}

export function useApprovalPending(
  sessionKey: string,
  setStreamIdlePausedForApproval: (paused: boolean) => void,
): void {
  // Subscribe to the pending list from the shared approvals store.
  const pending = useApprovals((s) => s.pending)

  useEffect(() => {
    setStreamIdlePausedForApproval(computeHasPendingForSession(pending, sessionKey))
    // Re-run whenever the pending set OR the active session changes (a session
    // switch re-evaluates "is there a pending approval for the NEW session").
  }, [pending, sessionKey, setStreamIdlePausedForApproval])
}
