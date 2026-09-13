import { jobs } from './en/jobs'
import { notifications } from './en/notifications'
import { projects } from './en/projects'
import { sessions } from './en/sessions'
import { settings } from './en/settings'
import { setup } from './en/setup'
import { shell } from './en/shell'
import { skills } from './en/skills'
import { theme } from './en/theme'

const en = {
  ...shell,
  ...theme,
  ...jobs,
  ...skills,
  ...projects,
  ...sessions,
  ...settings,
  ...setup,
  ...notifications,
} as const

export type MessageKey = keyof typeof en

/**
 * Minimal catalog lookup. Single locale for now; the shape matches the web
 * console's i18n so extra locales can slot in without touching call sites.
 * Always call inside a component or function, never at module scope.
 */
export function t(key: MessageKey): string {
  return en[key]
}
