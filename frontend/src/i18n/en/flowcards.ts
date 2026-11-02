import { defineNamespace } from '../registry'

/**
 * Image Flow cards in the chat transcript (views/chat/transcript/flowRun.ts,
 * flowChange.ts, docs/flows.md). Its own namespace so the copy travels in the
 * cards' lazy chunk, not in the chat page's; the placeholder's loading line
 * stays in `chat`.
 */
export const flowcards = defineNamespace('flowcards', {
  // Run cards (flow_run).
  runUnavailable: 'Flow run data is unavailable.',
  runUnreadable: 'Flow run data could not be read.',
  runFailed: 'Flow run card failed to load.',
  eyebrow: 'Image Flow',
  runStatusQueued: 'Queued',
  runStatusRunning: 'Running',
  runStatusDone: 'Done',
  runStatusFailed: 'Failed',
  runStatusCancelled: 'Cancelled',
  runStatusUnknown: 'Unknown',
  runProgress: '{done}/{total} nodes',
  runCached: '{count} cached',
  runCost: 'cost {cost}',
  runEstimate: 'est. {cost}',
  runImage: 'Image {n}',
  runMore: '+{count} more',
  runNoImages: 'No images.',
  open: 'Open in Flows',
  // Change cards (flow_create / flow_update).
  changeCreated: 'Created',
  changeUpdated: 'Updated',
  changeNodes: '{count} nodes',
  changePerRun: '{cost} per run',
  changeFree: 'free to run',
  changeCostUnknown: 'cost per run unknown',
  changeRev: 'rev {rev}',
} as const)
