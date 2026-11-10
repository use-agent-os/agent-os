import { defineNamespace } from '../registry'

export const channels = defineNamespace('channels', {
  // Derived copy (logic.ts).
  nameUnknown: 'Unknown',
  inactiveNone: 'no inactive channels',
  inactiveDisabled: '{count} disabled',
  inactiveIdle: 'configured but idle',
  hintDisabled:
    'Disabled in config — gateway restart required after re-enabling. Run `agentos onboard configure channels` to change.',
  hintDead: 'Adapter is dead. Inspect gateway logs, then `agentos channels restart {name}`.',
  hintRunning: 'Adapter is live in the current gateway process.',
  hintRestarting: 'Adapter is restarting after dispatch errors.',
  hintExhausted: 'Adapter exhausted its retry budget. Try `agentos channels restart {name}`.',
  hintConfigured:
    'Configured on disk but not active in this gateway process — restart the gateway to load it.',
  senderFallback: 'Telegram user {id}',
  senderUnknownId: 'unknown',
  senderMetaId: 'ID {id}',
  senderMetaExpires: 'expires {time}',
} as const)
