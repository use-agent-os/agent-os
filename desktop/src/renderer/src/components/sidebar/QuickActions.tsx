import { CalendarClock, PenSquare, Sparkles, type LucideIcon } from 'lucide-react'
import { Link } from 'react-router'
import { t, type MessageKey } from '~/i18n'
import { useUi } from '~/stores/ui'

interface Action {
  label: MessageKey
  icon: LucideIcon
  shortcut?: readonly string[]
  /** Navigate here (never shown as the selected place). */
  to?: string
  /** Or open a panel over the window instead of navigating. */
  panel?: 'jobs' | 'skills'
}

export const QUICK_ACTIONS: readonly Action[] = [
  { to: '/sessions', label: 'sidebar.new', icon: PenSquare, shortcut: ['⌘', 'N'] },
  { panel: 'skills', label: 'sidebar.skills', icon: Sparkles },
  { panel: 'jobs', label: 'sidebar.jobs', icon: CalendarClock },
]

/** Fixed destinations above the session list. */
export function QuickActions() {
  const jobsOpen = useUi((s) => s.jobsOpen)
  const skillsOpen = useUi((s) => s.skillsOpen)
  const openJobs = useUi((s) => s.openJobs)
  const openSkills = useUi((s) => s.openSkills)
  const panels = {
    jobs: { open: jobsOpen, show: openJobs },
    skills: { open: skillsOpen, show: openSkills },
  } as const

  return (
    <nav aria-label={t('shell.brand')} className="flex flex-col gap-px px-2">
      {QUICK_ACTIONS.map(({ to, panel, label, icon: Icon, shortcut }) => {
        const body = (
          <>
            <Icon
              className="size-4 shrink-0 text-muted-foreground"
              strokeWidth={1.75}
              aria-hidden
            />
            <span className="flex-1 truncate">{t(label)}</span>
            {shortcut ? (
              <span className="flex gap-0.5" aria-hidden>
                {shortcut.map((k) => (
                  <kbd key={k} className="kbd">
                    {k}
                  </kbd>
                ))}
              </span>
            ) : null}
          </>
        )
        if (panel) {
          return (
            <button
              key={label}
              type="button"
              className="mac-row w-full app-no-drag"
              aria-haspopup="dialog"
              aria-expanded={panels[panel].open}
              onClick={panels[panel].show}
            >
              {body}
            </button>
          )
        }
        return (
          <Link key={label} to={to ?? '/'} className="mac-row">
            {body}
          </Link>
        )
      })}
    </nav>
  )
}
