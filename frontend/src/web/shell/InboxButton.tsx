import { Bell } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webShell'
import { useAttentionInbox } from '@/lib/useAttentionInbox'
import { SimpleTooltip } from '@/web/components/ui/tooltip'
import { RailLink } from './RailLink'

/**
 * Inbox with the attention badge (shell spec "Inbox attention badge"). The
 * badge sits in an absolutely positioned slot, so appearing or growing never
 * moves a rail button; an increase is announced politely.
 */
export function InboxButton({ onNavigate }: { onNavigate?: () => void }) {
  const { count } = useAttentionInbox()
  const previous = useRef(count)
  const [announcement, setAnnouncement] = useState('')

  useEffect(() => {
    if (count > previous.current) {
      setAnnouncement(tPlural('webShell.attentionAnnounce', count))
    }
    previous.current = count
  }, [count])

  const label = count > 0 ? t('webShell.railInboxCount', { count }) : t('webShell.railInbox')
  return (
    <>
      <SimpleTooltip label={t('webShell.railInbox')} side="right">
        <RailLink to="/inbox" onClick={onNavigate} aria-label={label}>
          <Bell aria-hidden />
          <span
            data-testid="inbox-badge"
            aria-hidden
            // A template string, not cn(): tailwind-merge would drop text-small
            // as a colour clashing with text-danger-fg.
            className={`inbox-badge pointer-events-none absolute top-0 right-0 h-4 min-w-4 rounded-full bg-danger px-1 text-center text-small leading-4 font-semibold text-danger-fg tabular-nums ring-2 ring-surface-1 ${count > 0 ? 'visible' : 'invisible'}`}
          >
            {count > 99 ? '99+' : count}
          </span>
        </RailLink>
      </SimpleTooltip>
      <span role="status" aria-live="polite" className="sr-only">
        {announcement}
      </span>
    </>
  )
}
