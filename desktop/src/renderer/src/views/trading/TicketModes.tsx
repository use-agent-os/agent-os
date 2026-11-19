import { t } from '~/i18n'

export type TicketMode = 'swap' | 'bridge'

const MODES: readonly TicketMode[] = ['swap', 'bridge']

/**
 * The full desk's ticket head: Swap | Bridge, where the BOOK has two tabs.
 * It sits where the ticket's title was and wears the same micro label, so
 * the head keeps its height and the chain control keeps its place.
 */
export function TicketModes({
  mode,
  onMode,
}: {
  mode: TicketMode
  onMode: (mode: TicketMode) => void
}) {
  return (
    <div role="radiogroup" aria-label={t('trading.ticket.modes')} className="trd-ticket__modes">
      {MODES.map((m) => (
        <button
          key={m}
          type="button"
          role="radio"
          aria-checked={mode === m}
          className="app-no-drag"
          data-testid={`ticket-mode-${m}`}
          onClick={() => {
            if (m !== mode) onMode(m)
          }}
        >
          {t(`trading.${m}.title`)}
        </button>
      ))}
    </div>
  )
}
