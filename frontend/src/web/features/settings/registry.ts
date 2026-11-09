import type { MessageKey } from '@/i18n'

// The Settings section registry (overhaul-control-ui D9). Metadata only —
// catalog keys, never resolved copy — so the shell, the palette and settings
// search can list sections without loading any section's code.

export type SettingsGroup = 'general' | 'connections' | 'agents' | 'security' | 'system'

export interface SettingsSectionMeta {
  id: string
  group: SettingsGroup
  titleKey: MessageKey
  descriptionKey: MessageKey
  /** Extra search terms (English, matched case-insensitively). */
  keywords: readonly string[]
}

export const SETTINGS_GROUPS: readonly { id: SettingsGroup; titleKey: MessageKey }[] = [
  { id: 'general', titleKey: 'webSettings.groupGeneral' },
  { id: 'connections', titleKey: 'webSettings.groupConnections' },
  { id: 'agents', titleKey: 'webSettings.groupAgents' },
  { id: 'security', titleKey: 'webSettings.groupSecurity' },
  { id: 'system', titleKey: 'webSettings.groupSystem' },
]

function section(
  id: string,
  group: SettingsGroup,
  keywords: readonly string[] = [],
): SettingsSectionMeta {
  return {
    id,
    group,
    titleKey: `webSettings.${id}` as MessageKey,
    descriptionKey: `webSettings.${id}Description` as MessageKey,
    keywords,
  }
}

/** Every sidebar section, in sidebar order. */
export const SETTINGS_SECTIONS: readonly SettingsSectionMeta[] = [
  section('appearance', 'general', [
    'theme',
    'dark',
    'light',
    'text size',
    'font',
    'send',
    'enter',
  ]),
  section('notifications', 'general', ['push', 'web push', 'notify']),
  section('gateway', 'connections', ['token', 'url', 'websocket', 'restart', 'reconnect']),
  section('channels', 'connections', ['telegram', 'discord', 'slack', 'pairing', 'adapter']),
  section('agents', 'agents', ['agent', 'persona', 'prompt']),
  section('models', 'agents', [
    'provider',
    'model',
    'router',
    'pilot',
    'tier',
    'openrouter',
    'login',
  ]),
  section('search', 'agents', ['web search', 'x search', 'brave']),
  section('skills', 'agents', ['skill', 'install', 'hub', 'partner']),
  section('mcp', 'agents', ['server', 'oauth', 'tools']),
  section('memory', 'agents', ['knowledge', 'embedding', 'reindex']),
  section('media', 'agents', ['image', 'audio', 'voice']),
  section('approvals', 'security', ['elevated', 'bypass', 'strategy']),
  section('environment', 'security', ['env', 'variable', 'secret', 'api key']),
  section('health', 'system', ['doctor', 'readiness', 'findings']),
  section('logs', 'system', ['log', 'tail', 'diagnostics']),
  section('advanced', 'system', ['config', 'yaml', 'schema']),
  section('debug', 'system', ['rpc', 'events', 'status']),
  section('updates', 'system', ['upgrade', 'version']),
  section('about', 'system', ['license', 'version']),
]

/** Reachable by URL (and search) but not listed in the sidebar groups. */
export const SETUP_SECTION: SettingsSectionMeta = section('setup', 'general', [
  'onboarding',
  'wizard',
  'first run',
])

export const ALL_SETTINGS_SECTIONS: readonly SettingsSectionMeta[] = [
  ...SETTINGS_SECTIONS,
  SETUP_SECTION,
]

export function findSection(id: string | undefined): SettingsSectionMeta | null {
  return ALL_SETTINGS_SECTIONS.find((s) => s.id === id) ?? null
}
