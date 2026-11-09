import type { MessageKey } from '@/i18n'
import '@/i18n/en/webPalette'
import { t } from '@/i18n'
import { blockedBecause, gate, requireConnection, type Gate } from '@/lib/disabled-reason'
import type { RpcState } from '@/lib/ws-rpc'
import type { ThemePreference } from '@/stores/theme'

// The palette's Actions group (command-palette spec "Palette actions"). Pure:
// the palette passes in what it knows and the side effects to run, so every
// action and its disabled reason are testable without rendering.

export interface PaletteAction {
  id: string
  labelKey: MessageKey
  keywords: string[]
  gate: Gate
  run(): void
}

export interface ActionContext {
  connection: RpcState
  /** The session open in chat, if any. */
  sessionKey: string | null
  sessionRunning: boolean
  newSession(): void
  toggleSidebar(): void
  toggleDetails(): void
  setTheme(preference: ThemePreference): void
  openShortcuts(): void
  retryConnection(): void
  openInbox(): void
  compact(key: string): void
  exportSession(key: string): void
}

const OPEN = gate()

export function buildActions(ctx: ActionContext): PaletteAction[] {
  const noSession = ctx.sessionKey ? null : blockedBecause(t('webPalette.reasonNoSession'))
  const sessionGate = gate(requireConnection(ctx.connection), noSession)
  const compactGate = gate(
    requireConnection(ctx.connection),
    noSession,
    ctx.sessionRunning ? blockedBecause(t('webPalette.reasonTurnRunning')) : null,
  )
  const key = ctx.sessionKey
  return [
    {
      id: 'new-session',
      labelKey: 'webPalette.actionNewSession',
      keywords: ['create', 'chat'],
      gate: OPEN,
      run: ctx.newSession,
    },
    {
      id: 'toggle-sidebar',
      labelKey: 'webPalette.actionToggleSidebar',
      keywords: ['list', 'pane', 'collapse'],
      gate: OPEN,
      run: ctx.toggleSidebar,
    },
    {
      id: 'toggle-details',
      labelKey: 'webPalette.actionToggleDetails',
      keywords: ['panel', 'info'],
      gate: OPEN,
      run: ctx.toggleDetails,
    },
    {
      id: 'theme-system',
      labelKey: 'webPalette.actionThemeSystem',
      keywords: ['theme', 'appearance', 'auto'],
      gate: OPEN,
      run: () => ctx.setTheme('system'),
    },
    {
      id: 'theme-light',
      labelKey: 'webPalette.actionThemeLight',
      keywords: ['theme', 'appearance'],
      gate: OPEN,
      run: () => ctx.setTheme('light'),
    },
    {
      id: 'theme-dark',
      labelKey: 'webPalette.actionThemeDark',
      keywords: ['theme', 'appearance'],
      gate: OPEN,
      run: () => ctx.setTheme('dark'),
    },
    {
      id: 'shortcuts',
      labelKey: 'webPalette.actionShortcuts',
      keywords: ['keyboard', 'keys', 'help'],
      gate: OPEN,
      run: ctx.openShortcuts,
    },
    {
      id: 'retry-connection',
      labelKey: 'webPalette.actionRetryConnection',
      keywords: ['reconnect', 'gateway', 'offline'],
      gate: OPEN,
      run: ctx.retryConnection,
    },
    {
      id: 'inbox',
      labelKey: 'webPalette.actionInbox',
      keywords: ['approvals', 'attention', 'questions'],
      gate: OPEN,
      run: ctx.openInbox,
    },
    {
      id: 'compact',
      labelKey: 'webPalette.actionCompact',
      keywords: ['context', 'summarize'],
      gate: compactGate,
      run: () => key && ctx.compact(key),
    },
    {
      id: 'export',
      labelKey: 'webPalette.actionExport',
      keywords: ['markdown', 'download'],
      gate: sessionGate,
      run: () => key && ctx.exportSession(key),
    },
  ]
}
