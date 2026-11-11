import { Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webConnection'
import { useUpdatePreflight } from '@/lib/update-preflight'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/web/components/ui/alert-dialog'
import { Button } from '@/web/components/ui/button'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Confirm an update after the preflight (connection spec "Update
 * availability"): list the running sessions and soon-due automations the
 * restart would interrupt before `updates.apply` is called.
 */
export function UpdateApplyDialog({
  version,
  open,
  onOpenChange,
  onApplied,
}: {
  version: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onApplied?: () => void
}) {
  const rpc = useRpc()
  const preflight = useUpdatePreflight()
  const [applying, setApplying] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { run } = preflight

  useEffect(() => {
    if (open) run()
  }, [open, run])

  const apply = async () => {
    setApplying(true)
    setError(null)
    try {
      await rpc.call('updates.apply', { source: 'auto' })
      onOpenChange(false)
      onApplied?.()
    } catch (e) {
      setError(t('webConnection.updateFailed', { message: errorMessage(e) }))
    } finally {
      setApplying(false)
    }
  }

  const report = preflight.preflight
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('webConnection.updateConfirmTitle', { version })}</AlertDialogTitle>
          <AlertDialogDescription>{t('webConnection.updateConfirmBody')}</AlertDialogDescription>
        </AlertDialogHeader>
        <div aria-live="polite" className="flex flex-col gap-2">
          {preflight.loading ? (
            <p className="flex items-center gap-2 text-text-muted">
              <Loader2 className="size-4 animate-spin" aria-hidden />
              {t('webConnection.updateChecking')}
            </p>
          ) : preflight.error ? (
            <p role="alert" className="text-danger">
              {t('webConnection.updateCheckFailed', { message: preflight.error })}
            </p>
          ) : report ? (
            <>
              {report.runningSessions.length > 0 ? (
                <div className="rounded-card border border-warn/30 bg-warn/10 px-3 py-2">
                  <p className="font-medium">
                    {tPlural('webConnection.updateRunningSessions', report.runningSessions.length)}
                  </p>
                  <ul
                    data-testid="preflight-sessions"
                    className="mt-1 ml-4 list-disc font-mono text-small"
                  >
                    {report.runningSessions.map((key) => (
                      <li key={key}>{key}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {report.dueJobs.length > 0 ? (
                <div className="rounded-card border border-border bg-surface-2 px-3 py-2">
                  <p className="font-medium">
                    {tPlural('webConnection.updateDueJobs', report.dueJobs.length)}
                  </p>
                  <ul className="mt-1 ml-4 list-disc text-small">
                    {report.dueJobs.map((job) => (
                      <li key={job.id}>{job.name}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {!report.warn ? (
                <p className="text-text-muted">{t('webConnection.updateNothingRunning')}</p>
              ) : null}
            </>
          ) : null}
          {preflight.gate.reason ? (
            <p className="text-small text-text-muted">{preflight.gate.reason}</p>
          ) : null}
          {error ? (
            <p role="alert" className="text-danger">
              {error}
            </p>
          ) : null}
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel>{t('webCommon.cancel')}</AlertDialogCancel>
          <Button
            variant="primary"
            disabled={applying || preflight.gate.disabled || !report}
            onClick={() => void apply()}
          >
            {applying ? <Loader2 className="animate-spin" aria-hidden /> : null}
            {t('webConnection.updateApply')}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
