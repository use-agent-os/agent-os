// Pure health-view helpers ported 1:1 from the legacy view
// (src/agentos/gateway/static/js/views/health.js). Each function below carries
// the legacy line range it mirrors so the parity matrix stays auditable.

// Registers this view's copy; it ships in this chunk, not the entry bundle.
import '@/i18n/en/health'
import { t } from '@/i18n'

export type Impact = 'blocks_ready' | 'degrades' | 'optional' | 'none'
export type GroupKind = 'action' | 'degraded' | 'optional' | 'ready'

export interface Finding {
  id?: string
  severity?: string
  readinessImpact?: string
  surface?: string
  title?: string
  detail?: string
  evidence?: Record<string, unknown>
  fixSteps?: Array<{ label?: string; command?: string; detail?: string }>
  restartRequired?: boolean
}

export interface HealthReport {
  status?: string
  ready?: boolean
  summary?: string
  gatewayUrl?: string
  configPath?: string
  requestedConfigPath?: string
  agentId?: string
  counts?: Record<string, number>
  impactCounts?: Partial<Record<Impact, number>>
  findings?: Finding[]
}

const HIDDEN_EVIDENCE_KEYS = new Set(['restart_required', 'restartRequired'])

/** — readinessImpact passthrough else severity mapping. */
export function impactValue(f: Pick<Finding, 'readinessImpact' | 'severity'>): Impact {
  const impact = String(f?.readinessImpact || '')
  if (['blocks_ready', 'degrades', 'optional', 'none'].includes(impact)) return impact as Impact
  const severity = String(f?.severity || 'info')
  if (severity === 'error') return 'blocks_ready'
  if (severity === 'warn') return 'degrades'
  if (severity === 'info') return 'optional'
  return 'none'
}

/** — derive impact counts from severity counts. */
export function impactCountsFromSeverity(counts: Record<string, number>): Record<Impact, number> {
  return {
    blocks_ready: Number(counts.error || 0),
    degrades: Number(counts.warn || 0),
    optional: Number(counts.info || 0),
    none: Number(counts.ok || 0),
  }
}

/** — readiness label incl. "Ready with warnings". */
export function statusLabel(status: string, ready?: boolean): string {
  if (ready && status === 'degraded') return t('health.statusReadyWithWarnings')
  if (ready) return t('health.statusReady')
  const labels: Record<string, string> = {
    action_required: t('health.statusActionRequired'),
    degraded: t('health.statusDegraded'),
    unavailable: t('health.statusUnavailable'),
    ready: t('health.statusReady'),
  }
  return labels[status] || status
}

/** — impact -> finding group kind. */
export function findingGroupKind(f: Finding): GroupKind {
  const impact = impactValue(f)
  if (impact === 'blocks_ready') return 'action'
  if (impact === 'degrades') return 'degraded'
  if (impact === 'optional') return 'optional'
  return 'ready'
}

