// Pure health-view helpers ported 1:1 from the legacy view
// (src/agentos/gateway/static/js/views/health.js). Each function below carries
// the legacy line range it mirrors so the parity matrix stays auditable.

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

/** health.js:403-411 — readinessImpact passthrough else severity mapping. */
export function impactValue(f: Pick<Finding, 'readinessImpact' | 'severity'>): Impact {
  const impact = String(f?.readinessImpact || '')
  if (['blocks_ready', 'degrades', 'optional', 'none'].includes(impact)) return impact as Impact
  const severity = String(f?.severity || 'info')
  if (severity === 'error') return 'blocks_ready'
  if (severity === 'warn') return 'degrades'
  if (severity === 'info') return 'optional'
  return 'none'
}

/** health.js:413-420 — derive impact counts from severity counts. */
export function impactCountsFromSeverity(counts: Record<string, number>): Record<Impact, number> {
  return {
    blocks_ready: Number(counts.error || 0),
    degrades: Number(counts.warn || 0),
    optional: Number(counts.info || 0),
    none: Number(counts.ok || 0),
  }
}

/** health.js:462-472 — readiness label incl. "Ready with warnings". */
export function statusLabel(status: string, ready?: boolean): string {
  if (ready && status === 'degraded') return 'Ready with warnings'
  if (ready) return 'Ready'
  const labels: Record<string, string> = {
    action_required: 'Action required',
    degraded: 'Degraded',
    unavailable: 'Unavailable',
    ready: 'Ready',
  }
  return labels[status] || status
}

/** health.js:316-322 — impact -> finding group kind. */
export function findingGroupKind(f: Finding): GroupKind {
  const impact = impactValue(f)
  if (impact === 'blocks_ready') return 'action'
  if (impact === 'degrades') return 'degraded'
  if (impact === 'optional') return 'optional'
  return 'ready'
}

/** health.js:244-248 — shell-quote unless the value is in the safe charset. */
export function shellArg(value: string): string {
  const text = String(value || '')
  if (/^[A-Za-z0-9_@%+=:,./~-]+$/.test(text)) return text
  return `'${text.replace(/'/g, `'\\''`)}'`
}

/** health.js:250-254 — a gateway URL is local when it resolves to a loopback host. */
export function isLocalGatewayUrl(url: string): boolean {
  const target = gatewayStatusTarget(url)
  if (!target) return true
  return ['127.0.0.1', '::1', 'localhost', '0.0.0.0'].includes(target.host)
}

/** health.js:256-268 — parse host/port, normalize wildcard hosts, default port. */
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
 * health.js:227-238 — a gateway URL "uses the default" when it equals the
 * default RPC URL on protocol+host+pathname (query/hash ignored), NOT when the
 * localStorage override is merely absent: legacy saveConnectionSettings
 * (app.js:210) routinely stores the default URL itself. An empty gatewayUrl
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
  // health.js:240-242
  return configPath ? ` --config ${shellArg(configPath)}` : ''
}

/**
 * health.js:197-225 — synthetic fix steps for the gateway.unavailable finding.
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
        label: 'Inspect remote gateway',
        command: `agentos gateway status --gateway ${shellArg(url)} --json`,
      },
      {
        label: 'Repair remote deployment',
        detail: 'Start or repair the remote AgentOS gateway deployment, then refresh health.',
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
      label: 'Run local doctor',
      command: `agentos doctor${doctorTarget}${configTarget} --json`,
      detail: 'Checks local config and onboarding before restarting the gateway.',
    },
    { label: 'Start local gateway', command: `agentos gateway start${targetArgs}${configTarget}` },
    {
      label: 'Inspect local gateway',
      command: `agentos gateway status${targetArgs} --json${configTarget}`,
    },
  ]
}

/** health.js:448-451 — drop hidden restart keys and null/undefined values. */
export function visibleEvidenceEntries(e?: Record<string, unknown>): Array<[string, unknown]> {
  return Object.entries(e || {}).filter(
    ([key, value]) => value !== undefined && value !== null && !HIDDEN_EVIDENCE_KEYS.has(key),
  )
}

/**
 * health.js:453-460 — camelCase / snake_case -> "Title-ish" label: split
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

/** health.js:474-483 — stringify value, truncating JSON at 120 chars. */
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
