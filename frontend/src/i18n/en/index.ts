import { agents } from './agents'
import { approvals } from './approvals'
import { channels } from './channels'
import { chat } from './chat'
import { common } from './common'
import { config } from './config'
import { cron } from './cron'
import { env } from './env'
import { flowcards } from './flowcards'
import { health } from './health'
import { inbox } from './inbox'
import { logs } from './logs'
import { mcp } from './mcp'
import { memory } from './memory'
import { overview } from './overview'
import { projects } from './projects'
import { settings } from './settings'
import { sessions } from './sessions'
import { setup } from './setup'
import { shell } from './shell'
import { skills } from './skills'
import { usage } from './usage'
import { webChat } from './webChat'
import { webCommon } from './webCommon'
import { webConnection } from './webConnection'
import { webPagesOverview } from './webPagesOverview'
import { webPagesWork } from './webPagesWork'
import { webPalette } from './webPalette'
import { webSetAccess } from './webSetAccess'
import { webSetCapabilities } from './webSetCapabilities'
import { webSetConfig } from './webSetConfig'
import { webSetSystem } from './webSetSystem'
import { webSettings } from './webSettings'
import { webShell } from './webShell'

/**
 * Every English namespace in one object.
 *
 * This is the *type* source of truth — `MessageKey` is derived from it, so
 * adding a namespace here is what makes its keys callable from `t()` — and the
 * shape tests read it at runtime.
 *
 * It is NOT how the app loads copy. Importing this module pulls every
 * namespace into whichever chunk does so, which for anything reachable from
 * the entry graph is exactly the growth #261 fixed. Runtime code must import
 * the one namespace it needs (`@/i18n/en/logs`) and let `t()` read the
 * registry; `i18n/chunking.test.ts` enforces that. `src/test/setup.ts` imports
 * this module once so unit tests see the complete catalog, standing in for the
 * views that register their own namespace in the browser.
 */
export const en = {
  agents,
  approvals,
  channels,
  chat,
  common,
  config,
  cron,
  env,
  flowcards,
  health,
  inbox,
  logs,
  mcp,
  memory,
  overview,
  projects,
  sessions,
  settings,
  setup,
  shell,
  skills,
  usage,
  webChat,
  webCommon,
  webConnection,
  webPagesOverview,
  webPagesWork,
  webPalette,
  webSetAccess,
  webSetCapabilities,
  webSetConfig,
  webSetSystem,
  webSettings,
  webShell,
} as const
