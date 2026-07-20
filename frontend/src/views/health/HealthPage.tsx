import { useQuery } from '@tanstack/react-query'
import { CopyIcon, RefreshCwIcon } from 'lucide-react'
import { useEffect } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { useBootstrap, useRpc } from '@/app/providers'
import {
  evidenceLabel,
  evidenceValue,
  findingGroupKind,
  gatewayUnavailableFixSteps,
  impactCountsFromSeverity,
  impactValue,
  isLocalGatewayUrl,
  statusLabel,
  usesDefaultGatewayUrl,
  visibleEvidenceEntries,
  type Finding,
  type GroupKind,
  type HealthReport,
  type Impact,
} from './logic'

const WS_URL_KEY = 'agentos.wsUrl'

// health.js:422-430 — impact -> human label for the finding meta line.
const IMPACT_LABELS: Record<Impact, string> = {
  blocks_ready: 'Blocks readiness',
  degrades: 'Degrades',
  optional: 'Optional',
  none: 'Reference',
}

// health.js:432-437 — finding kind -> tone token used for the card accent.
const FINDING_TONE: Record<GroupKind, string> = {
  action: 'error',
  degraded: 'warn',
  optional: 'info',
  ready: 'ok',
}

// health.js:397-401 — steps heading by group kind.
function stepsHeading(kind: GroupKind): string {
  if (kind === 'optional') return 'Optional setup steps'
  if (kind === 'ready') return 'Reference steps'
  return 'Recovery steps'
}

// health.js:485-487 — normalize a value into a CSS-safe class token.
function classToken(value: string): string {
  return String(value || 'unknown')
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
}

// health.js:356-368 — id-derived badge for known finding families.
function findingBadge(finding: Finding): string | null {
  const id = String(finding?.id || '')
  if (id.endsWith('.diagnostic.incomplete')) return 'Diagnostics incomplete'
  if (id.endsWith('.repair.pending')) return 'Repair pending'
  if (id === 'gateway.config.mismatch') return 'Config mismatch'
  return null
}

// health.js:191-195 — detail text for the synthetic gateway.unavailable finding.
function gatewayUnavailableDetail(gatewayUrl: string, err: unknown): string {
  const reason = err instanceof Error ? err.message : String(err)
  if (!gatewayUrl) return reason
  return `Cannot load doctor.status from ${gatewayUrl}. ${reason}`
}

// health.js:48-62 — clipboard write with an execCommand textarea fallback.
function copyText(text: string): Promise<void> {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    return navigator.clipboard.writeText(text)
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.position = 'fixed'
  ta.style.left = '-9999px'
  document.body.appendChild(ta)
  ta.select()
  const ok = document.execCommand('copy')
  document.body.removeChild(ta)
  return ok ? Promise.resolve() : Promise.reject(new Error('Copy command failed'))
}

// health.js:35-46 — copy handler with ok/err toast.
async function onCopyCommand(command: string): Promise<void> {
  if (!command) return
  try {
    await copyText(command)
    toast.success('Copied command')
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    toast.error('Copy failed: ' + message)
  }
}

function CommandRow({ command }: { command: string }) {
  // health.js:388-395 — code + copy button.
  return (
    <span className="health-step__command">
      <code>{command}</code>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        title="Copy command"
        aria-label="Copy command"
        onClick={() => void onCopyCommand(command)}
      >
        <CopyIcon />
      </Button>
    </span>
  )
}

function StepsList({ steps, kind }: { steps: NonNullable<Finding['fixSteps']>; kind: GroupKind }) {
  // health.js:370-386 — numbered steps, optional command + detail.
  if (!steps.length) return null
  return (
    <div className="health-steps">
      <div className="health-steps__heading">{stepsHeading(kind)}</div>
      <ol>
        {steps.map((step, index) => (
          <li className="health-step" key={index}>
            <span className="health-step__number">{index + 1}</span>
            <span className="health-step__body">
              <b>{step.label || 'Step'}</b>
              {step.command ? <CommandRow command={step.command} /> : null}
              {step.detail ? <span className="health-step__detail">{step.detail}</span> : null}
            </span>
          </li>
        ))}
      </ol>
    </div>
  )
}

function EvidenceTags({ evidence }: { evidence?: Record<string, unknown> }) {
  // health.js:439-446 — up to 6 visible evidence entries.
  const entries = visibleEvidenceEntries(evidence).slice(0, 6)
  if (!entries.length) return null
  return (
    <div className="health-evidence" aria-label="Finding evidence">
      {entries.map(([key, value]) => (
        <span key={key}>
          <b>{evidenceLabel(key)}</b>
          {evidenceValue(value)}
        </span>
      ))}
    </div>
  )
}

