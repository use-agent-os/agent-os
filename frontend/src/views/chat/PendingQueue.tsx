import { MAX_PENDING, type PendingItem } from './logic'

/**
 * The pending-queue rail (chat.js:8474-8503 `_renderPendingQueue`).
 *
 * A queued-send backlog shown above the composer while a turn is streaming or a
 * compaction is in flight. Sends enqueue here (chat.js:6091-6110) and drain FIFO
 * after the terminal event; the user can recover them (ESC / Alt+↑) or drop them
 * (per-chip × / Clear all). This is a PRESENTATIONAL component: the queue array
 * and the mutation callbacks are owned by ChatPage's `usePendingQueue` (the
 * React equivalent of the legacy `_pendingQueue` module-global + the delegated
 * `_onPendingAreaClick` handler at chat.js:8455-8472).
 *
 * Terminal styling: one severity rail (never stacked gutters); lime stays
 * signal-only, so the label/chips render in the neutral mono treatment and only
 * the count carries emphasis. Queue chips use the shared control radius.
 */
export interface PendingQueueProps {
  /** The queued sends, oldest→newest (legacy `_pendingQueue`). */
  queue: PendingItem[]
  /** Remove the item at `idx` (chat.js:8459-8463). */
  onRemove: (idx: number) => void
  /** Clear the whole queue (chat.js:8466-8471). */
  onClearAll: () => void
}

// chat.js:8484 — the label title string, verbatim.
const LABEL_TITLE =
  'Alt+↑ pulls the most recent back into the input · ESC recovers all to input · sends FIFO when the current response finishes'

export function PendingQueue({ queue, onRemove, onClearAll }: PendingQueueProps) {
  // chat.js:8476-8480 — hidden (nothing rendered) when the queue is empty.
  if (queue.length === 0) return null

  // chat.js:8482 — the Clear-all affordance appears only at 2+ queued items.
  const showClearAll = queue.length >= 2

  return (
    <div className="chat-pending" role="region" aria-label="Pending messages">
      <div className="chat-pending-header">
        <span className="chat-pending-label" title={LABEL_TITLE}>
          Pending {queue.length}/{MAX_PENDING}
        </span>
        {showClearAll && (
          <button
            type="button"
            className="chat-pending-clear"
            aria-label="Clear all pending messages"
            onClick={onClearAll}
          >
            Clear all
          </button>
        )}
      </div>
      <div className="chat-pending-chips">
        {queue.map((p, i) => {
          // chat.js:8490-8494 — 30-char preview (ellipsis past 30), attachment
          // count chip, and the full text as the chip title / aria label.
          const raw = p.text || (p.attachments.length ? '(attachment only)' : '')
          const preview = raw.slice(0, 30) + (raw.length > 30 ? '…' : '')
          const chipLabel = `Pending message ${i + 1}: ${raw.slice(0, 80)}`
          return (
            <span key={i} className="chat-pending-chip" title={raw}>
              <span className="chat-pending-text">{preview}</span>
              {p.attachments.length > 0 && (
                <span className="chat-pending-attch">📎{p.attachments.length}</span>
              )}
              <button
                type="button"
                className="chat-pending-chip-remove"
                aria-label={`Remove ${chipLabel}`}
                title="Remove"
                onClick={() => onRemove(i)}
              >
                ×
              </button>
            </span>
          )
        })}
      </div>
    </div>
  )
}
