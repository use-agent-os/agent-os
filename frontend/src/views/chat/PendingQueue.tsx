import { MAX_PENDING, type PendingItem } from './logic'
import { t } from '@/i18n'
import '@/i18n/en/chat'

/**
 * The pending-queue rail.
 *
 * A queued-send backlog shown above the composer while a turn is streaming or a
 * compaction is in flight. Sends enqueue here and drain FIFO
 * after the terminal event; the user can recover them (ESC / Alt+↑) or drop them
 * (per-chip × / Clear all). This is a PRESENTATIONAL component: the queue array
 * and the mutation callbacks are owned by ChatPage's `usePendingQueue` (the
 * React equivalent of the legacy `_pendingQueue` module-global + the delegated
 * `_onPendingAreaClick` handler ).
 *
 * Terminal styling: one severity rail (never stacked gutters); lime stays
 * signal-only, so the label/chips render in the neutral mono treatment and only
 * the count carries emphasis. Queue chips use the shared control radius.
 */
export interface PendingQueueProps {
  /** The queued sends, oldest→newest (legacy `_pendingQueue`). */
  queue: PendingItem[]
  /** Remove the item at `idx`. */
  onRemove: (idx: number) => void
  /** Clear the whole queue. */
  onClearAll: () => void
}

export function PendingQueue({ queue, onRemove, onClearAll }: PendingQueueProps) {
  // hidden (nothing rendered) when the queue is empty.
  if (queue.length === 0) return null

  // the Clear-all affordance appears only at 2+ queued items.
  const showClearAll = queue.length >= 2

  return (
    <div className="chat-pending" role="region" aria-label={t('chat.pendingLandmark')}>
      <div className="chat-pending-header">
        <span className="chat-pending-label" title={t('chat.pendingLabelTitle')}>
          {t('chat.pendingCount', { count: queue.length, max: MAX_PENDING })}
        </span>
        {showClearAll && (
          <button
            type="button"
            className="chat-pending-clear"
            aria-label={t('chat.pendingClearAllAria')}
            onClick={onClearAll}
          >
            {t('chat.pendingClearAll')}
          </button>
        )}
      </div>
      <div className="chat-pending-chips">
        {queue.map((p, i) => {
          // 30-char preview (ellipsis past 30), attachment
          // count chip, and the full text as the chip title / aria label.
          const raw = p.text || (p.attachments.length ? t('chat.pendingAttachmentOnly') : '')
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
                title={t('chat.pendingRemove')}
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
