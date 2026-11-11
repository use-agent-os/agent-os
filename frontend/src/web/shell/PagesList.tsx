import { NavLink } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { useGlide } from '@/web/components/ui/use-glide'
import { PAGES } from './pages'

export function PagesList({ onNavigate }: { onNavigate?: () => void }) {
  // One pill glides to the current page (its links sit in plain <li>s, so
  // the list is their offset parent).
  const track = useGlide<HTMLUListElement>()
  return (
    <nav
      aria-label={t('webShell.pagesHeading')}
      className="scroll-contained flex min-h-0 flex-1 flex-col px-2 pt-3 pb-3"
    >
      <h2 className="mb-2 flex h-7 items-center px-2 text-section">{t('webShell.pagesHeading')}</h2>
      <ul
        ref={track}
        role="list"
        className="glide-track flex flex-col gap-0.5 [--glide-bg:var(--surface-3)] [--glide-radius:10px]"
      >
        {PAGES.map((page) => (
          <li key={page.id}>
            <NavLink
              to={page.path}
              viewTransition
              onClick={onNavigate}
              className="group/page press target flex items-center gap-3 rounded-[10px] px-2 py-2 [&:not([aria-current=page]):hover]:bg-surface-2"
            >
              <span
                aria-hidden
                className="grid size-8 shrink-0 place-items-center rounded-lg border border-border bg-surface-2 text-text-muted shadow-highlight group-aria-[current=page]/page:border-accent/30 group-aria-[current=page]/page:bg-accent-tint group-aria-[current=page]/page:text-accent"
              >
                <page.icon className="size-4" />
              </span>
              <span className="min-w-0">
                <span className="block font-medium">{t(page.titleKey)}</span>
                <span className="block truncate text-small text-text-muted">
                  {t(page.descriptionKey)}
                </span>
              </span>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
