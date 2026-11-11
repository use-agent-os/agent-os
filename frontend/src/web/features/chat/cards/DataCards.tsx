import { BarChart3, LayoutGrid, Workflow } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { useTheme } from '@/stores/theme'
import type { Artifact } from '@/views/chat/transcript/artifacts'
import { normalizeCardsPayload } from '@/views/chat/transcript/cards'
import {
  CHART_HEIGHT,
  chartTheme,
  hasVolume,
  normalizeChartPayload,
  priceDecimals,
  volumeSeriesData,
  type ChartPayload,
} from '@/views/chat/transcript/chart'
import { normalizeFlowChangePayload } from '@/views/chat/transcript/flowChange'
import {
  cardFromRun,
  flowRunStatusLabel,
  formatFlowUsd,
  isTerminalFlowRun,
  normalizeFlowRunPayload,
  thumbnailUrl,
  type FlowRunCard,
} from '@/views/chat/transcript/flowRun'
import { ErrorState } from '@/web/components/ui/states'
import { CARD, CARD_PLACEHOLDER, CardHeader } from './CoreCards'
import { useArtifactJson } from './payload'

function Shell({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode
  title: string
  children: React.ReactNode
}) {
  return (
    <section className={CARD}>
      <CardHeader icon={icon} title={title} />
      {children}
    </section>
  )
}

function ChartCanvas({ payload }: { payload: ChartPayload }) {
  const host = useRef<HTMLDivElement>(null)
  const mode = useTheme((s) => s.mode)
  useEffect(() => {
    const el = host.current
    if (!el) return
    let disposed = false
    let remove: (() => void) | null = null
    void import('lightweight-charts').then((lib) => {
      if (disposed || !host.current) return
      const theme = chartTheme(mode)
      const chart = lib.createChart(el, {
        height: CHART_HEIGHT,
        autoSize: true,
        layout: { background: { color: theme.background }, textColor: theme.textColor },
        grid: { vertLines: { color: theme.gridColor }, horzLines: { color: theme.gridColor } },
        rightPriceScale: { borderColor: theme.borderColor },
        timeScale: { borderColor: theme.borderColor, timeVisible: true },
      })
      const decimals = priceDecimals(payload)
      const candles = chart.addSeries(lib.CandlestickSeries, {
        upColor: theme.upColor,
        downColor: theme.downColor,
        wickUpColor: theme.upColor,
        wickDownColor: theme.downColor,
        borderVisible: false,
        priceFormat: { type: 'price', precision: decimals, minMove: 1 / 10 ** decimals },
      })
      candles.setData(payload.candles.map((c) => ({ ...c, time: c.time as never })))
      if (hasVolume(payload)) {
        const volume = chart.addSeries(lib.HistogramSeries, {
          priceScaleId: '',
          priceFormat: { type: 'volume' },
        })
        volume.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
        volume.setData(volumeSeriesData(payload, theme) as never)
      }
      chart.timeScale().fitContent()
      remove = () => chart.remove()
    })
    return () => {
      disposed = true
      remove?.()
    }
  }, [payload, mode])
  return (
    <div
      ref={host}
      data-testid="chart-canvas"
      style={{ height: CHART_HEIGHT }}
      role="img"
      aria-label={payload.title}
    />
  )
}

