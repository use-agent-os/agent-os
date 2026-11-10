import { defineNamespace } from '../registry'

export const skills = defineNamespace('skills', {
  title: 'Skills',
  refresh: 'Refresh',

  // Brand artwork. Brand *names* (Bankr / Capminal / Robinhood) are proper
  // nouns and stay literal in the component.
  altGmgnLogo: 'GMGN logo',
  altAgentosLogo: 'AgentOS logo',

  // Install button.
  install: 'Install',

  // Catalog panels.
  communityTitle: 'Community catalog',
  communityDesc: 'Discover skills published by the wider AgentOS community.',
  registryLabelCommunity: 'community',
  registrySearchPlaceholder: 'Search {label} skills…',
  // The partner count renders in its own <strong>, so the noun is a plain key
  // rather than a tPlural() pair (which must carry {count} in the value).
  skillWord: 'skill',
  skillsWord: 'skills',

  // Partner catalog intros.
  bankrTitle: 'Bankr skill catalog',
  bankrDesc: 'Curated financial and on-chain capabilities maintained by the Bankr ecosystem.',
  bankrNotice: "the 'bankr' skill is required for all skills in this catalog.",
  capminalTitle: 'Capminal skill catalog',
  capminalDesc:
    'Wallet, token-launch, and on-chain execution skills maintained by the Capminal team.',
  capminalNotice: "the 'capminal' skill is required for all skills in this catalog.",
  aeonTitle: 'Aeon skill catalog',
  aeonDesc:
    'On-chain investigation, token research, and trend-tracking skills maintained by the Aeon team.',
  aeonNotice:
    "Aeon skills are written for Aeon's scheduled runtime; AgentOS adapts them on install. Some need an API key you supply yourself — each card lists what it wants.",
  robinhoodTitle: 'Robinhood skills',
  robinhoodDesc:
    'Official bundled capabilities for Robinhood products and on-chain assets, maintained by AgentOS against the Robinhood Trading MCP.',
  robinhoodNotice:
    "the 'robinhood-agentic-trading' skill trades from a dedicated Robinhood Agentic account — create that account in Robinhood and complete its authorization flow before using the skill.",

  // Requirements section.
  reqReady: 'ready',
  reqNeedsSetup: 'needs setup',
  reqMissingSkill: 'missing skill',
  reqNotDeclared: 'no deps declared',
  reqMissingLead: 'Missing',
  reqOneOf: 'one of {list}',
  reqEnv: '{name} env',
  reqNoDeps: 'No declared dependencies',
  reqUnknown: 'unknown',

  // Skill detail dialog.
  update: 'Update',

  // Registry detail dialog.
  descPending: "Description loads after install (from the skill's SKILL.md).",

  // Toasts.
  toastLoadFailed: 'Failed to load skills: {message}',
  toastInstalled: 'Installed {name}',
  toastScanBlocked: 'Security scan flagged {target}. Click again to install anyway.',
  toastScanTarget_one: '{name} ({count} finding)',
  toastScanTarget_other: '{name} ({count} findings)',
  toastScanUnnamed: 'this skill',
  toastInstallFailed: 'Install failed',
  toastRemoved: 'Removed {name}',
  toastUninstallFailed: 'Uninstall failed',
  toastUpdated: 'Updated {name}',
  toastUpdateFailed: 'Update failed',
  toastDepsInstalled: 'Installed',
  toastEnvSaved: '{name} saved.',

  // Loading layers (logic.ts).
  layerWorkspace: 'Workspace',
  layerBundled: 'Bundled',
  layerManaged: 'Managed',
  layerPersonal: 'Personal',
  layerProject: 'Project',
  layerExtra: 'Extra',
  layerUnknown: 'Unknown',
  layerHelpWorkspace: 'Workspace skills are local to the active workspace.',
  layerHelpBundled: 'Bundled skills ship with AgentOS.',
  layerHelpManaged: 'Managed skills are locally installed into AgentOS state.',
  layerHelpPersonal: 'Personal skills are local user installs, not bundled.',
  layerHelpProject: 'Project skills are local to the current project.',
  layerHelpExtra: 'Extra skills come from configured local directories.',
  layerHelpFallback: 'Configured local skill directory.',

  // Acquisition provenance (logic.ts).
  srcHub: 'hub',
  srcShipped: 'shipped',
  srcLocal: 'local',

  // Catalog categories (logic.ts).
  catAll: 'All',
  catTrading: 'Trading',
  catDefi: 'DeFi',
  catWallet: 'Wallets',
  catMarkets: 'Markets',
  catSocial: 'Social',
  catData: 'Data',
  catNft: 'NFT',
  catDev: 'Dev tools',
  catInfra: 'Infra',
  catCrypto: 'Crypto',
  catOther: 'Other',

  // Installed empty states (logic.ts).
  emptyMatch: 'No skills match {query}.',
  emptyReady: 'No skills are ready. Install dependencies to enable them.',
  emptyNeedsSetup: 'No skills currently need setup.',
  emptyDisabled: 'No skills are switched off.',
  emptyNone: 'No skills installed.',

  // Provenance groups (logic.ts).
  groupPartners: 'Partner Skills',
  groupCrypto: 'AgentOS Crypto Skills',
  groupShipped: 'AgentOS Normal Skills',
  groupHub: 'Installed from a hub',
  groupLocal: 'Your local skills',
  groupHelpPartners: 'Skills published by an AgentOS partner.',
  groupHelpCrypto: 'On-chain and wallet skills that ship with AgentOS.',
  groupHelpShipped: 'Skills that ship with AgentOS.',
  groupHelpHub: 'Skills you installed from a skill hub.',
  groupHelpLocal: 'Skills you added yourself, from a local skill directory.',

  // Status buckets + dot tooltip (logic.ts).
  bucketReady: 'Ready',
  bucketNeedsSetup: 'Setup required',
  bucketDisabled: 'Disabled',
  dotDisabled: 'Disabled in config',
  dotReady: 'Ready',
  dotNeedsSetup: 'Needs setup',

  // Availability (logic.ts).
  availOffered: 'Offered to the agent',
  availNotOffered: 'Not offered to the agent',
  availModelInvocationDisabled: 'Not offered — agent cannot invoke',
  availIneligible: 'Not offered — needs setup',
  availToolGate: 'Not offered — missing tools',
  availFallbackSuperseded: 'Not offered — superseded',
  availNotRetrieved: 'Not offered — not retrieved',
  availPromptBudget: 'Not offered — prompt too long',

  // Partner empty states (logic.ts).
  partnerEmptyMatch: 'No {brand} skills match {query}.',
  partnerEmptyReady: 'No {brand} skills are ready.',
  partnerEmptyNeedsSetup: 'No {brand} skills currently need setup.',
  partnerEmptyDisabled: 'No {brand} skills are switched off.',
  partnerEmptyNone: '{brand} skills are on the way. No {brand} skills are installed yet.',

  // Catalog empty states (logic.ts).
  registryEmptyMatch: 'No skills match {query}.',
  registryEmptyBankr: 'No Bankr skills available right now.',
  registryEmptyCapminal: 'No Capminal skills available right now.',
  registryEmptyAeon: 'No Aeon skills available right now.',
  registryEmptyCommunity: 'No community skills available right now.',
} as const)