/** — shell-quote unless the value is in the safe charset. */
export function shellArg(value: string): string {
  const text = String(value || '')
  if (/^[A-Za-z0-9_@%+=:,./~-]+$/.test(text)) return text
  return `'${text.replace(/'/g, `'\\''`)}'`
}

/** — a gateway URL is local when it resolves to a loopback host. */
export function isLocalGatewayUrl(url: string): boolean {
  const target = gatewayStatusTarget(url)
  if (!target) return true
  return ['127.0.0.1', '::1', 'localhost', '0.0.0.0'].includes(target.host)
}

/** — parse host/port, normalize wildcard hosts, default port. */
export function gatewayStatusTarget(url: string): { host: string; port: string } | null {
  try {
    const parsed = new URL(url)
    let host = parsed.hostname || '127.0.0.1'
    if (host.startsWith('[') && host.endsWith(']')) host = host.slice(1, -1)
    if (host === '0.0.0.0') host = '127.0.0.1'
    if (host === '::') host = '::1'
    const port =
      parsed.port || (parsed.protocol === 'wss:' || parsed.protocol === 'https:' ? '443' : '18791')
    return { host, port }
  } catch {
    return null
  }
}

/**
 * a gateway URL "uses the default" when it equals the
 * default RPC URL on protocol+host+pathname (query/hash ignored), NOT when the
 * localStorage override is merely absent: legacy saveConnectionSettings
 * routinely stores the default URL itself. An empty gatewayUrl
 * falls back to the default (=== true); an unparsable URL or unknown default
 * returns false, mirroring the legacy try/catch + missing-App guard.
 */
export function usesDefaultGatewayUrl(gatewayUrl: string, defaultRpcUrl: string): boolean {
  if (!defaultRpcUrl) return false
  try {
    const requested = new URL(gatewayUrl || defaultRpcUrl, location.href)
    const defaults = new URL(defaultRpcUrl, location.href)
    return (
      requested.protocol === defaults.protocol &&
      requested.host === defaults.host &&
      requested.pathname === defaults.pathname
    )
  } catch {
    return false
  }
}

function configOption(configPath: string): string {
  return configPath ? ` --config ${shellArg(configPath)}` : ''
}

/**
 * synthetic fix steps for the gateway.unavailable finding.
 * `usesDefault` mirrors legacy `_usesDefaultGatewayUrl(gatewayUrl) && configPath`:
 * when the URL matches the bootstrap default and a config path is known we target
 * the config file instead of an explicit --gateway/--bind pair.
 */
export function gatewayUnavailableFixSteps(
  url: string,
  configPath: string,
  usesDefault: boolean,
): Finding['fixSteps'] {
  if (!isLocalGatewayUrl(url)) {
    return [
      {
        label: t('health.stepInspectRemote'),
        command: `agentos gateway status --gateway ${shellArg(url)} --json`,
      },
      {
        label: t('health.stepRepairRemote'),
        detail: t('health.stepRepairRemoteDetail'),
      },
    ]
  }
  const target = gatewayStatusTarget(url)
  const bindArgs = target ? ` --bind ${target.host} --port ${target.port}` : ''
  const useConfigTarget = usesDefault && Boolean(configPath)
  const doctorTarget = useConfigTarget ? '' : url ? ` --gateway ${shellArg(url)}` : ''
  const configTarget = useConfigTarget ? configOption(configPath) : ''
  const targetArgs = useConfigTarget ? '' : bindArgs
  return [
    {
      label: t('health.stepRunDoctor'),
      command: `agentos doctor${doctorTarget}${configTarget} --json`,
      detail: t('health.stepRunDoctorDetail'),
    },
    {
      label: t('health.stepStartGateway'),
      command: `agentos gateway start${targetArgs}${configTarget}`,
    },
    {
      label: t('health.stepInspectGateway'),
      command: `agentos gateway status${targetArgs} --json${configTarget}`,
    },
  ]
}

/** — drop hidden restart keys and null/undefined values. */
export function visibleEvidenceEntries(e?: Record<string, unknown>): Array<[string, unknown]> {
  return Object.entries(e || {}).filter(
    ([key, value]) => value !== undefined && value !== null && !HIDDEN_EVIDENCE_KEYS.has(key),
  )
}

/**
 * camelCase / snake_case -> "Title-ish" label: split
 * camel-humps and _/- separators into words and capitalize only the leading
 * character, leaving each word's own casing intact. So `gatewayUrl` ->
 * `Gateway Url` (each hump keeps its capital), matching legacy 1:1.
 */
export function evidenceLabel(key: string): string {
  const label = String(key || '')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  return label ? label.charAt(0).toUpperCase() + label.slice(1) : ''
}

/** — stringify value, truncating JSON at 120 chars. */
export function evidenceValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    const text = JSON.stringify(value)
    return text.length > 120 ? `${text.slice(0, 117)}...` : text
  } catch {
    return String(value)
  }
}
