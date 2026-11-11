import { NavLink } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { cn } from '@/lib/utils'
import { PAGES } from './pages'

export function PagesList({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label={t('webShell.pagesHeading')} className="flex min-h-0 flex-1 flex-col px-2 pt-3">
      <h2 className="px-1 pb-2 text-small font-semibold tracking-wide text-text-muted uppercase">
        {t('webShell.pagesHeading')}
      </h2>
      <ul role="list" className="flex flex-col gap-0.5">
        {PAGES.map((page) => (
          <li key={page.id}>
            <NavLink
              to={page.path}
              viewTransition
              onClick={onNavigate}
              className={({ isActive }) =>
                cn(
                  'target flex items-start gap-3 rounded-md px-2 py-2',
                  isActive ? 'bg-surface-3' : 'hover:bg-surface-2',
                )
              }
            >
              <page.icon className="mt-0.5 size-4 shrink-0 text-text-muted" aria-hidden />
              <span className="min-w-0">
                <span className="block font-medium">{t(page.titleKey)}</span>
                <span className="block text-small text-text-muted">{t(page.descriptionKey)}</span>
              </span>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
