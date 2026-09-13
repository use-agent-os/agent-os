import { LoaderCircle, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { t } from '@/i18n'
import { useRpc } from './providers'

export interface UpdateCheck {
  current: string
  latest: string | null
  status: 'up-to-date' | 'outdated' | 'offline'
}

/** What `updates.status` reports about the last `updates.apply` job. */
export interface UpdateJob {
  status: 'idle' | 'running' | 'done' | 'failed'
  exitCode?: number | null
  logTail?: string[]
  result?: { old?: string; new?: string; verified?: boolean } | null
}

const POLL_MS = 2000

/**
 * The "new release" strip under the header. Beyond telling the operator, it
 * runs the upgrade for them: `updates.apply` spawns `agentos upgrade` as a
 * detached job, the gateway restarts under us, and `updates.status` keeps
 * answering from whichever gateway process is up — so polling simply rides
 * through the reconnect.
 */
export function UpdateBanner({ check, onDismiss }: { check: UpdateCheck; onDismiss: () => void }) {
  const rpc = useRpc()
  const [job, setJob] = useState<UpdateJob | null>(null)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  const running = starting || job?.status === 'running'

  useEffect(() => {
    if (job?.status !== 'running') return
    let active = true
    const tick = async () => {
      try {
        const next = await rpc.call<UpdateJob>('updates.status')
        if (active && next) setJob(next)
      } catch {
        /* the gateway is restarting under us: keep polling */
      }
      if (active) timer.current = window.setTimeout(() => void tick(), POLL_MS)
    }
    timer.current = window.setTimeout(() => void tick(), POLL_MS)
    return () => {
      active = false
      if (timer.current !== null) window.clearTimeout(timer.current)
    }
  }, [rpc, job?.status])

  const apply = async () => {
    setStarting(true)
    setError(null)
    try {
      const started = await rpc.call<UpdateJob & { started: boolean }>('updates.apply', {
        source: 'auto',
      })
      setJob(started)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setStarting(false)
    }
  }

  const lastLine = job?.logTail?.length ? job.logTail[job.logTail.length - 1] : null

  let message: string
  if (job?.status === 'done') {
    message = t('shell.updateDone', { latest: job.result?.new ?? check.latest ?? '' })
  } else if (job?.status === 'failed') {
    message = t('shell.updateFailed', { detail: lastLine ?? '' })
  } else if (running) {
    message = t('shell.updateRunning')
  } else if (error) {
    message = t('shell.updateFailed', { detail: error })
  } else {
    message = t('shell.updateAvailable', { current: check.current, latest: check.latest ?? '' })
  }

  return (
    <div
      role="status"
      className="flex items-center justify-between gap-4 border-b border-warn/20 bg-warn/10 px-4 py-2 text-sm text-warn shrink-0"
      data-testid="update-banner"
      data-state={job?.status ?? (running ? 'running' : 'idle')}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className="shrink-0 font-semibold uppercase tracking-wider text-[10px] bg-warn/25 px-1.5 py-0.5 rounded-sm">
          {t('shell.updateLabel')}
        </span>
        <span className="truncate font-medium" title={lastLine ?? undefined}>
          {message}
        </span>
        {running && lastLine ? (
          <span className="hidden truncate text-xs opacity-70 md:inline">{lastLine}</span>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {job?.status === 'done' ? (
          <Button variant="outline" size="xs" onClick={() => window.location.reload()}>
            {t('shell.updateReload')}
          </Button>
        ) : (
          <Button variant="outline" size="xs" disabled={running} onClick={() => void apply()}>
            {running ? (
              <>
                <LoaderCircle className="animate-spin" aria-hidden />
                {t('shell.updateRunningShort')}
              </>
            ) : job?.status === 'failed' || error ? (
              t('shell.updateRetry')
            ) : (
              t('shell.updateNow')
            )}
          </Button>
        )}
        <Button
          variant="ghost"
          size="icon-sm"
          disabled={running}
          onClick={onDismiss}
          title={t('shell.updateDismiss')}
          aria-label={t('shell.updateDismiss')}
        >
          <X className="size-4" />
        </Button>
      </div>
    </div>
  )
}