function FindingCard({ finding, index }: { finding: Finding; index: number }) {
  // health.js:324-354 — meta line, title/detail, evidence + steps.
  const kind = findingGroupKind(finding)
  const severity = String(finding.severity || 'info')
  const impact = impactValue(finding)
  const surface = String(finding.surface || 'system')
  const badge = findingBadge(finding)
  return (
    <article className={`health-finding is-${classToken(FINDING_TONE[kind])}`}>
      <div className="health-finding__marker" aria-hidden="true">
        <span className="health-finding__dot" />
        <span className="health-finding__line" />
      </div>
      <div className="health-finding__body">
        <div className="health-finding__meta">
          <span>{severity}</span>
          <span className="health-impact">{IMPACT_LABELS[impact]}</span>
          <span className="health-surface">{surface}</span>
          {badge ? <span className="health-chip health-chip--badge">{badge}</span> : null}
          {finding.restartRequired ? (
            <span className="health-chip">Recovery requires restart</span>
          ) : null}
        </div>
        <div className="health-finding__title">
          {finding.title || finding.id || `Finding ${index + 1}`}
        </div>
        <div className="health-finding__detail">{finding.detail || ''}</div>
        <EvidenceTags evidence={finding.evidence} />
        <StepsList steps={finding.fixSteps || []} kind={kind} />
      </div>
    </article>
  )
}

const GROUPS: Array<{ kind: GroupKind; title: string; note: string }> = [
  // health.js:281-301
  {
    kind: 'action',
    title: 'Needs action',
    note: 'Fix these first to make AgentOS ready.',
  },
  {
    kind: 'degraded',
    title: 'Degraded capabilities',
    note: 'AgentOS can run, but these capabilities need attention.',
  },
  {
    kind: 'optional',
    title: 'Optional setup',
    note: 'These improve capability or posture but do not block readiness.',
  },
  {
    kind: 'ready',
    title: 'Ready checks',
    note: 'These surfaces are already working.',
  },
]

function FindingsSection({ findings }: { findings: Finding[] }) {
  // health.js:277-313 — empty state else grouped sections.
  if (!findings.length) {
    return <article className="health-empty">No findings returned.</article>
  }
  const groups = GROUPS.map((group) => ({
    ...group,
    findings: findings.filter((finding) => findingGroupKind(finding) === group.kind),
  })).filter((group) => group.findings.length)

  return (
    <>
      {groups.map((group) => (
        <section className="health-finding-group" key={group.kind}>
          <header className="health-finding-group__header">
            <div>
              <h3>{group.title}</h3>
              <p>{group.note}</p>
            </div>
            <span>{group.findings.length}</span>
          </header>
          {group.findings.map((finding, index) => (
            <FindingCard finding={finding} index={index} key={finding.id || index} />
          ))}
        </section>
      ))}
    </>
  )
}

function CountTile({ label, value, kind }: { label: string; value: number; kind: string }) {
  // health.js:270-275
  return (
    <div className={`health-count is-${classToken(kind)}`}>
      <span>{label}</span>
      <strong>{Number(value || 0)}</strong>
    </div>
  )
}

function ReportContext({
  report,
  fallbackGatewayUrl,
}: {
  report: HealthReport
  fallbackGatewayUrl: string
}) {
  // health.js:152-170 — gateway/config/agent context row.
  const items: Array<[string, string]> = []
  const gatewayUrl = report.gatewayUrl || fallbackGatewayUrl
  if (gatewayUrl) items.push(['Gateway', gatewayUrl])
  if (report.configPath) items.push(['Config', report.configPath])
  if (report.requestedConfigPath && report.requestedConfigPath !== report.configPath) {
    items.push(['Requested config', report.requestedConfigPath])
  }
  if (report.agentId) items.push(['Agent', report.agentId])
  if (!items.length) return null
  return (
    <div className="health-report-context" aria-label="Health report context">
      {items.map(([label, value]) => (
        <span className="health-report-context__item" key={label}>
          <b>{label}</b>
          <span className="health-report-context__value">{value}</span>
        </span>
      ))}
    </div>
  )
}

function StatusRail({
  report,
  fallbackGatewayUrl,
}: {
  report: HealthReport
  fallbackGatewayUrl: string
}) {
  // health.js:133-150 — readiness label + 4 count tiles.
  const impactCounts = report.impactCounts || impactCountsFromSeverity(report.counts || {})
  const status = report.status || 'unknown'
  return (
    <section className={`health-status__rail is-${classToken(status)}`} aria-label="Health summary">
      <div className="health-score">
        <span className="health-score__label">Readiness</span>
        <strong>{statusLabel(status, report.ready)}</strong>
        <span className="health-score__summary">{report.summary || status}</span>
        <ReportContext report={report} fallbackGatewayUrl={fallbackGatewayUrl} />
      </div>
      <div className="health-count-grid">
        <CountTile
          label="Needs action"
          value={impactCounts.blocks_ready || 0}
          kind="blocks_ready"
        />
        <CountTile label="Degraded" value={impactCounts.degrades || 0} kind="degrades" />
        <CountTile label="Optional" value={impactCounts.optional || 0} kind="optional" />
        <CountTile label="Ready" value={impactCounts.none || 0} kind="none" />
      </div>
    </section>
  )
}

