import { defineNamespace } from '../registry'

export const setup = defineNamespace('setup', {
  // Router tiers + readiness vocabulary (logic.ts).
  tierC0: 'Route c0',
  tierC1: 'Route c1',
  tierC2: 'Route c2',
  tierC3: 'Route c3',
  readyOk: 'Ready',
  readyOptional: 'Optional',
  readyMissing: 'Missing',
  readyDegraded: 'Needs action',
  readyUnknown: 'Check',

  // Wizard steps (logic.ts).
  stepProvider: 'Provider',
  stepRouter: 'Router Tiers',
  stepExtras: 'Capabilities',
  stepFinish: 'Finish',

  // Step chips (logic.ts).
  chipReview: 'Review',
  chipNeedsAction: 'Needs action',
  chipReady: 'Ready',
  chipOptional: 'Optional',
  chipProviderFirst: 'Provider first',
  chipChooseProvider: 'Choose provider',
  chipConfigure: 'Configure',

  // Readiness reasons + headline (logic.ts).
  reasonEnvNotVisible: '{key} is not visible',
  reasonConnectProvider: 'Connect a model provider',
  reasonReviewSections: 'Review setup sections for pending actions',
  reasonSectionSetup: '{label} setup needed',
  headlineActionNeeded: 'Action needed',
  headlineOptional: 'Optional improvements',
  headlineOptionalChip_one: 'Optional · {count} item',
  headlineOptionalChip_other: 'Optional · {count} items',
  headlineReady: 'Ready to run',

  // Provider capability blurb (logic.ts).
  routerSupported: 'Pilot Router ready',
  routerDirectOnly: 'Direct only',
  modelPilotDefaults: 'Pilot Router defaults',
  modelNotConfigured: 'not configured',
  routerPilot: 'Pilot Router',

  // Capability status text (logic.ts).
  missingEnvStatus: '{capability} is selected, but ${key} is not visible to the gateway.',
  capSearch: 'Web search',
  searchOff: 'Web search is off until a provider is selected.',
  searchReady: 'Web search is ready for new turns.',
  searchNeedsKey: 'Web search is selected but still needs a visible provider key.',
  capImage: 'Image generation',
  imageDisabled: 'Image generation is hidden from agents until this capability is enabled.',
  imageReadySameKey: 'Image generation will be available in new turns using the same provider key.',
  imageReadyVisibleKey:
    'Image generation will be available in new turns once the gateway has the visible key.',
  imageNeedsKey:
    'Image generation is enabled but still needs a visible provider key before agents can use it.',
  capAudio: 'Voice audio',
  audioDisabled: 'Voice audio tools stay hidden until this capability is enabled.',
  audioReady:
    'Voice audio tools are ready for TTS, transcription, dubbing, cloning, conversion, and music.',
  audioNeedsKey: 'Voice audio is enabled but still needs a visible provider key.',
  capMemory: 'Remote memory embeddings',
  memoryKeywordOnly: 'Keyword search stays available; embeddings are disabled.',
  memoryLocalBge: 'Uses local BGE embeddings; no remote key is needed.',
  memoryOllama: 'Uses your Ollama server; no API key is needed.',
  memoryLocalFirst: 'Local-first memory search; optional remote fallback can be configured.',
  memoryRemoteReady: 'Remote memory embeddings are configured for new turns.',
  memoryNeedsKey: 'Remote memory embeddings need a visible provider key before they can run.',

  // CLI recipes (logic.ts).
  cliSetEnvKey: 'Set environment key',
  cliRestartGateway: 'Restart gateway after env fix',
  cliGuided: 'Guided CLI',
  cliCheckStatus: 'Check status',
  cliProviderOptions: 'Provider options',
  cliRouterTiers: 'Router tiers',
  cliSearchOptions: 'Search options',
  cliChannelOptions: 'Channel options',
  cliImageOptions: 'Image options',
  cliMemoryOptions: 'Memory options',

  // X (Twitter) search card.
  xSearchOauthActive: 'Signed in to xAI — x_search will use that subscription.',
  xSearchOauthIncomplete: 'Signed in to xAI, but the login has no refresh token. Sign in again.',
  // One literal, not a concatenation: the catalog's types read placeholders off
  // the string literal, and `a + b` widens it to `string` so {envKey} is lost.
  xSearchOauthAbsent:
    'Not signed in to xAI. Using {envKey} if set. Sign in below to use a SuperGrok / X Premium+ subscription instead.',
} as const)
