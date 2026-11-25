import { acp } from './en/acp'
import { attention } from './en/attention'
import { files } from './en/files'
import { flows } from './en/flows'
import { jobs } from './en/jobs'
import { notifications } from './en/notifications'
import { projects } from './en/projects'
import { quickAsk } from './en/quick-ask'
import { sessions } from './en/sessions'
import { settings } from './en/settings'
import { capabilities } from './en/settings-capabilities'
import { memory } from './en/settings-memory'
import { overview } from './en/settings-overview'
import { setup } from './en/setup'
import { shell } from './en/shell'
import { skills } from './en/skills'
import { theme } from './en/theme'
import { trading } from './en/trading'

const en = {
  ...shell,
  ...theme,
  ...jobs,
  ...skills,
  ...projects,
  ...sessions,
  ...settings,
  ...overview,
  ...memory,
  ...capabilities,
  ...setup,
  ...notifications,
  ...trading,
  ...quickAsk,
  ...acp,
  ...attention,
  ...flows,
  ...files,
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
