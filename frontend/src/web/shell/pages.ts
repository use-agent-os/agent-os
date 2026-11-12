import { Bell, CalendarClock, ChartColumn, FolderKanban, House, MessagesSquare } from 'lucide-react'
import type { MessageKey } from '@/i18n'
import type { Destination } from '@/web/app/routes'

/** The Pages catalog (shell spec "Pages catalog"). Keys, never resolved copy. */
export interface PageEntry {
  id: Destination
  path: string
  titleKey: MessageKey
  descriptionKey: MessageKey
  icon: typeof House
  /** The Pages pane's group: what the runtime is doing, or the work in it. */
  group: 'overview' | 'work'
}

export const PAGES: readonly PageEntry[] = [
  {
    id: 'home',
    path: '/home',
    titleKey: 'webShell.pageHome',
    descriptionKey: 'webShell.pageHomeDescription',
    icon: House,
    group: 'overview',
  },
  {
    id: 'sessions',
    path: '/sessions',
    titleKey: 'webShell.pageSessions',
    descriptionKey: 'webShell.pageSessionsDescription',
    icon: MessagesSquare,
    group: 'work',
  },
  {
    id: 'projects',
    path: '/projects',
    titleKey: 'webShell.pageProjects',
    descriptionKey: 'webShell.pageProjectsDescription',
    icon: FolderKanban,
    group: 'work',
  },
  {
    id: 'usage',
    path: '/usage',
    titleKey: 'webShell.pageUsage',
    descriptionKey: 'webShell.pageUsageDescription',
    icon: ChartColumn,
    group: 'overview',
  },
  {
    id: 'automations',
    path: '/automations',
    titleKey: 'webShell.pageAutomations',
    descriptionKey: 'webShell.pageAutomationsDescription',
    icon: CalendarClock,
    group: 'work',
  },
  {
    id: 'inbox',
    path: '/inbox',
    titleKey: 'webShell.pageInbox',
    descriptionKey: 'webShell.pageInboxDescription',
    icon: Bell,
    group: 'overview',
  },
]
