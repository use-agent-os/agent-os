import { CircleAlert } from 'lucide-react'
import { t, type MessageKey } from '~/i18n'
import { cn } from '~/lib/utils'
import { useNow } from '~/lib/use-now'
import { useFlowRunRecord, useFlowThumb } from '~/stores/flows'
import type { ViewerSource } from './ImageViewer'
import { formatElapsed, usd } from './price'
import {
  isTerminal,
  runImages,
  runProgress,
  viewOfRecord,
  type RunTracker,
  type RunView,
} from './run-tracker'
import type { ImageRef, NodeSchema, RunStatus, RunSummary } from './types'

export interface TrayRun {
  runId: string
  status: RunStatus
  created: string | null
  /** Unique images of the run, or null while only its summary is known. */
  images: ImageRef[] | null
  preview: ImageRef | null
  cost: number | null
  est: number | null
  origin: RunSummary['origin'] | null
  failedNode: string | null
}

export interface ImageKinds {
  /** A node's type on the canvas, for live runs whose events carry no graph. */
  typeOf?: (nodeId: string) => string | undefined
  schemas?: ReadonlyMap<string, NodeSchema>
}

/**
 * Runs for the tray, newest first: the history from `flows.runs`, with any
 * run the tracker has live state for laid over its summary (and runs too new
 * for the history put first). A summary alone does not list a run's images
 * (its `image_count` may count an image once per node it passed through), so
 * such a run's group reads the full record before it counts anything.
 */
export function trayRuns(
  summaries: readonly RunSummary[],
  tracker: RunTracker,
  kinds: ImageKinds = {},
): TrayRun[] {
  const seen = new Set<string>()
  const out: TrayRun[] = []
  const fromView = (view: RunView, summary?: RunSummary): TrayRun => {
    const images = runImages(view, kinds.typeOf, kinds.schemas)
    // A settled run whose events carried no images: read its record instead.
    const unknown = !images.length && isTerminal(view.status) && (summary?.image_count ?? 0) > 0
    return {
      runId: view.run_id,
      status: view.status,
      created: summary?.created ?? view.created ?? null,
      images: unknown ? null : images,
      preview: summary?.preview ?? null,
      cost: view.cost_usd ?? summary?.cost_usd ?? null,
      est: view.est_usd ?? summary?.est_usd ?? null,
      origin: summary?.origin ?? null,
      failedNode: view.error?.node_id ?? summary?.error?.node_id ?? null,
    }
  }
  const live = Object.values(tracker.runs)
    .map((slot) => slot.view)
    .filter((v) => !summaries.some((s) => s.run_id === v.run_id))
    .reverse()
  for (const view of live) {
    seen.add(view.run_id)
    out.push(fromView(view))
  }
  for (const s of summaries) {
    if (seen.has(s.run_id)) continue
    seen.add(s.run_id)
    const view = tracker.runs[s.run_id]?.view
    if (view) {
      out.push(fromView(view, s))
      continue
    }
    out.push({
      runId: s.run_id,
      status: s.status,
      created: s.created,
      images: s.image_count > 0 ? null : [],
      preview: s.preview,
      cost: s.cost_usd ?? null,
      est: s.est_usd ?? null,
      origin: s.origin,
      failedNode: s.error?.node_id ?? null,
    })
  }
  return out
}

