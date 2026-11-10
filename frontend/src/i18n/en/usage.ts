import { defineNamespace } from '../registry'

export const usage = defineNamespace('usage', {
  // Range picker.
  hiddenSessions_one: '{count} undated legacy session hidden',
  hiddenSessions_other: '{count} undated legacy sessions hidden',

  // Overview.
  totalTokens: 'Total tokens',

  // Cost-source badges (logic.ts).
  sourceEphemeral: 'Ephemeral',
  sourceActual: 'Actual',
  sourceEstimated: 'Estimated',
  sourceMixed: 'Mixed',
  sourceUnpriced: 'Unpriced',
  sourceNone: 'None',
  tipEphemeral: 'Ephemeral session — cost not yet persisted',
  tipActual: 'Actual — cost billed by the provider',
  tipProrated: 'Total is real billed; per-model split is estimated.',
  tipEstimated: 'Estimated — derived locally from token counts',
  tipMixed: 'Mixed — partial billing data, rest estimated',
  tipUnpriced: 'Unpriced — no pricing table entry for this model',
  tipNone: 'No cost recorded',

  // Composition hint. Stored lowercase rather than lowercasing the badge label
  // at runtime — `toLowerCase()` on a translated noun is wrong in languages
  // that capitalise them.
  compositionActual: 'actual',
  compositionEstimated: 'estimated',
  compositionMixed: 'mixed',
  compositionUnpriced: 'unpriced',
  compositionEphemeral: 'ephemeral',
  compositionEntry: '{label} {count}',

  // Relative timestamps (logic.ts).
  relJustNow: 'just now',
  relMinutes: '{count}m ago',
  relHours: '{count}h ago',
  relDays: '{count}d ago',
} as const)