function FlowRunView({ initial, name }: { initial: FlowRunCard; name: string }) {
  const rpc = useRpc()
  const [card, setCard] = useState(initial)
  const [thumbs, setThumbs] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!initial.runId) return
    let active = true
    const refresh = () =>
      rpc
        .call('flows.run.get', { run_id: initial.runId })
        .then((raw) => {
          const next = cardFromRun(raw, name)
          if (active && next) setCard(next)
        })
        .catch(() => {})
    void refresh()
    const onEvent = (payload: unknown) => {
      const row = payload as { run_id?: unknown } | null
      if (row?.run_id === initial.runId) void refresh()
    }
    const offRun = rpc.on('flows.run', onEvent)
    const offNode = rpc.on('flows.node', onEvent)
    return () => {
      active = false
      offRun()
      offNode()
    }
  }, [rpc, initial.runId, name])

  useEffect(() => {
    for (const img of card.images.slice(0, 8)) {
      if (thumbs[img.sha256]) continue
      void rpc
        .call('flows.media', { sha256: img.sha256, max_side: 256 })
        .then((raw) => {
          const url = thumbnailUrl(raw)
          if (url) setThumbs((prev) => ({ ...prev, [img.sha256]: url }))
        })
        .catch(() => {})
    }
  }, [rpc, card.images, thumbs])

  const done = card.progress.total > 0 ? `${card.progress.done}/${card.progress.total}` : ''
  return (
    <Shell icon={<Workflow />} title={t('webChat.flowRunTitle', { name: card.flowName || name })}>
      <p className="text-small" data-testid="flow-status">
        {flowRunStatusLabel(card.status)}
        {done ? ` · ${done}` : ''}
        {card.costUsd !== null ? ` · ${formatFlowUsd(card.costUsd)}` : ''}
      </p>
      {card.error ? (
        <p role="alert" className="text-small text-danger">
          {card.error.message}
        </p>
      ) : null}
      {card.images.length ? (
        <div className="mt-2 flex flex-wrap gap-2">
          {card.images
            .slice(0, 8)
            .map((img) =>
              thumbs[img.sha256] ? (
                <img
                  key={img.sha256}
                  src={thumbs[img.sha256]}
                  alt={img.name ?? ''}
                  className="anim-fade-in size-24 rounded-lg border border-border object-cover"
                />
              ) : (
                <div
                  key={img.sha256}
                  className="skeleton-shimmer size-24 rounded-lg bg-surface-3"
                  aria-hidden
                />
              ),
            )}
        </div>
      ) : null}
      {!isTerminalFlowRun(card) ? (
        <span className="sr-only">{flowRunStatusLabel(card.status)}</span>
      ) : null}
    </Shell>
  )
}

/** Chart, cards and flow cards: read from the artifact body, mounted lazily. */
export function DataCard({ category, artifact }: { category: string; artifact: Artifact }) {
  const query = useArtifactJson(artifact)
  const name = String(artifact.name ?? '')
  if (query.isPending) {
    return <div className={CARD_PLACEHOLDER} role="status" aria-label={t('webChat.cardLoading')} />
  }
  if (query.isError) {
    return (
      <ErrorState
        message={t('webChat.cardFailed', {
          message: String((query.error as Error)?.message ?? query.error),
        })}
        onRetry={() => void query.refetch()}
      />
    )
  }
  const raw = query.data
  const mime = String(artifact.mime ?? '').toLowerCase()

  if (category === 'chart') {
    const payload = normalizeChartPayload(raw)
    if (!payload) return null
    return (
      <Shell icon={<BarChart3 />} title={payload.title || t('webChat.chartTitle', { name })}>
        <ChartCanvas payload={payload} />
      </Shell>
    )
  }
  if (category === 'cards') {
    const payload = normalizeCardsPayload(raw)
    if (!payload) return null
    return (
      <Shell icon={<LayoutGrid />} title={payload.title || name}>
        <ul role="list" className="grid gap-2 sm:grid-cols-2">
          {payload.cards.map((card, i) => (
            <li key={i} className="rounded-control border border-border bg-surface-2/50 p-3">
              <p className="font-medium">{card.title}</p>
              {card.subtitle ? <p className="text-small text-text-muted">{card.subtitle}</p> : null}
              {card.badge ? <span className="text-small text-text-muted">{card.badge}</span> : null}
              <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-small">
                {card.fields.map((f, j) => (
                  <div key={j} className="contents">
                    <dt className="text-text-muted">{f.label}</dt>
                    <dd className={f.copyable ? 'font-mono break-all' : ''}>{f.value}</dd>
                  </div>
                ))}
              </dl>
            </li>
          ))}
        </ul>
      </Shell>
    )
  }
  if (category === 'flowrun' && mime.includes('flow-change')) {
    const change = normalizeFlowChangePayload(raw)
    if (!change) return null
    return (
      <Shell
        icon={<Workflow />}
        title={t('webChat.flowChangeTitle', { kind: change.change, name: change.name })}
      >
        <p className="text-small">{change.summary}</p>
      </Shell>
    )
  }
  if (category === 'flowrun') {
    const card = normalizeFlowRunPayload(raw)
    return card ? <FlowRunView initial={card} name={name} /> : null
  }
  return null
}
