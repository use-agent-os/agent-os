import { Search, X } from 'lucide-react'
import { t } from '~/i18n'
import { useUi } from '~/stores/ui'

export function SearchField() {
  const query = useUi((s) => s.sessionQuery)
  const setQuery = useUi((s) => s.setSessionQuery)
  return (
    <label className="mac-search mx-2">
      <Search className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t('sidebar.search')}
        aria-label={t('sidebar.search')}
        spellCheck={false}
      />
      {query ? (
        <button type="button" aria-label={t('sidebar.search')} onClick={() => setQuery('')}>
          <X className="size-3.5" aria-hidden />
        </button>
      ) : null}
    </label>
  )
}
