import { defineNamespace } from '../registry'

export const sessions = defineNamespace('sessions', {
  // Session lifecycle labels (logic.ts).
  statusRunning: 'Running',
  statusDone: 'Completed',
  statusFailed: 'Failed',
  statusKilled: 'Aborted by operator',
  statusTimeout: 'Timed out',
  statusUnknown: 'Unknown',

  // Relative timestamps (logic.ts).
  relJustNow: 'just now',
  relMinutes: '{count}m ago',
  relHours: '{count}h ago',
  relDays: '{count}d ago',

  // Run badges (logic.ts).
  runQueued: 'Task queued',
  runRunning: 'Task running',
  runInterrupted: 'Interrupted',
  runFailed: 'Last task failed',
  runTimeout: 'Last task timed out',
  runCancelled: 'Last task cancelled',
} as const)
