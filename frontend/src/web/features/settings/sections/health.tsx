import { useQuery } from '@tanstack/react-query'
import {
  CircleCheck,
  CircleX,
  Copy,
  Info,
  RefreshCw,
  ShieldCheck,
  TriangleAlert,
} from 'lucide-react'
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
import { Badge } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { SectionHeader } from '@/web/components/ui/section-header'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import '@/web/styles/settings.css'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'

// System › Health: the `doctor.status` report, readiness first, then the
// findings grouped by what they mean for readiness, each with its fix steps.

const GROUP_ORDER: readonly GroupKind[] = ['action', 'degraded', 'optional', 'ready']

const GROUP_TONE = {
  action: 'danger',
  degraded: 'warn',
  optional: 'info',
  ready: 'ok',
} as const satisfies Record<GroupKind, string>

const GROUP_ICON = {
  action: CircleX,
  degraded: TriangleAlert,
  optional: Info,
  ready: CircleCheck,
} satisfies Record<GroupKind, unknown>

/** The status mark of a finding group: an icon in a tile tinted by its tone. */
function StatusMark({ kind, size = 'md' }: { kind: GroupKind; size?: 'md' | 'lg' }) {
  const Icon = GROUP_ICON[kind]
  return (
    <span
      data-tone={GROUP_TONE[kind]}
      className={cn(
        'set-notice-icon set-mark',
        size === 'lg' && 'size-11 rounded-card [&_svg]:size-5',
      )}
      aria-hidden
    >
      <Icon data-status-icon />
    </span>
  )
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
    <div className="mt-1.5 flex max-w-full items-center gap-1 rounded-control border border-border bg-surface-inset pl-3 shadow-inset">
      <span aria-hidden className="font-mono text-small text-accent select-none">
        $
      </span>
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

/** A label for the finding families that mean something beyond their severity. */
function findingBadge(id: string | undefined): string | null {
  const value = String(id || '')
  if (value.endsWith('.diagnostic.incomplete')) return t('webSetSystem.badgeDiagnosticsIncomplete')
  if (value.endsWith('.repair.pending')) return t('webSetSystem.badgeRepairPending')
  if (value === 'gateway.config.mismatch') return t('webSetSystem.badgeConfigMismatch')
  return null
}

function FindingCard({ finding, index }: { finding: Finding; index: number }) {
  const kind = findingGroupKind(finding)
  const evidence = visibleEvidenceEntries(finding.evidence).slice(0, 6)
  const steps = finding.fixSteps ?? []
  // Raw report tokens, not copy.
  const severity = finding.severity || 'info'
  const surface = finding.surface || 'system'
  const badge = findingBadge(finding.id)
  return (
    <article className="set-card flex gap-3.5" aria-label={finding.title || finding.id}>
      <StatusMark kind={kind} />
      <div className="min-w-0 flex-1">
        <h3 className="font-medium">
          {finding.title || finding.id || t('webSetSystem.findingFallback', { index: index + 1 })}
        </h3>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-small text-text-muted">
          <Badge tone={GROUP_TONE[kind]}>{severity}</Badge>
          <span>{impactLabel(impactValue(finding))}</span>
          <span aria-hidden>·</span>
          <span className="font-mono">{surface}</span>
          {finding.restartRequired ? (
            <Badge tone="warn" variant="outline">
              {t('webSetSystem.findingRestart')}
            </Badge>
          ) : null}
          {badge ? <Badge tone="neutral">{badge}</Badge> : null}
        </div>
        {finding.detail ? (
          <p className="mt-2 text-text-muted text-pretty">{finding.detail}</p>
        ) : null}
        {evidence.length ? (
          <dl
            aria-label={t('webSetSystem.evidence')}
            className="mt-2.5 flex flex-wrap gap-1.5 text-small"
          >
            {evidence.map(([key, value]) => (
              <div key={key} className="set-key gap-1.5 font-sans">
                <dt className="inline text-text-muted">{evidenceLabel(key)}</dt>{' '}
                <dd className="inline font-mono break-all">{evidenceValue(value)}</dd>
              </div>
            ))}
          </dl>
        ) : null}
        {steps.length ? (
          <div className="mt-4 border-t border-border pt-3">
            <p className="text-label text-text-muted">{stepsHeading(kind)}</p>
            <ol className="mt-2 flex flex-col gap-3">
              {steps.map((step, i) => (
                <li key={i} className="flex gap-2.5">
                  <span className="grid size-5 shrink-0 place-items-center rounded-full border border-border-strong bg-surface-2 text-small font-semibold tabular-nums">
                    {i + 1}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="font-medium">{step.label || t('webSetSystem.stepFallback')}</p>
                    {step.command ? <CommandLine command={step.command} /> : null}
                    {step.detail ? (
                      <p className="mt-1 text-small text-text-muted">{step.detail}</p>
                    ) : null}
                  </div>
                </li>
              ))}
            </ol>
          </div>
        ) : null}
      </div>
    </article>
  )
}

function Findings({ findings }: { findings: Finding[] }) {
  if (!findings.length) {
    return (
      <EmptyState
        size="sm"
        icon={<ShieldCheck />}
        title={t('webSetSystem.findingsEmpty')}
        className="set-card"
      />
    )
  }
  return (
    <div className="flex flex-col gap-6">
      {GROUP_ORDER.map((kind) => {
        const group = findings.filter((f) => findingGroupKind(f) === kind)
        if (!group.length) return null
        const copy = groupCopy(kind)
        return (
          <section key={kind} aria-label={copy.title} className="flex flex-col gap-2.5">
            <SectionHeader
              title={copy.title}
              description={copy.note}
              actions={
                <Badge tone={GROUP_TONE[kind]} className="tabular-nums">
                  {formatNumber(group.length)}
                </Badge>
              }
            />
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
  const kind: GroupKind = report.ready ? 'ready' : status === 'degraded' ? 'degraded' : 'action'
  return (
    <section
      aria-label={t('webSetSystem.readiness')}
      className="set-card relative flex flex-col gap-4 overflow-hidden"
    >
      <div className="flex items-start gap-3.5">
        <StatusMark kind={kind} size="lg" />
        <div className="min-w-0">
          <p className="text-label text-text-muted">{t('webSetSystem.readiness')}</p>
          <p data-testid="health-readiness" className="text-stat leading-tight">
            {statusLabel(status, report.ready)}
          </p>
          {report.summary ? (
            <p className="mt-1 text-text-muted text-pretty">{report.summary}</p>
          ) : null}
        </div>
      </div>
      <ul role="list" className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {counts.map((c) => (
          <li
            key={c.label}
            className="rounded-control border border-border bg-surface-2 px-3 py-2 shadow-highlight"
          >
            <span className="block text-label text-text-muted">{c.label}</span>
            <strong
              className={cn(
                'text-lg font-semibold tabular-nums',
                c.value > 0 ? c.tone : 'text-text-muted',
              )}
            >
              {formatNumber(c.value)}
            </strong>
          </li>
        ))}
      </ul>
      {context.length ? (
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 border-t border-border pt-3 text-small">
          {context.map(([label, value]) => (
            <div key={label} className="contents">
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
        size="sm"
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
        <LoadingRegion label={t('webSetSystem.checking')} className="flex flex-col gap-4">
          <Skeleton className="h-44 rounded-card" />
          <Skeleton className="h-6 w-48" />
          <Skeleton className="h-28 rounded-card" />
          <Skeleton className="h-28 rounded-card" />
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
