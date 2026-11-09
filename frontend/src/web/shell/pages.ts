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
}

export const PAGES: readonly PageEntry[] = [
  {
    id: 'home',
    path: '/home',
    titleKey: 'webShell.pageHome',
    descriptionKey: 'webShell.pageHomeDescription',
    icon: House,
  },
  {
    id: 'sessions',
    path: '/sessions',
    titleKey: 'webShell.pageSessions',
    descriptionKey: 'webShell.pageSessionsDescription',
    icon: MessagesSquare,
  },
  {
    id: 'projects',
    path: '/projects',
    titleKey: 'webShell.pageProjects',
    descriptionKey: 'webShell.pageProjectsDescription',
    icon: FolderKanban,
  },
  {
    id: 'usage',
    path: '/usage',
    titleKey: 'webShell.pageUsage',
    descriptionKey: 'webShell.pageUsageDescription',
    icon: ChartColumn,
  },
  {
    id: 'automations',
    path: '/automations',
    titleKey: 'webShell.pageAutomations',
    descriptionKey: 'webShell.pageAutomationsDescription',
    icon: CalendarClock,
  },
  {
    id: 'inbox',
    path: '/inbox',
    titleKey: 'webShell.pageInbox',
    descriptionKey: 'webShell.pageInboxDescription',
    icon: Bell,
  },
]
