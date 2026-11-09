import { defineNamespace } from '../registry'

/** Shared copy of the rebuilt web console (overhaul-control-ui). */
export const webCommon = defineNamespace('webCommon', {
  close: 'Close',
  cancel: 'Cancel',
  retry: 'Retry',
  loading: 'Loading…',
  errorTitle: 'Something went wrong',
  stale: 'Snapshot',
  staleTitle: 'Showing saved data while the gateway is offline',
  staleSince: 'Snapshot from {time}',
} as const)