function LoadingRail() {
  // health.js:118-130 — loading strip.
  return (
    <section className="health-status__rail is-loading" aria-label="Health summary">
      <div className="health-score">
        <span className="health-score__label">Readiness</span>
        <strong>Checking</strong>
        <span className="health-score__summary">Waiting for doctor.status</span>
      </div>
      <div className="health-count-grid">
        <CountTile label="Needs action" value={0} kind="blocks_ready" />
        <CountTile label="Degraded" value={0} kind="degrades" />
        <CountTile label="Optional" value={0} kind="optional" />
        <CountTile label="Ready" value={0} kind="none" />
      </div>
    </section>
  )
}

export function HealthPage() {
  const rpc = useRpc()
  const bootstrap = useBootstrap()

  useEffect(() => {
    document.title = 'Health - AgentOS Control'
  }, [])

  // Simplification (parity matrix): legacy _gatewayContextUrl() read
  // App.loadConnectionSettings(); the new console owns the same effective value
  // via the stored WS override falling back to bootstrap.ws_url. Storage access
  // is guarded like legacy app.js:205 — blocked storage falls back, not throws.
  let storedWsUrl: string | null = null
  try {
    storedWsUrl = localStorage.getItem(WS_URL_KEY)
  } catch {
    /* blocked storage: fall back to bootstrap */
  }
  const gatewayUrl = storedWsUrl || bootstrap.ws_url || ''
  const configPath = bootstrap.config_path || ''

  const query = useQuery<HealthReport>({
    queryKey: ['doctor.status', 'main'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const report = await rpc.call<HealthReport>('doctor.status', { agentId: 'main', deep: true })
      if (!report.gatewayUrl) report.gatewayUrl = gatewayUrl
      return report
    },
    // health.js:64-77 — legacy _load issues exactly one deep doctor.status call
    // per view entry and renders the error immediately. Pin the react-query
    // lifecycle to that contract: no retry before the error state, no cached
    // report served across view entries (fresh load + loading strip each time),
    // and no background deep diagnostics on tab focus or network reconnect.
    retry: false,
    staleTime: 0,
    gcTime: 0,
    refetchOnMount: 'always',
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  const summaryText = query.isError
    ? 'Health report unavailable'
    : query.data
      ? query.data.summary || query.data.status || 'Health report loaded'
      : 'Checking readiness'

  let railNode
  let findingsNode
  if (query.isError) {
    // health.js:86-115 — synthetic gateway.unavailable report + finding.
    // health.js:227-238 — usesDefault is URL-equality against the default RPC
    // URL (bootstrap ws_url stands in for legacy App.getDefaultRpcUrl()), not
    // mere absence of the localStorage override: legacy saveConnectionSettings
    // stores the default URL itself on save (app.js:210).
    const usesDefault = usesDefaultGatewayUrl(gatewayUrl, bootstrap.ws_url || '')
    const errorConfigPath = usesDefault && isLocalGatewayUrl(gatewayUrl) ? configPath : ''
    const errorReport: HealthReport = {
      status: 'unavailable',
      ready: false,
      // Rail summary intentionally left empty so the rail shows the status token
      // ("Unavailable") rather than duplicating the finding title. Legacy reused
      // the finding title here; the console keeps the two strings distinct.
      summary: '',
      gatewayUrl,
      configPath: errorConfigPath,
      counts: { error: 1, warn: 0, info: 0, ok: 0 },
      impactCounts: { blocks_ready: 1, degrades: 0, optional: 0, none: 0 },
    }
    const finding: Finding = {
      id: 'gateway.unavailable',
      severity: 'error',
      readinessImpact: 'blocks_ready',
      surface: 'gateway',
      title: 'Gateway health report unavailable',
      detail: gatewayUnavailableDetail(gatewayUrl, query.error),
      evidence: errorConfigPath ? { gatewayUrl, configPath: errorConfigPath } : { gatewayUrl },
      fixSteps: gatewayUnavailableFixSteps(gatewayUrl, errorConfigPath, usesDefault),
      restartRequired: false,
    }
    railNode = <StatusRail report={errorReport} fallbackGatewayUrl={gatewayUrl} />
    findingsNode = <FindingsSection findings={[finding]} />
  } else if (query.data) {
    railNode = <StatusRail report={query.data} fallbackGatewayUrl={gatewayUrl} />
    findingsNode = <FindingsSection findings={query.data.findings || []} />
  } else {
    railNode = <LoadingRail />
    findingsNode = <article className="health-empty">Loading health report</article>
  }

  return (
    <div className="health-layout health-stage">
      <header className="health-stage__header">
        <div className="health-stage__title-block">
          <span className="health-eyebrow">Control · Health</span>
          <h2>Health</h2>
          <p id="health-summary">{summaryText}</p>
        </div>
        <Button
          variant="ghost"
          id="health-refresh"
          title="Refresh health report"
          onClick={() => void query.refetch()}
        >
          <RefreshCwIcon />
          <span>Refresh</span>
        </Button>
      </header>
      {railNode}
      <section className="health-findings" aria-label="Health findings">
        {findingsNode}
      </section>
    </div>
  )
}
