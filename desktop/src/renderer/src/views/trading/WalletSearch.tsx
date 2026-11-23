import { Search, X } from 'lucide-react'
import { t } from '~/i18n'
import { cn } from '~/lib/utils'

/**
 * The wallet list grows a search field once it is long enough to hunt
 * through. Below this many wallets every one of them is already in view.
 */
export const WALLET_SEARCH_MIN = 6

/**
 * One field for finding a wallet by label or address, in the rail and in the
 * head's switcher alike, so both lists answer the same query the same way.
 * Return picks the first match; Escape empties the field (inside a menu the
 * menu takes Escape first and closes, which empties it too).
 */
export function WalletSearch({
  value,
  onChange,
  onSubmit,
  menuFocus,
  className,
}: {
  value: string
  onChange: (value: string) => void
  /** Return: take the first wallet that matches. */
  onSubmit: () => void
  /** Inside a menu: the field, not the first row, takes focus when it opens. */
  menuFocus?: boolean
  className?: string
}) {
  return (
    <label className={cn('trd-picker__search trd-wallet-search', className)}>
      <Search className="size-3.5 text-dim" strokeWidth={2} aria-hidden />
      <input
        type="text"
        role="searchbox"
        value={value}
        placeholder={t('trading.rail.search')}
        aria-label={t('trading.rail.search')}
        spellCheck={false}
        autoComplete="off"
        data-menu-autofocus={menuFocus ? '' : undefined}
        data-testid="wallet-search"
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            onSubmit()
          } else if (e.key === 'Escape' && value) {
            e.preventDefault()
            e.stopPropagation()
            onChange('')
          }
        }}
      />
      {value ? (
        <button
          type="button"
          className="trd-wallet-search__clear app-no-drag"
          aria-label={t('trading.rail.search.clear')}
          title={t('trading.rail.search.clear')}
          onClick={() => onChange('')}
        >
          <X className="size-3" strokeWidth={2} aria-hidden />
        </button>
      ) : null}
    </label>
  )
}
