import { defineNamespace } from '../registry'

export const health = defineNamespace('health', {
  // Readiness labels.
  statusReadyWithWarnings: 'Ready with warnings',
  statusReady: 'Ready',
  statusActionRequired: 'Action required',
  statusDegraded: 'Degraded',
  statusUnavailable: 'Unavailable',

  // Readiness rail.
  railCountShare: '{percent}%',

  // Finding card.
  findingFallbackTitle: 'Finding {index}',

  // Fix steps.
  stepInspectRemote: 'Inspect remote gateway',
  stepRepairRemote: 'Repair remote deployment',
  stepRepairRemoteDetail:
    'Start or repair the remote AgentOS gateway deployment, then refresh health.',
  stepRunDoctor: 'Run local doctor',
  stepRunDoctorDetail: 'Checks local config and onboarding before restarting the gateway.',
  stepStartGateway: 'Start local gateway',
  stepInspectGateway: 'Inspect local gateway',
} as const)