function clock(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function ResultsTray({
  runs,
  active,
  activeStartedAt,
  kinds,
  nodeTitle,
  onOpen,
}: {
  runs: TrayRun[]
  active: RunView | null
  /** When this window started the active run (ms), for the elapsed clock. */
  activeStartedAt: number | null
  kinds: ImageKinds
  nodeTitle: (id: string) => string
  onOpen: (source: ViewerSource) => void
}) {
  const live = active && !isTerminal(active.status) ? active : null
  return (
    <section className="flw-tray" aria-label={t('flows.tray.title')}>
      <div className="flw-tray__head">
        {live ? (
          <LiveHeader run={live} startedAt={live.startedAt ?? activeStartedAt} />
        ) : (
          <>
            <b>{t('flows.tray.title')}</b>
            <span className="flex-1" />
            {runs.length ? <span>{t('flows.tray.hint')}</span> : null}
          </>
        )}
      </div>
      <div className="flw-tray__body">
        {runs.length === 0 ? <p className="flw-tray__empty">{t('flows.tray.empty')}</p> : null}
        {runs.map((run, i) => (
          <div key={run.runId} className="flw-tray__group-wrap">
            {i > 0 ? <span className="flw-tray__sep" aria-hidden /> : null}
            <RunGroup run={run} kinds={kinds} nodeTitle={nodeTitle} onOpen={onOpen} />
          </div>
        ))}
      </div>
    </section>
  )
}

function LiveHeader({ run, startedAt }: { run: RunView; startedAt: number | null | undefined }) {
  const now = useNow(1000)
  const { done, total } = runProgress(run)
  const pct = total ? Math.round((100 * done) / total) : 0
  return (
    <>
      <span className="flw-dot flw-dot--live" aria-hidden />
      <b>{t(`flows.run.${run.status}` as MessageKey)}</b>
      {total ? (
        <span className="font-mono">
          {done}/{total}
        </span>
      ) : null}
      <span className="flw-tray__bar" aria-hidden>
        <i style={{ width: `${pct}%` }} />
      </span>
      {startedAt ? <span className="font-mono">{formatElapsed(now - startedAt)}</span> : null}
      {run.est_usd ? (
        <span className="font-mono">
          {t('flows.tray.est')} {usd(run.est_usd)}
        </span>
      ) : null}
    </>
  )
}

function RunGroup({
  run,
  kinds,
  nodeTitle,
  onOpen,
}: {
  run: TrayRun
  kinds: ImageKinds
  nodeTitle: (id: string) => string
  onOpen: (source: ViewerSource) => void
}) {
  const record = useFlowRunRecord(run.images === null ? run.runId : null)
  const images: ImageRef[] =
    run.images ??
    (record.data
      ? runImages(viewOfRecord(record.data), kinds.typeOf, kinds.schemas)
      : run.preview
        ? [run.preview]
        : [])
  const known = run.images !== null || Boolean(record.data)

  const bits: string[] = []
  const time = clock(run.created)
  if (time) bits.push(time)
  if (run.status === 'failed') {
    bits.push(
      run.failedNode
        ? t('flows.run.failedAt').replace('{node}', nodeTitle(run.failedNode))
        : t('flows.run.failed'),
    )
  } else if (run.status !== 'done') {
    bits.push(t(`flows.run.${run.status}` as MessageKey))
  } else if (known && images.length) {
    bits.push(
      images.length === 1
        ? t('flows.tray.images.one')
        : t('flows.tray.images.many').replace('{n}', String(images.length)),
    )
  } else if (known) {
    // A run that made only text (or served everything from cache): say so,
    // rather than leave a bare time with nothing under it.
    bits.push(t('flows.tray.noImages'))
  }
  if (run.cost) bits.push(usd(run.cost))
  else if (run.est && !isTerminal(run.status)) bits.push(`${t('flows.tray.est')} ${usd(run.est)}`)
  if (run.origin === 'agent') bits.push(t('flows.tray.from.agent'))
  if (run.origin === 'cli') bits.push(t('flows.tray.from.cli'))

  return (
    <div
      className={cn('flw-tray__group', run.status === 'failed' && 'is-failed')}
      data-empty={(known && !images.length) || undefined}
    >
      <span className="flw-tray__label">
        {run.status === 'failed' ? (
          <CircleAlert className="size-3 text-danger" strokeWidth={2} aria-hidden />
        ) : null}
        {bits.join(' · ')}
      </span>
      <div className="flw-tray__thumbs">
        {images.map((img, i) => (
          <TrayThumb
            key={`${img.sha256}:${i}`}
            image={img}
            onOpen={() => onOpen({ images, index: i })}
          />
        ))}
        {!images.length && !isTerminal(run.status) ? (
          <div className="flw-tray__wait" aria-hidden>
            …
          </div>
        ) : null}
      </div>
    </div>
  )
}

function TrayThumb({ image, onOpen }: { image: ImageRef; onOpen: () => void }) {
  const thumb = useFlowThumb(image)
  return (
    <button
      type="button"
      className="flw-tray__thumb"
      aria-label={t('flows.node.openImage')}
      onClick={onOpen}
    >
      {thumb.src ? <img src={thumb.src} alt="" draggable={false} /> : null}
    </button>
  )
}
