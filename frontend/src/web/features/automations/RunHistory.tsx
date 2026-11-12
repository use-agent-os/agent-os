import { useQuery } from '@tanstack/react-query'
import { Check, ChevronRight, History, MessageSquare, Send, Timer, X } from 'lucide-react'
import { useState, type CSSProperties } from 'react'
import { Link } from 'react-router'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { cn } from '@/lib/utils'
import { formatDuration, runRow, type RawRun } from '@/views/cron/logic'
import { relTimeLabel } from '@/views/sessions/logic'
import { Badge } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/web/components/ui/sheet'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import '@/web/styles/page-automations.css'
import { sessionKeyToPath } from '@/web/shell/session-url'

// Run history for one automation: the last ten runs from cron.runs, each with
// its full output on demand (cron.runOutput) and a link to the run's chat when
// the session still exists. Lives under ['cron'] so `cron.run.finished`
// refreshes it with the job list.

const RUNS_LIMIT = 10

function runsQueryKey(jobId: string) {
  return ['cron', 'runs', jobId] as const
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function RunHistory({
  jobId,
  jobName,
  onClose,
}: {
  jobId: string
  jobName: string
  onClose: () => void
}) {
  const rpc = useRpc()
  const runs = useQuery({
    queryKey: runsQueryKey(jobId),
    queryFn: async (): Promise<RawRun[]> => {
      await rpc.waitForConnection()
      const data = await rpc.call<RawRun[] | { runs?: RawRun[] }>('cron.runs', {
        id: jobId,
        limit: RUNS_LIMIT,
      })
      return Array.isArray(data) ? data : (data?.runs ?? [])
    },
  })
  const [openRun, setOpenRun] = useState<string | null>(null)
  const list = runs.data ?? []

  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="w-[min(100vw,36rem)]">
        <div className="flex items-start gap-3 border-b border-border px-5 py-4">
          <span
            aria-hidden
            className="grid size-9 shrink-0 place-items-center rounded-control border border-border bg-surface-2 text-text-muted shadow-highlight [&_svg]:size-4"
          >
            <History />
          </span>
          <div className="min-w-0 flex-1">
            <SheetTitle className="truncate text-section">
              {t('webPagesWork.historyTitle', { name: jobName })}
            </SheetTitle>
            <SheetDescription className="text-small text-text-muted">
              {t('webPagesWork.historyBody', { count: RUNS_LIMIT })}
            </SheetDescription>
          </div>
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={t('webPagesWork.close')}
            onClick={onClose}
          >
            <X />
          </Button>
        </div>
        <div className="scroll-contained flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-5 py-5">
          {runs.isError ? (
            <ErrorState
              title={t('webPagesWork.historyFailed')}
              message={errorMessage(runs.error)}
              onRetry={() => void runs.refetch()}
              retrying={runs.isFetching}
            />
          ) : null}
          {runs.isPending ? (
            <LoadingRegion label={t('webPagesWork.historyLoading')} className="flex flex-col gap-5">
              {Array.from({ length: 4 }, (_, i) => (
                <div key={i} className="flex gap-3">
                  <Skeleton className="size-6 shrink-0 rounded-full" />
                  <div className="flex flex-1 flex-col gap-2 pt-1">
                    <Skeleton className="h-3.5 w-40" />
                    <Skeleton className="h-3 w-full" />
                  </div>
                </div>
              ))}
            </LoadingRegion>
          ) : list.length === 0 && !runs.isError ? (
            <EmptyState size="sm" icon={<History />} title={t('webPagesWork.historyEmpty')} />
          ) : list.length ? (
            <>
              <RunSummary runs={list} />
              <ol
                role="list"
                aria-label={t('webPagesWork.historyTable')}
                className="am-history anim-stagger flex flex-col gap-4"
              >
                {list.map((run, i) => {
                  const row = runRow(run, relTimeLabel)
                  const rowKey = row.runId || String(i)
                  const open = openRun === rowKey
                  return (
                    <li key={rowKey} className="flex gap-3">
                      <span
                        aria-hidden
                        className={cn(
                          'relative z-[1] grid size-6 shrink-0 place-items-center rounded-full border bg-surface-overlay [&_svg]:size-3.5',
                          row.statusOk ? 'border-ok/40 text-ok' : 'border-danger/40 text-danger',
                        )}
                      >
                        {row.statusOk ? <Check /> : <X />}
                      </span>
                      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                          <Badge tone={row.statusOk ? 'ok' : 'danger'}>
                            <span className="sr-only">{t('webPagesWork.runStatus')}: </span>
                            {row.status}
                          </Badge>
                          <span className="font-medium">{row.timeLabel}</span>
                          <span className="ml-auto inline-flex items-center gap-1 text-small text-text-muted tabular-nums">
                            <Timer className="size-3.5" aria-hidden />
                            <span className="sr-only">{t('webPagesWork.runDuration')}: </span>
                            {row.duration}
                          </span>
                        </div>
                        <p className="flex min-w-0 items-center gap-1.5 text-small text-text-muted">
                          <Send className="size-3.5 shrink-0" aria-hidden />
                          <span className="sr-only">{t('webPagesWork.runDelivery')}: </span>
                          <span className="truncate font-mono">{row.delivery}</span>
                        </p>
                        {row.replyFull ? (
                          <button
                            type="button"
                            aria-expanded={open}
                            onClick={() => setOpenRun(open ? null : rowKey)}
                            className="press target flex w-full items-start gap-1.5 rounded-control border border-border bg-surface-2 px-2.5 py-2 text-left text-small hover:border-border-strong"
                          >
                            <ChevronRight
                              className={cn(
                                'mt-0.5 size-3.5 shrink-0 text-text-muted transition-[rotate] duration-[var(--duration-base)] ease-[var(--ease-out)]',
                                open && 'rotate-90',
                              )}
                              aria-hidden
                            />
                            <span className="sr-only">{t('webPagesWork.runOutput')}: </span>
                            <span className="line-clamp-2 min-w-0 break-words">{row.reply}</span>
                          </button>
                        ) : (
                          <p className="text-small text-text-muted">
                            <span className="sr-only">{t('webPagesWork.runOutput')}: </span>
                            {row.reply}
                          </p>
                        )}
                        {open ? (
                          <div className="anim-enter">
                            <RunOutput jobId={jobId} runId={row.runId} preview={row.replyFull} />
                          </div>
                        ) : null}
                        {row.sessionKey && row.chatAvailable ? (
                          <Link
                            to={sessionKeyToPath(row.sessionKey)}
                            viewTransition
                            className="target inline-flex w-fit items-center gap-1 text-small text-accent hover:underline"
                          >
                            <MessageSquare className="size-3.5" aria-hidden />
                            {t('webPagesWork.runOpenChat')}
                          </Link>
                        ) : null}
                      </div>
                    </li>
                  )
                })}
              </ol>
            </>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  )
}

/**
 * The runs at a glance: how many succeeded and how long they took, with the
 * runs drawn oldest to newest as bars (height by duration, colour by outcome).
 */
function RunSummary({ runs }: { runs: RawRun[] }) {
  const ordered = [...runs].reverse()
  const ok = runs.filter((r) => (r.status || 'unknown') === 'ok').length
  const durations = runs.map((r) => r.duration_ms).filter((d): d is number => typeof d === 'number')
  const longest = Math.max(0, ...durations)
  const average = durations.length
    ? durations.reduce((sum, d) => sum + d, 0) / durations.length
    : null
  return (
    <section
      aria-label={t('webPagesWork.historySummary')}
      className="flex flex-col gap-3 rounded-card border border-border bg-surface-2 px-4 py-3.5 shadow-highlight"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <p className="text-section tabular-nums">
          {t('webPagesWork.historyOk', { ok, total: runs.length })}
        </p>
        {average != null ? (
          <p className="text-small text-text-muted tabular-nums">
            {t('webPagesWork.historyAverage', {
              duration: average < 1000 ? `${Math.round(average)}ms` : formatDuration(average),
            })}
          </p>
        ) : null}
      </div>
      <div aria-hidden className="am-runs-strip">
        {ordered.map((run, i) => (
          <span
            key={run.id ? String(run.id) : i}
            data-failed={(run.status || 'unknown') !== 'ok' || undefined}
            style={
              {
                // Runs that all took no measurable time read as full bars.
                height: longest
                  ? `${Math.max(18, ((run.duration_ms ?? 0) / longest) * 100)}%`
                  : '100%',
                '--bar-i': Math.min(i, 9),
              } as CSSProperties
            }
          />
        ))}
      </div>
      <p className="text-small text-text-muted">
        {tPlural('webPagesWork.historyStripNote', runs.length)}
      </p>
    </section>
  )
}

/** The full stored output of one run; the preview shows until it arrives. */
function RunOutput({ jobId, runId, preview }: { jobId: string; runId: string; preview: string }) {
  const rpc = useRpc()
  const output = useQuery({
    queryKey: ['cron', 'runOutput', jobId, runId],
    enabled: !!runId,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<{ output?: string }>('cron.runOutput', { id: jobId, runId })
    },
  })
  return (
    <div className="flex flex-col gap-1">
      <pre
        tabIndex={0}
        className="max-h-80 overflow-auto rounded-control bg-code-bg p-3 font-mono text-small whitespace-pre-wrap text-code-text shadow-inset"
      >
        {output.data?.output ?? preview}
      </pre>
      {output.isFetching && !output.data ? (
        <p className="text-small text-text-muted">{t('webPagesWork.runOutputLoading')}</p>
      ) : output.isError ? (
        <p role="alert" className="text-small text-danger">
          {t('webPagesWork.runOutputFailed')}
        </p>
      ) : null}
    </div>
  )
}
