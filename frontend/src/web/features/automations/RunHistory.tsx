import { useQuery } from '@tanstack/react-query'
import { ChevronRight, X } from 'lucide-react'
import { Fragment, useState } from 'react'
import { Link } from 'react-router'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { cn } from '@/lib/utils'
import { runRow, type RawRun } from '@/views/cron/logic'
import { relTimeLabel } from '@/views/sessions/logic'
import { Button } from '@/web/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/web/components/ui/sheet'
import { ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
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
      <SheetContent side="right" className="w-[min(100vw,40rem)]">
        <div className="flex items-start gap-2 border-b border-border px-4 py-3">
          <div className="min-w-0 flex-1">
            <SheetTitle className="text-base font-semibold">
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
        <div className="scroll-contained flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
          {runs.isError ? (
            <ErrorState
              title={t('webPagesWork.historyFailed')}
              message={errorMessage(runs.error)}
              onRetry={() => void runs.refetch()}
              retrying={runs.isFetching}
            />
          ) : null}
          {runs.isPending ? (
            <LoadingRegion label={t('webPagesWork.historyLoading')}>
              {Array.from({ length: 4 }, (_, i) => (
                <Skeleton key={i} className="my-2 h-8" />
              ))}
            </LoadingRegion>
          ) : list.length === 0 && !runs.isError ? (
            <p className="text-text-muted">{t('webPagesWork.historyEmpty')}</p>
          ) : list.length ? (
            <div
              role="region"
              aria-label={t('webPagesWork.historyTable')}
              tabIndex={0}
              className="overflow-x-auto rounded-md border border-border"
            >
              <table className="w-full min-w-[34rem] border-collapse text-left text-small">
                <thead className="bg-surface-2 text-text-muted">
                  <tr>
                    <th scope="col" className="px-2 py-1.5 font-medium">
                      {t('webPagesWork.runWhen')}
                    </th>
                    <th scope="col" className="px-2 py-1.5 font-medium">
                      {t('webPagesWork.runStatus')}
                    </th>
                    <th scope="col" className="px-2 py-1.5 font-medium">
                      {t('webPagesWork.runDuration')}
                    </th>
                    <th scope="col" className="px-2 py-1.5 font-medium">
                      {t('webPagesWork.runDelivery')}
                    </th>
                    <th scope="col" className="px-2 py-1.5 font-medium">
                      {t('webPagesWork.runOutput')}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((run, i) => {
                    const row = runRow(run, relTimeLabel)
                    const rowKey = row.runId || String(i)
                    const open = openRun === rowKey
                    return (
                      <Fragment key={rowKey}>
                        <tr className="border-t border-border align-top">
                          <td className="px-2 py-1.5 whitespace-nowrap">{row.timeLabel}</td>
                          <td
                            className={cn(
                              'px-2 py-1.5 font-medium',
                              row.statusOk ? 'text-ok' : 'text-danger',
                            )}
                          >
                            {row.status}
                          </td>
                          <td className="px-2 py-1.5 tabular-nums">{row.duration}</td>
                          <td className="px-2 py-1.5">{row.delivery}</td>
                          <td className="max-w-[16rem] px-2 py-1.5">
                            {row.replyFull ? (
                              <button
                                type="button"
                                aria-expanded={open}
                                onClick={() => setOpenRun(open ? null : rowKey)}
                                className="target flex w-full items-start gap-1 rounded text-left hover:underline"
                              >
                                <ChevronRight
                                  className={cn(
                                    'mt-0.5 size-3.5 shrink-0 transition-transform',
                                    open && 'rotate-90',
                                  )}
                                  aria-hidden
                                />
                                <span className="line-clamp-2">{row.reply}</span>
                              </button>
                            ) : (
                              <span className="text-text-muted">{row.reply}</span>
                            )}
                            {row.sessionKey && row.chatAvailable ? (
                              <Link
                                to={sessionKeyToPath(row.sessionKey)}
                                className="target mt-1 inline-flex text-accent hover:underline"
                              >
                                {t('webPagesWork.runOpenChat')}
                              </Link>
                            ) : null}
                          </td>
                        </tr>
                        {open ? (
                          <tr>
                            <td colSpan={5} className="px-2 pb-2">
                              <RunOutput jobId={jobId} runId={row.runId} preview={row.replyFull} />
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    )
                  })}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
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
      <pre className="max-h-80 overflow-auto rounded-md bg-code-bg p-2 font-mono whitespace-pre-wrap text-code-text">
        {output.data?.output ?? preview}
      </pre>
      {output.isFetching && !output.data ? (
        <p className="text-text-muted">{t('webPagesWork.runOutputLoading')}</p>
      ) : output.isError ? (
        <p role="alert" className="text-danger">
          {t('webPagesWork.runOutputFailed')}
        </p>
      ) : null}
    </div>
  )
}
