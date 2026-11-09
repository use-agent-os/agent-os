import { useQuery } from '@tanstack/react-query'
import { ArrowUpCircle, CheckCircle2, Loader2, RefreshCw } from 'lucide-react'
import { lazy, Suspense, useState } from 'react'
import { useBootstrap, useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSetSystem'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { Button } from '@/web/components/ui/button'
import { ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { UPDATE_CHECK_KEY, type UpdateCheck } from '@/web/shell/updates'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'

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
    <div className="grid grid-cols-[10rem_1fr] gap-2 py-1.5 max-sm:grid-cols-1 max-sm:gap-0">
      <dt className="text-text-muted">{label}</dt>
      <dd className="min-w-0 break-all">{value}</dd>
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

  return (
    <div className="flex flex-col gap-4">
      {check.isPending && connection === 'connected' ? (
        <LoadingRegion>
          <Skeleton className="h-24" />
        </LoadingRegion>
      ) : check.isError ? (
        <ErrorState
          title={t('webSetSystem.updateCheckFailed')}
          message={check.error instanceof Error ? check.error.message : String(check.error)}
        />
      ) : data ? (
        <div className="flex flex-col gap-2 rounded-lg border border-border p-4">
          <p className="flex items-center gap-2 font-medium">
            {outdated ? (
              <ArrowUpCircle className="size-5 text-info" aria-hidden data-status-icon />
            ) : (
              <CheckCircle2 className="size-5 text-ok" aria-hidden data-status-icon />
            )}
            {outdated
              ? t('webSetSystem.updateAvailable', { version: data.latest ?? '' })
              : checkStatusLabel(data.status)}
          </p>
          <dl>
            <Row
              label={t('webSetSystem.updateCurrent')}
              value={data.current || t('webSetSystem.none')}
            />
            <Row
              label={t('webSetSystem.updateLatest')}
              value={data.latest || t('webSetSystem.none')}
            />
          </dl>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          disabled={actions.disabled || check.isFetching}
          onClick={() => void check.refetch()}
        >
          <RefreshCw className={check.isFetching ? 'animate-spin' : undefined} aria-hidden />
          {check.isFetching ? t('webSetSystem.checking') : t('webSetSystem.checkNow')}
        </Button>
        <Button
          variant="primary"
          disabled={actions.disabled || !outdated || job.data?.status === 'running'}
          onClick={() => setApplyOpen(true)}
        >
          {t('webSetSystem.updateNow')}
        </Button>
        {actions.reason ? (
          <span className="text-small text-text-muted">{actions.reason}</span>
        ) : !outdated && data ? (
          <span className="text-small text-text-muted">{t('webSetSystem.updateNothing')}</span>
        ) : null}
      </div>

      {jobText ? (
        <p role="status" className="flex items-center gap-2">
          {job.data?.status === 'running' || (applied && !job.data) ? (
            <Loader2 className="size-4 animate-spin" aria-hidden />
          ) : null}
          {jobText}
        </p>
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
      <dl className="rounded-lg border border-border px-3 py-1.5">
        <Row
          label={t('webSetSystem.aboutVersion')}
          value={
            <span data-testid="about-version">{bootstrap.version || t('webSetSystem.none')}</span>
          }
        />
      </dl>
      <section aria-labelledby="about-licenses" className="flex flex-col gap-2">
        <h2 id="about-licenses" className="text-base font-semibold">
          {t('webSetSystem.licenses')}
        </h2>
        <p className="text-small text-text-muted">{t('webSetSystem.licensesHelp')}</p>
        {licenses.isPending ? (
          <LoadingRegion label={t('webSetSystem.loading')}>
            <Skeleton className="h-64" />
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
            className="max-h-[32rem] overflow-auto rounded-md border border-border bg-surface-0 p-3 font-mono text-small whitespace-pre-wrap"
          >
            {licenses.data}
          </pre>
        )}
      </section>
    </div>
  )
}
