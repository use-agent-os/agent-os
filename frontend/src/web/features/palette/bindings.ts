import type { MessageKey } from '@/i18n'

// The web console's binding table (command-palette spec "Global shortcuts",
// "Navigation chords"). One source for registration, the shortcut overview
// and docs/web-ui.md.

export type BindingCategory = 'global' | 'navigation' | 'composer'

export interface Binding {
  combo: string
  descriptionKey: MessageKey
  category: BindingCategory
  /** Handled by the element that owns it (the composer); listed only. */
  documentationOnly?: boolean
}

export const GLOBAL_BINDINGS: readonly Binding[] = [
  { combo: 'mod+k', descriptionKey: 'webPalette.bindPalette', category: 'global' },
  { combo: 'mod+b', descriptionKey: 'webPalette.bindSidebar', category: 'global' },
  { combo: 'mod+/', descriptionKey: 'webPalette.bindOverview', category: 'global' },
  {
    combo: '?',
    descriptionKey: 'webPalette.bindOverview',
    category: 'global',
    documentationOnly: true,
  },
  { combo: 'mod+shift+o', descriptionKey: 'webPalette.bindNewSession', category: 'global' },
  {
    combo: 'escape',
    descriptionKey: 'webPalette.bindEscape',
    category: 'global',
    documentationOnly: true,
  },
]

export interface Chord extends Binding {
  path: string
}

function chord(key: string, path: string, descriptionKey: MessageKey): Chord {
  return { combo: `g ${key}`, path, descriptionKey, category: 'navigation' }
}

export const NAVIGATION_CHORDS: readonly Chord[] = [
  chord('c', '/chat', 'webPalette.navChat'),
  chord('h', '/home', 'webPalette.navHome'),
  chord('i', '/inbox', 'webPalette.navInbox'),
  chord('s', '/sessions', 'webPalette.navSessions'),
  chord('p', '/projects', 'webPalette.navProjects'),
  chord('u', '/usage', 'webPalette.navUsage'),
  chord('r', '/automations', 'webPalette.navAutomations'),
  chord(',', '/settings', 'webPalette.navSettings'),
  chord('n', '/settings/channels', 'webPalette.navChannels'),
  chord('m', '/settings/mcp', 'webPalette.navMcp'),
  chord('k', '/settings/skills', 'webPalette.navSkills'),
  chord('a', '/settings/agents', 'webPalette.navAgents'),
  chord('y', '/settings/memory', 'webPalette.navMemory'),
  chord('e', '/settings/environment', 'webPalette.navEnvironment'),
  chord('l', '/settings/logs', 'webPalette.navLogs'),
  chord('v', '/settings/approvals', 'webPalette.navApprovals'),
  chord('o', '/home', 'webPalette.navOverview'),
]

export const COMPOSER_BINDINGS: readonly Binding[] = [
  {
    combo: 'enter',
    descriptionKey: 'webPalette.composerSend',
    category: 'composer',
    documentationOnly: true,
  },
  {
    combo: 'shift+enter',
    descriptionKey: 'webPalette.composerNewline',
    category: 'composer',
    documentationOnly: true,
  },
  {
    combo: '/',
    descriptionKey: 'webPalette.composerSlash',
    category: 'composer',
    documentationOnly: true,
  },
  {
    combo: 'arrowup',
    descriptionKey: 'webPalette.composerHistory',
    category: 'composer',
    documentationOnly: true,
  },
  {
    combo: 'alt+arrowup',
    descriptionKey: 'webPalette.composerRecoverQueued',
    category: 'composer',
    documentationOnly: true,
  },
  {
    combo: 'tab',
    descriptionKey: 'webPalette.composerSuggestion',
    category: 'composer',
    documentationOnly: true,
  },
]

export const ALL_BINDINGS: readonly Binding[] = [
  ...GLOBAL_BINDINGS,
  ...NAVIGATION_CHORDS,
  ...COMPOSER_BINDINGS,
]
