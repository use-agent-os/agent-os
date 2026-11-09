import { defineNamespace } from '../registry'

export const health = defineNamespace('health', {
  documentTitle: 'Health - AgentOS Control',
  eyebrow: 'Control · Health',
  title: 'Health',
  refreshTitle: 'Refresh health report',
  refresh: 'Refresh',
  checking: 'Checking',

  // Readiness labels.
  statusReadyWithWarnings: 'Ready with warnings',
  statusReady: 'Ready',
  statusActionRequired: 'Action required',
  statusDegraded: 'Degraded',
  statusUnavailable: 'Unavailable',

  // Header summary line.
  summaryChecking: 'Checking readiness',
  summaryUnavailable: 'Health report unavailable',
  summaryLoaded: 'Health report loaded',

  // Readiness rail.
  railLandmark: 'Health summary',
  railReadiness: 'System readiness',
  railWaiting: 'Waiting for doctor.status',
  railImpactHead: 'Impact distribution',
  railImpactChecks: '{count} checks',
  railImpactRunning: 'Running checks',
  railImpactMeter: 'Impact distribution: {breakdown}',
  railCountValue: '{label}: {value}',
  railCountChecking: 'checking',
  railCountShare: '{percent}%',

  // Count tiles / finding groups.
  countBlocksReady: 'Needs action',
  countDegrades: 'Degraded',
  countOptional: 'Optional',
  countNone: 'Ready',

  // Impact labels on a finding meta line.
  impactBlocksReady: 'Blocks readiness',
  impactDegrades: 'Degrades',
  impactOptional: 'Optional',
  impactNone: 'Reference',

  // Report context row.
  contextLandmark: 'Health report context',
  contextGateway: 'Gateway',
  contextConfig: 'Config',
  contextRequestedConfig: 'Requested config',
  contextAgent: 'Agent',

  // Finding groups.
  groupActionTitle: 'Needs action',
  groupActionNote: 'Fix these first to make AgentOS ready.',
  groupDegradedTitle: 'Degraded capabilities',
  groupDegradedNote: 'AgentOS can run, but these capabilities need attention.',
  groupOptionalTitle: 'Optional setup',
  groupOptionalNote: 'These improve capability or posture but do not block readiness.',
  groupReadyTitle: 'Ready checks',
  groupReadyNote: 'These surfaces are already working.',

  // Findings section.
  findingsEyebrow: 'Diagnostics',
  findingsTitle: 'What needs attention',
  findingsIntro: 'Ordered by readiness impact so the next useful action stays obvious.',
  findingsEmpty: 'No findings returned.',
  findingsLoading: 'Loading health report',

  // Finding card.
  findingFallbackTitle: 'Finding {index}',
  findingRestart: 'Recovery requires restart',
  findingEvidence: 'Finding evidence',
  badgeDiagnosticsIncomplete: 'Diagnostics incomplete',
  badgeRepairPending: 'Repair pending',
  badgeConfigMismatch: 'Config mismatch',

  // Fix steps.
  stepsOptional: 'Optional setup steps',
  stepsReference: 'Reference steps',
  stepsRecovery: 'Recovery steps',
  stepFallbackLabel: 'Step',
  stepInspectRemote: 'Inspect remote gateway',
  stepRepairRemote: 'Repair remote deployment',
  stepRepairRemoteDetail:
    'Start or repair the remote AgentOS gateway deployment, then refresh health.',
  stepRunDoctor: 'Run local doctor',
  stepRunDoctorDetail: 'Checks local config and onboarding before restarting the gateway.',
  stepStartGateway: 'Start local gateway',
  stepInspectGateway: 'Inspect local gateway',

  // Synthetic gateway.unavailable finding.
  gatewayUnavailableTitle: 'Gateway health report unavailable',
  gatewayUnavailableDetail: 'Cannot load doctor.status from {url}. {reason}',
} as const)
