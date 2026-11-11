import { useQuery } from '@tanstack/react-query'
import { ArrowUpCircle, CheckCircle2, Download, Loader2, RefreshCw, Sparkles } from 'lucide-react'
import { lazy, Suspense, useState } from 'react'
import { useBootstrap, useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSetSystem'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { Button } from '@/web/components/ui/button'
import { SectionHeader } from '@/web/components/ui/section-header'
import { ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { UPDATE_CHECK_KEY, type UpdateCheck } from '@/web/shell/updates'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { Notice } from '../ui'

// System › Updates and About.

const UpdateApplyDialog = lazy(async () => ({
  default: (await import('@/web/features/settings/updates/UpdateApplyDialog')).UpdateApplyDialog,
}))

/** What `updates.status` reports about the last `updates.apply` job. */
interface UpdateJob {
  status: 'idle' | 'running' | 'done' | 'failed'
  logTail?: string[]
  result?: { old?: string; new?: string } | null
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[10rem_1fr] gap-2 py-2 max-sm:grid-cols-1 max-sm:gap-0 [&+&]:border-t [&+&]:border-border">
      <dt className="text-small text-text-muted">{label}</dt>
      <dd className="min-w-0 font-mono break-all tabular-nums">{value}</dd>
    </div>
  )
}

function checkStatusLabel(status: UpdateCheck['status']): string {
  if (status === 'outdated') return t('webSetSystem.updateOutdated')
  if (status === 'offline') return t('webSetSystem.updateOffline')
  return t('webSetSystem.updateUpToDate')
}

function jobLabel(job: UpdateJob): string | null {
  const last = job.logTail?.length ? job.logTail[job.logTail.length - 1] : ''
  if (job.status === 'running') return t('webSetSystem.updateRunning')
  if (job.status === 'done') return t('webSetSystem.updateDone', { version: job.result?.new ?? '' })
  if (job.status === 'failed') return t('webSetSystem.updateFailed', { detail: last ?? '' })
  return null
}

export function UpdatesSection() {
  const rpc = useRpc()
  const connection = useDisplayedConnection()
  const [applyOpen, setApplyOpen] = useState(false)
  const [applied, setApplied] = useState(false)
  const check = useQuery({
    queryKey: UPDATE_CHECK_KEY,
    queryFn: () => rpc.call<UpdateCheck>('updates.check'),
    enabled: connection === 'connected',
    retry: false,
  })
  // After an apply, follow the job; it keeps answering across the restart.
  const job = useQuery({
    queryKey: ['updates', 'status'],
    queryFn: () => rpc.call<UpdateJob>('updates.status'),
    enabled: applied,
    retry: true,
    refetchInterval: (q) => (q.state.data?.status === 'running' || !q.state.data ? 2000 : false),
  })
  const actions = gate(requireConnection(connection))
  const data = check.data
  const outdated = data?.status === 'outdated' && !!data.latest
  const jobText = job.data ? jobLabel(job.data) : applied ? t('webSetSystem.updateRunning') : null

  const running = job.data?.status === 'running' || (applied && !job.data)
  const actionRow = (
    <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
      {actions.reason ? (
        <span className="text-small text-text-muted">{actions.reason}</span>
      ) : !outdated && data ? (
        <span className="text-small text-text-muted">{t('webSetSystem.updateNothing')}</span>
      ) : null}
      <span className="flex-1" />
      <Button
        variant="outline"
        size="sm"
        disabled={actions.disabled || check.isFetching}
        onClick={() => void check.refetch()}
      >
        <RefreshCw className={check.isFetching ? 'animate-spin' : undefined} aria-hidden />
        {check.isFetching ? t('webSetSystem.checking') : t('webSetSystem.checkNow')}
      </Button>
      <Button
        variant="primary"
        size="sm"
        disabled={actions.disabled || !outdated || job.data?.status === 'running'}
        onClick={() => setApplyOpen(true)}
      >
        <Download aria-hidden />
        {t('webSetSystem.updateNow')}
      </Button>
    </div>
  )

  return (
    <div className="flex flex-col gap-4">
      {check.isPending && connection === 'connected' ? (
        <LoadingRegion className="set-card flex flex-col gap-3">
          <Skeleton className="h-11 w-64" />
          <Skeleton className="h-16" />
        </LoadingRegion>
      ) : check.isError ? (
        <ErrorState
          title={t('webSetSystem.updateCheckFailed')}
          message={check.error instanceof Error ? check.error.message : String(check.error)}
        />
      ) : null}
      <section className="set-card relative flex flex-col gap-3 overflow-hidden">
        {outdated ? (
          <div
            aria-hidden
            className="pointer-events-none absolute inset-0 bg-[radial-gradient(70%_140%_at_0%_0%,var(--accent-tint),transparent_70%)]"
          />
        ) : null}
        {data ? (
          <>
            <p className="relative flex items-center gap-3 text-section">
              <span
                data-tone={outdated ? 'accent' : 'ok'}
                className="set-notice-icon set-mark size-10 rounded-card [&_svg]:size-5"
                aria-hidden
              >
                {outdated ? <ArrowUpCircle data-status-icon /> : <CheckCircle2 data-status-icon />}
              </span>
              {outdated
                ? t('webSetSystem.updateAvailable', { version: data.latest ?? '' })
                : checkStatusLabel(data.status)}
            </p>
            <dl className="relative">
              <Row
                label={t('webSetSystem.updateCurrent')}
                value={data.current || t('webSetSystem.none')}
              />
              <Row
                label={t('webSetSystem.updateLatest')}
                value={data.latest || t('webSetSystem.none')}
              />
            </dl>
          </>
        ) : null}
        {actionRow}
      </section>

      {jobText ? (
        <Notice
          role="status"
          tone={
            job.data?.status === 'failed' ? 'danger' : job.data?.status === 'done' ? 'ok' : 'info'
          }
          icon={running ? <Loader2 className="animate-spin" aria-hidden /> : undefined}
          className="anim-enter"
        >
          {jobText}
        </Notice>
      ) : null}

      {applyOpen && data?.latest ? (
        <Suspense fallback={null}>
          <UpdateApplyDialog
            version={data.latest}
            open={applyOpen}
            onOpenChange={setApplyOpen}
            onApplied={() => setApplied(true)}
          />
        </Suspense>
      ) : null}
    </div>
  )
}

/** The licence ledger bundled next to the console's index.html. */
function licensesUrl(): string {
  return new URL('THIRD_PARTY_LICENSES.txt', document.baseURI).href
}

export function AboutSection() {
  const bootstrap = useBootstrap()
  const licenses = useQuery({
    queryKey: ['about', 'licenses'],
    queryFn: async () => {
      const res = await fetch(licensesUrl())
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.text()
    },
    retry: false,
    staleTime: Infinity,
  })

  return (
    <div className="flex flex-col gap-6">
      <section className="set-card flex items-center gap-4">
        <span
          aria-hidden
          className="grid size-12 shrink-0 place-items-center rounded-card bg-accent [background-image:var(--accent-gradient)] text-accent-fg shadow-[var(--elevation-primary)]"
        >
          <Sparkles className="size-6" />
        </span>
        <dl className="min-w-0 flex-1">
          <Row
            label={t('webSetSystem.aboutVersion')}
            value={
              <span data-testid="about-version">{bootstrap.version || t('webSetSystem.none')}</span>
            }
          />
        </dl>
      </section>
      <section aria-labelledby="about-licenses" className="flex flex-col gap-3">
        <SectionHeader
          id="about-licenses"
          title={t('webSetSystem.licenses')}
          description={t('webSetSystem.licensesHelp')}
        />
        {licenses.isPending ? (
          <LoadingRegion label={t('webSetSystem.loading')}>
            <Skeleton className="h-64 rounded-card" />
          </LoadingRegion>
        ) : licenses.isError ? (
          <ErrorState
            title={t('webSetSystem.licensesFailed')}
            message={
              licenses.error instanceof Error ? licenses.error.message : String(licenses.error)
            }
            onRetry={() => void licenses.refetch()}
            retrying={licenses.isFetching}
          />
        ) : (
          <pre
            tabIndex={0}
            aria-label={t('webSetSystem.licenses')}
            className="set-log max-h-[32rem] overflow-auto p-4 whitespace-pre-wrap"
          >
            {licenses.data}
          </pre>
        )}
      </section>
    </div>
  )
}
