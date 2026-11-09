import { defineNamespace } from '../registry'

export const overview = defineNamespace('overview', {
  documentTitle: 'Overview - AgentOS Control',
  eyebrow: 'Control · Overview',
  title: 'Overview',
  subtitle: 'Live status, recent sessions, and the gateway event stream.',
  refresh: 'Refresh',
  openChat: 'Open chat',
  toastStatusFailed: 'Failed to load status: {message}',

  // Gateway summary tiles.
  summaryLandmark: 'Gateway summary',
  summaryEyebrow: 'System pulse',
  summaryCadence: 'Refreshes every 30s',
  tileHealth: 'Health',
  tileHealthOpen: 'open health',
  tileHealthDetails: 'view details',
  tileTokens: 'Total tokens',
  tileTokensHint: 'view usage',
  tileSpent: '{amount} spent',
  tileSessions: 'Total sessions',
  tileSessionsHint: 'view all',
  tileProvider: 'Provider',
  tileProviderHint: 'manage agents',
  tileUptime: 'Uptime',
  tileVersion: 'v{version}',

  // Readiness status labels.
  readyReady: 'Ready',
  readyDegraded: 'Degraded',
  readyActionRequired: 'Action required',
  readyUnavailable: 'Unavailable',
  readyUnknown: 'Unknown',

  // Session status labels.
  sessionRunning: 'Running',
  sessionDone: 'Completed',
  sessionFailed: 'Failed',
  sessionKilled: 'Aborted by operator',
  sessionTimeout: 'Timed out',
  sessionUnknown: 'Unknown',

  // Formatting.
  uptime: '{hours}h {minutes}m {seconds}s',
  relJustNow: 'just now',
  relMinutes: '{count}m ago',
  relHours: '{count}h ago',
  relDays: '{count}d ago',

  // Recent sessions panel.
  recentTitle: 'Recent sessions',
  recentSubtitle: 'Resume the latest agent work',
  recentViewAllLabel: 'View all sessions',
  recentViewAll: 'View all →',
  recentUnavailable: 'Recent sessions unavailable.',
  recentEmpty: 'No sessions yet — open chat to start your first one.',
  recentMessages: '{count} msg',
  recentOpenSession: 'Open session {key}',

  // Event stream panel.
  eventsTitle: 'Event stream',
  eventsSubtitle: 'Live gateway activity',
  eventsCount_one: '{count} event',
  eventsCount_other: '{count} events',
  eventsEmpty: 'Listening for events…',

  // Gateway connection panel.
  connTitle: 'Gateway connection',
  connSubtitle: 'Override the active endpoint for this browser',
  connUrlLabel: 'WebSocket URL',
  connUrlPlaceholder: 'ws://…',
  connTokenLabel: 'Token',
  connTokenOptional: 'optional',
  connConnect: 'Connect',
  connDisconnect: 'Disconnect',
} as const)
