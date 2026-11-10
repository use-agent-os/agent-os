import { defineNamespace } from '../registry'

export const overview = defineNamespace('overview', {
  toastStatusFailed: 'Failed to load status: {message}',

  // Gateway summary tiles.
  tileSpent: '{amount} spent',
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

  // Event stream panel.
  eventsCount_one: '{count} event',
  eventsCount_other: '{count} events',
} as const)
