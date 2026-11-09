import { useQuery } from '@tanstack/react-query'
import { Activity, Copy, RefreshCw, ShieldAlert, ShieldCheck } from 'lucide-react'
import { toast } from 'sonner'
import { useBootstrap, useRpc } from '@/app/providers'
import { formatNumber, t } from '@/i18n'
import '@/i18n/en/webSetSystem'
import { copyWithFallback } from '@/lib/clipboard'
import { readStoredWsUrl } from '@/lib/credentials'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  evidenceLabel,
  evidenceValue,
  findingGroupKind,
  gatewayUnavailableFixSteps,
  impactCountsFromSeverity,
  impactValue,
  statusLabel,
  visibleEvidenceEntries,
  type Finding,
  type GroupKind,
  type HealthReport,
  type Impact,
} from '@/views/health/logic'
import { Button } from '@/web/components/ui/button'
import { ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'

// System › Health: the `doctor.status` report, readiness first, then the
// findings grouped by what they mean for readiness, each with its fix steps.

const GROUP_ORDER: readonly GroupKind[] = ['action', 'degraded', 'optional', 'ready']

const GROUP_TONE: Record<GroupKind, string> = {
  action: 'border-l-danger',
  degraded: 'border-l-warn',
  optional: 'border-l-info',
  ready: 'border-l-ok',
}

function groupCopy(kind: GroupKind): { title: string; note: string } {
  switch (kind) {
    case 'action':
      return { title: t('webSetSystem.groupAction'), note: t('webSetSystem.groupActionNote') }
    case 'degraded':
      return { title: t('webSetSystem.groupDegraded'), note: t('webSetSystem.groupDegradedNote') }
    case 'optional':
      return { title: t('webSetSystem.groupOptional'), note: t('webSetSystem.groupOptionalNote') }
    default:
      return { title: t('webSetSystem.groupReady'), note: t('webSetSystem.groupReadyNote') }
  }
}

function impactLabel(impact: Impact): string {
  if (impact === 'blocks_ready') return t('webSetSystem.impactBlocks')
  if (impact === 'degrades') return t('webSetSystem.impactDegrades')
  if (impact === 'optional') return t('webSetSystem.impactOptional')
  return t('webSetSystem.impactNone')
}

function stepsHeading(kind: GroupKind): string {
  if (kind === 'optional') return t('webSetSystem.stepsOptional')
  if (kind === 'ready') return t('webSetSystem.stepsReference')
  return t('webSetSystem.stepsRecovery')
}

function CommandLine({ command }: { command: string }) {
  return (
    <div className="mt-1 flex max-w-full items-center gap-1 rounded-md border border-border bg-surface-0 pl-3">
      <code
        tabIndex={0}
        className="min-w-0 flex-1 overflow-x-auto py-1.5 font-mono text-small whitespace-pre"
      >
        {command}
      </code>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={t('webSetSystem.copyCommand')}
        onClick={() =>
          void copyWithFallback(command)
            .then(() => toast.success(t('webSetSystem.copied')))
            .catch(() => toast.error(t('webSetSystem.copyFailed')))
        }
      >
        <Copy />
      </Button>
    </div>
  )
}

function FindingCard({ finding, index }: { finding: Finding; index: number }) {
  const kind = findingGroupKind(finding)
  const evidence = visibleEvidenceEntries(finding.evidence).slice(0, 6)
  const steps = finding.fixSteps ?? []
  // Raw report tokens, not copy.
  const severity = finding.severity || 'info'
  const surface = finding.surface || 'system'
  return (
    <article
      className={cn('rounded-lg border border-border border-l-4 p-3', GROUP_TONE[kind])}
      aria-label={finding.title || finding.id}
    >
      <div className="flex flex-wrap gap-2 text-small text-text-muted">
        <span>{severity}</span>
        <span>{impactLabel(impactValue(finding))}</span>
        <span className="font-mono">{surface}</span>
        {finding.restartRequired ? (
          <span className="text-warn">{t('webSetSystem.findingRestart')}</span>
        ) : null}
      </div>
      <h4 className="mt-1 font-medium">
        {finding.title || finding.id || t('webSetSystem.findingFallback', { index: index + 1 })}
      </h4>
      {finding.detail ? <p className="text-text-muted">{finding.detail}</p> : null}
      {evidence.length ? (
        <dl
          aria-label={t('webSetSystem.evidence')}
          className="mt-2 flex flex-wrap gap-1.5 text-small"
        >
          {evidence.map(([key, value]) => (
            <div key={key} className="rounded border border-border px-1.5 py-0.5">
              <dt className="inline font-medium">{evidenceLabel(key)}</dt>{' '}
              <dd className="inline font-mono break-all">{evidenceValue(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {steps.length ? (
        <div className="mt-3">
          <p className="text-small font-semibold text-text-muted">{stepsHeading(kind)}</p>
          <ol className="mt-1 flex flex-col gap-2">
            {steps.map((step, i) => (
              <li key={i} className="flex gap-2">
                <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-surface-3 text-small">
                  {i + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="font-medium">{step.label || t('webSetSystem.stepFallback')}</p>
                  {step.command ? <CommandLine command={step.command} /> : null}
                  {step.detail ? <p className="text-small text-text-muted">{step.detail}</p> : null}
                </div>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
    </article>
  )
}

function Findings({ findings }: { findings: Finding[] }) {
  if (!findings.length) {
    return <p className="text-text-muted">{t('webSetSystem.findingsEmpty')}</p>
  }
  return (
    <div className="flex flex-col gap-6">
      {GROUP_ORDER.map((kind) => {
        const group = findings.filter((f) => findingGroupKind(f) === kind)
        if (!group.length) return null
        const copy = groupCopy(kind)
        return (
          <section key={kind} aria-label={copy.title} className="flex flex-col gap-2">
            <header className="flex items-baseline justify-between gap-2">
              <div>
                <h3 className="font-semibold">{copy.title}</h3>
                <p className="text-small text-text-muted">{copy.note}</p>
              </div>
              <span className="text-small text-text-muted">{formatNumber(group.length)}</span>
            </header>
            {group.map((finding, i) => (
              <FindingCard key={finding.id || i} finding={finding} index={i} />
            ))}
          </section>
        )
      })}
    </div>
  )
}

function Readiness({ report }: { report: HealthReport }) {
  const status = report.status || 'unknown'
  const impact = report.impactCounts || impactCountsFromSeverity(report.counts || {})
  const counts: { label: string; value: number; tone: string }[] = [
    { label: t('webSetSystem.countBlocks'), value: impact.blocks_ready || 0, tone: 'text-danger' },
    { label: t('webSetSystem.countDegrades'), value: impact.degrades || 0, tone: 'text-warn' },
    { label: t('webSetSystem.countOptional'), value: impact.optional || 0, tone: 'text-info' },
    { label: t('webSetSystem.countNone'), value: impact.none || 0, tone: 'text-ok' },
  ]
  const context: [string, string][] = []
  if (report.gatewayUrl) context.push([t('webSetSystem.contextGateway'), report.gatewayUrl])
  if (report.configPath) context.push([t('webSetSystem.contextConfig'), report.configPath])
  if (report.requestedConfigPath && report.requestedConfigPath !== report.configPath) {
    context.push([t('webSetSystem.contextRequested'), report.requestedConfigPath])
  }
  if (report.agentId) context.push([t('webSetSystem.contextAgent'), report.agentId])
  const Icon = status === 'ready' ? ShieldCheck : status === 'degraded' ? Activity : ShieldAlert
  return (
    <section
      aria-label={t('webSetSystem.readiness')}
      className="flex flex-col gap-3 rounded-lg border border-border p-4"
    >
      <div className="flex items-start gap-3">
        <Icon
          className={cn(
            'mt-0.5 size-6 shrink-0',
            report.ready ? 'text-ok' : status === 'degraded' ? 'text-warn' : 'text-danger',
          )}
          aria-hidden
          data-status-icon
        />
        <div>
          <p className="text-small text-text-muted">{t('webSetSystem.readiness')}</p>
          <p data-testid="health-readiness" className="text-lg font-semibold">
            {statusLabel(status, report.ready)}
          </p>
          {report.summary ? <p className="text-text-muted">{report.summary}</p> : null}
        </div>
      </div>
      <ul role="list" className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {counts.map((c) => (
          <li key={c.label} className="rounded-md bg-surface-2 px-3 py-2">
            <span className="block text-small text-text-muted">{c.label}</span>
            <strong className={cn('text-lg', c.value > 0 && c.tone)}>
              {formatNumber(c.value)}
            </strong>
          </li>
        ))}
      </ul>
      {context.length ? (
        <dl className="flex flex-col gap-1 text-small">
          {context.map(([label, value]) => (
            <div key={label} className="flex flex-wrap gap-2">
              <dt className="text-text-muted">{label}</dt>
              <dd className="font-mono break-all">{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </section>
  )
}

export function HealthSection() {
  const rpc = useRpc()
  const bootstrap = useBootstrap()
  const connection = useDisplayedConnection()
  const gatewayUrl = readStoredWsUrl() || bootstrap.ws_url || ''
  const query = useQuery<HealthReport>({
    queryKey: ['doctor.status', 'main', 'deep'],
    queryFn: async () => {
      const report = await rpc.call<HealthReport>('doctor.status', { agentId: 'main', deep: true })
      return { ...report, gatewayUrl: report.gatewayUrl || gatewayUrl }
    },
    retry: false,
    refetchOnWindowFocus: false,
  })
  const refresh = gate(requireConnection(connection))

  const header = (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        variant="outline"
        disabled={refresh.disabled || query.isFetching}
        onClick={() => void query.refetch()}
      >
        <RefreshCw className={query.isFetching ? 'animate-spin' : undefined} aria-hidden />
        {query.isFetching ? t('webSetSystem.checking') : t('webSetSystem.refresh')}
      </Button>
      {refresh.reason ? <span className="text-small text-text-muted">{refresh.reason}</span> : null}
    </div>
  )

  if (query.isPending) {
    return (
      <div className="flex flex-col gap-4">
        {header}
        <LoadingRegion label={t('webSetSystem.checking')}>
          <Skeleton className="h-40" />
        </LoadingRegion>
      </div>
    )
  }

  if (query.isError && !query.data) {
    // The report itself is unavailable: say so as a finding, with the steps
    // that bring the gateway back.
    const finding: Finding = {
      id: 'gateway.unavailable',
      severity: 'error',
      readinessImpact: 'blocks_ready',
      surface: 'gateway',
      title: t('webSetSystem.healthUnavailable'),
      detail: query.error instanceof Error ? query.error.message : String(query.error),
      evidence: { gatewayUrl },
      fixSteps: gatewayUnavailableFixSteps(gatewayUrl, '', false),
    }
    return (
      <div className="flex flex-col gap-4">
        {header}
        <ErrorState title={t('webSetSystem.healthUnavailable')}>
          <Findings findings={[finding]} />
        </ErrorState>
      </div>
    )
  }

  const report = query.data!
  return (
    <div className="flex flex-col gap-6">
      {header}
      <Readiness report={report} />
      <Findings findings={report.findings ?? []} />
    </div>
  )
}
