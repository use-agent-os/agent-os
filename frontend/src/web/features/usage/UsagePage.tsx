import { useQuery } from '@tanstack/react-query'
import { BarChart3, ChevronDown, Download, MessageSquarePlus, RefreshCw } from 'lucide-react'
import { Fragment, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import { useRpc } from '@/app/providers'
import { formatNumber, t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesOverview'
import { blockedBecause, gate, requireConnection } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  buildCsv,
  chartRows,
  costSourceBadge,
  csvFilename,
  formatCost,
  formatRelTime,
  hasModelExpand,
  modelBreakdownGrid,
  modelDisplayLabel,
  normalizeRange,
  rangeHiddenHint,
  rowVal,
  sessionExpandRows,
  sessionTimestamp,
  sortSessions,
  sourceCompositionHint,
  usageMetrics,
  visibleSessions,
  type ChartMode,
  type CostSourceBadge,
  type SortColumn,
  type UsageRange,
  type UsageRow,
} from '@/views/usage/logic'
import { Button } from '@/web/components/ui/button'
import { StatCard } from '@/web/components/ui/card'
import { PageHeader } from '@/web/components/ui/page-header'
import {
  EmptyState,
  ErrorState,
  LoadingRegion,
  Skeleton,
  StaleBadge,
} from '@/web/components/ui/states'
import { useDocumentVisible } from '@/web/shell/session-data'
import { sessionKeyToPath } from '@/web/shell/session-url'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'

// Usage: spend and tokens from `usage.status` for the sessions active in a
// range (each row is a session's lifetime total, so the range selects
// sessions, it does not slice usage by day), the top sessions as a bar chart
// by tokens or cost, a by-model rollup, a sortable sessions table whose
// multi-model rows expand into their per-model breakdown, and CSV export of
// the rows in range. Polls every 60 s while the tab is visible; a failed
// refresh keeps the loaded figures under its error notice.

const USAGE_QUERY_KEY = ['usage', 'status'] as const
const USAGE_POLL_MS = 60_000
const RANGE_KEY = 'agentos-usage-range'
const RANGES: readonly UsageRange[] = ['all', '7', '14', '30']

interface UsageStatus {
  sessions?: UsageRow[]
}

interface Column {
  key: SortColumn | 'cache_read_tokens' | 'cache_write_tokens' | 'cost_source'
  sortable: boolean
  numeric?: boolean
}

const COLUMNS: readonly Column[] = [
  { key: 'session', sortable: true },
  { key: 'updated_at', sortable: true },
  { key: 'input_tokens', sortable: true, numeric: true },
  { key: 'output_tokens', sortable: true, numeric: true },
  { key: 'cache_read_tokens', sortable: false, numeric: true },
  { key: 'cache_write_tokens', sortable: false, numeric: true },
  { key: 'cost_usd', sortable: true, numeric: true },
  { key: 'cost_source', sortable: false },
  { key: 'model', sortable: true },
]

function columnLabel(key: Column['key']): string {
  switch (key) {
    case 'session':
      return t('webPagesOverview.usageColSession')
    case 'updated_at':
      return t('webPagesOverview.usageColModified')
    case 'input_tokens':
      return t('webPagesOverview.usageColInput')
    case 'output_tokens':
      return t('webPagesOverview.usageColOutput')
    case 'cache_read_tokens':
      return t('webPagesOverview.usageColCacheRead')
    case 'cache_write_tokens':
      return t('webPagesOverview.usageColCacheWrite')
    case 'cost_usd':
      return t('webPagesOverview.usageColCost')
    case 'cost_source':
      return t('webPagesOverview.usageColSource')
    default:
      return t('webPagesOverview.usageColModel')
  }
}

function rangeLabel(range: UsageRange): string {
  return range === 'all'
    ? t('webPagesOverview.usageRangeAll')
    : t('webPagesOverview.usageRangeDays', { days: Number(range) })
}

function readRange(): UsageRange {
  try {
    return normalizeRange(localStorage.getItem(RANGE_KEY))
  } catch {
    return '7'
  }
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? '')
}

function rowKey(row: UsageRow): string {
  return String(rowVal(row as Record<string, unknown>, 'session', 'sessionKey', 'key') ?? '')
}

function rowNumber(row: UsageRow, ...keys: string[]): string {
  const value = rowVal(row as Record<string, unknown>, ...keys)
  return value == null || value === '' ? '—' : formatNumber(Number(value))
}

const SOURCE_TONE: Record<string, string> = {
  provider_billed: 'border-ok/50 text-ok',
  provider_billed_prorated: 'border-ok/50 text-ok',
  agentos_estimate: 'border-info/50 text-info',
  mixed: 'border-warn/50 text-warn',
  unavailable: 'border-danger/50 text-danger',
}

function SourceBadge({ badge }: { badge: CostSourceBadge }) {
  return (
    <span
      title={badge.tooltip}
      className={cn(
        'inline-flex rounded-full border px-2 py-0.5 text-small whitespace-nowrap',
        badge.ephemeral
          ? 'border-dashed border-border text-text-muted'
          : (SOURCE_TONE[badge.cls] ?? 'border-border text-text-muted'),
      )}
    >
      {badge.label}
    </span>
  )
}

function Metric({
  label,
  value,
  hint,
  children,
}: {
  label: string
  value: string
  hint?: string
  children?: ReactNode
}) {
  return (
    <StatCard label={label} value={value} hint={hint}>
      {children}
    </StatCard>
  )
}

/** The per-model breakdown of a session that used more than one model. */
function ModelBreakdown({ row }: { row: UsageRow }) {
  const data = sessionExpandRows(row)
  return (
    <div className="flex flex-col gap-2 bg-surface-0 px-3 py-2">
      <p className="text-small text-text-muted">
        {tPlural('webPagesOverview.usageBreakdownModels', data.count, {
          tokens: formatNumber(data.totalTokens),
          cost: formatCost(data.totalCost),
        })}
      </p>
      {data.anyProrated ? (
        <p role="note" className="text-small text-warn">
          {t('webPagesOverview.usageBreakdownProrated')}
        </p>
      ) : null}
      <ul role="list" className="flex flex-col gap-1">
        {data.rows.map((m, i) => (
          <li key={`${m.model}:${i}`} className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="min-w-40 font-mono" title={m.model}>
              {m.provider ? <span className="text-text-muted">{m.provider}/</span> : null}
              {m.name}
            </span>
            <span className="flex items-center gap-1.5">
              <span className="h-1.5 w-20 overflow-hidden rounded-full bg-surface-3" aria-hidden>
                <span
                  className="block h-full bg-accent"
                  style={{ width: `${m.sharePct.toFixed(1)}%` }}
                />
              </span>
              <span className="text-small tabular-nums">
                {t('webPagesOverview.usageShare', { share: m.sharePct.toFixed(1) })}
              </span>
            </span>
            <span className="tabular-nums">
              {t('webPagesOverview.usageTokens', { tokens: m.tokens })}
            </span>
            <span className="tabular-nums">{formatCost(m.cost)}</span>
            <SourceBadge badge={m.badge} />
          </li>
        ))}
      </ul>
    </div>
  )
}

function SessionsTable({ rows }: { rows: UsageRow[] }) {
  const [sortCol, setSortCol] = useState<SortColumn>('updated_at')
  const [sortAsc, setSortAsc] = useState(false)
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())
  const sorted = useMemo(() => sortSessions(rows, sortCol, sortAsc), [rows, sortCol, sortAsc])

  const sortBy = (col: SortColumn) => {
    if (col === sortCol) setSortAsc((asc) => !asc)
    else {
      setSortCol(col)
      setSortAsc(false)
    }
  }
  const toggle = (key: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  return (
    <div className="scroll-contained overflow-x-auto rounded-lg border border-border">
      <table className="w-full min-w-[56rem] text-left">
        <caption className="sr-only">{t('webPagesOverview.usageTable')}</caption>
        <thead className="bg-surface-2 text-small text-text-muted">
          <tr>
            {COLUMNS.map((col) => {
              const active = col.key === sortCol
              return (
                <th
                  key={col.key}
                  scope="col"
                  aria-sort={
                    col.sortable && active ? (sortAsc ? 'ascending' : 'descending') : undefined
                  }
                  className={cn('px-3 py-2 font-medium', col.numeric && 'text-right')}
                >
                  {col.sortable ? (
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 hover:text-text"
                      onClick={() => sortBy(col.key as SortColumn)}
                    >
                      {columnLabel(col.key)}
                      <span aria-hidden>{active ? (sortAsc ? '▲' : '▼') : ''}</span>
                    </button>
                  ) : (
                    columnLabel(col.key)
                  )}
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, index) => {
            const key = rowKey(row)
            const ts = sessionTimestamp(row)
            const open = expanded.has(key)
            const canExpand = hasModelExpand(row)
            return (
              <Fragment key={key || `row-${index}`}>
                <tr className="border-t border-border">
                  <td className="max-w-64 px-3 py-2">
                    {key ? (
                      <Link
                        to={sessionKeyToPath(key)}
                        title={t('webPagesOverview.usageOpenSession', { session: key })}
                        className="block truncate font-mono text-small text-accent underline-offset-2 hover:underline"
                      >
                        {key}
                      </Link>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap text-text-muted">
                    {ts != null ? formatRelTime(ts) : '—'}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {rowNumber(row, 'input_tokens', 'inputTokens')}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {rowNumber(row, 'output_tokens', 'outputTokens')}
                  </td>
                  <td className="px-3 py-2 text-right text-text-muted tabular-nums">
                    {rowNumber(row, 'cache_read_tokens', 'cacheReadTokens')}
                  </td>
                  <td className="px-3 py-2 text-right text-text-muted tabular-nums">
                    {rowNumber(row, 'cache_write_tokens', 'cacheWriteTokens')}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatCost(
                      Number(rowVal(row as Record<string, unknown>, 'cost_usd', 'costUsd') ?? 0),
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <SourceBadge badge={costSourceBadge(row as Record<string, unknown>)} />
                  </td>
                  <td className="px-3 py-2">
                    {canExpand ? (
                      <button
                        type="button"
                        aria-expanded={open}
                        onClick={() => toggle(key)}
                        className="inline-flex items-center gap-1 rounded px-1 hover:bg-surface-3"
                      >
                        {modelDisplayLabel(row)}
                        <ChevronDown
                          className={cn('size-3.5 transition-transform', open && 'rotate-180')}
                          aria-hidden
                        />
                      </button>
                    ) : (
                      <span className="font-mono text-small">{modelDisplayLabel(row)}</span>
                    )}
                  </td>
                </tr>
                {canExpand && open ? (
                  <tr>
                    <td colSpan={COLUMNS.length} className="p-0">
                      <ModelBreakdown row={row} />
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

const CHART_MODES: readonly ChartMode[] = ['tokens', 'cost']

/** The sessions with the most usage in range, as bars, by tokens or by cost. */
function TopSessionsChart({ rows }: { rows: UsageRow[] }) {
  const [mode, setMode] = useState<ChartMode>('tokens')
  const chart = useMemo(() => chartRows(rows, mode), [rows, mode])
  if (chart.poolSize === 0) return null
  return (
    <section
      aria-labelledby="usage-chart-title"
      className="flex flex-col gap-3 rounded-lg border border-border bg-surface-1 p-4"
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h2 id="usage-chart-title" className="font-semibold">
          {t('webPagesOverview.usageChartTitle')}
        </h2>
        <span className="text-small text-text-muted">
          {t('webPagesOverview.usageChartShown', { shown: chart.shown, total: chart.poolSize })}
        </span>
        <div
          role="group"
          aria-label={t('webPagesOverview.usageChartMeasure')}
          className="ml-auto flex gap-1 rounded-md border border-border p-0.5"
        >
          {CHART_MODES.map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={mode === value}
              onClick={() => setMode(value)}
              className={cn(
                'rounded px-2.5 py-0.5 text-small',
                mode === value ? 'bg-accent text-accent-fg' : 'text-text-muted hover:bg-surface-3',
              )}
            >
              {value === 'tokens'
                ? t('webPagesOverview.usageChartTokens')
                : t('webPagesOverview.usageChartCost')}
            </button>
          ))}
        </div>
      </header>
      {mode === 'tokens' ? (
        <p className="flex items-center gap-3 text-small text-text-muted" aria-hidden>
          <span className="inline-flex items-center gap-1">
            <span className="size-2.5 rounded-sm bg-accent" />
            {t('webPagesOverview.usageChartInput')}
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="size-2.5 rounded-sm bg-info" />
            {t('webPagesOverview.usageChartOutput')}
          </span>
        </p>
      ) : null}
      <ol role="list" className="flex flex-col gap-1.5">
        {chart.bars.map((bar) => (
          <li
            key={bar.key}
            aria-label={t('webPagesOverview.usageChartBar', {
              session: bar.key,
              value: bar.valueLabel,
            })}
            className="grid grid-cols-[minmax(6rem,14rem)_1fr_auto] items-center gap-2"
          >
            <Link
              to={sessionKeyToPath(bar.key)}
              title={bar.key}
              className="truncate font-mono text-small text-accent underline-offset-2 hover:underline"
            >
              {bar.label}
            </Link>
            <span className="flex h-3 overflow-hidden rounded-sm bg-surface-3" aria-hidden>
              <span className="h-full bg-accent" style={{ width: `${bar.inputPct}%` }} />
              {bar.outputPct > 0 ? (
                <span className="h-full bg-info" style={{ width: `${bar.outputPct}%` }} />
              ) : null}
            </span>
            <span className="text-small tabular-nums">{bar.valueLabel}</span>
          </li>
        ))}
      </ol>
    </section>
  )
}

/** Usage summed per model across the sessions in range. */
function ModelGrid({ rows }: { rows: UsageRow[] }) {
  const grid = useMemo(() => modelBreakdownGrid(rows), [rows])
  if (grid.models.length === 0) return null
  return (
    <section aria-labelledby="usage-models-title" className="flex flex-col gap-2">
      <h2 id="usage-models-title" className="font-semibold">
        {t('webPagesOverview.usageModelsTitle')}
      </h2>
      <ul role="list" className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {grid.models.map((m) => (
          <li
            key={m.model}
            aria-label={m.model}
            className="flex min-w-0 flex-col gap-1.5 rounded-lg border border-border bg-surface-1 p-4"
          >
            <p className="truncate font-mono text-small" title={m.model}>
              {m.provider && m.name !== m.model ? (
                <span className="text-text-muted">{m.provider}/</span>
              ) : null}
              {m.name}
            </p>
            <p className="text-xl font-semibold tabular-nums">{formatCost(m.costUsd)}</p>
            <span className="flex items-center gap-1.5">
              <span className="h-1.5 w-24 overflow-hidden rounded-full bg-surface-3" aria-hidden>
                <span
                  className="block h-full bg-accent"
                  style={{ width: `${m.sharePct.toFixed(1)}%` }}
                />
              </span>
              <span className="text-small tabular-nums">
                {t('webPagesOverview.usageShare', { share: m.sharePct.toFixed(1) })}
              </span>
            </span>
            <p className="text-small text-text-muted tabular-nums">
              {t('webPagesOverview.usageModelTokens', {
                tokens: formatNumber(m.totalTokens),
                input: formatNumber(m.inputTokens),
                output: formatNumber(m.outputTokens),
              })}
            </p>
            <p className="text-small text-text-muted">
              {tPlural('webPagesOverview.usageModelSessions', m.sessions)}
            </p>
          </li>
        ))}
      </ul>
    </section>
  )
}

function UsageBody({ all, range }: { all: UsageRow[]; range: UsageRange }) {
  const visible = useMemo(() => visibleSessions(all, range), [all, range])
  const metrics = useMemo(() => usageMetrics(visible), [visible])
  const composition = useMemo(() => sourceCompositionHint(visible), [visible])
  const hidden = rangeHiddenHint(all, range)

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric
          label={t('webPagesOverview.usageSpend')}
          value={formatCost(metrics.cost)}
          hint={composition || t('webPagesOverview.usageNoCostSource')}
        />
        <Metric
          label={t('webPagesOverview.usageTokensTitle')}
          value={formatNumber(metrics.totalTokens)}
        >
          <dl className="grid grid-cols-2 gap-x-3 text-small">
            <dt className="text-text-muted">{t('webPagesOverview.usageInput')}</dt>
            <dd className="text-right tabular-nums">{formatNumber(metrics.input)}</dd>
            <dt className="text-text-muted">{t('webPagesOverview.usageOutput')}</dt>
            <dd className="text-right tabular-nums">{formatNumber(metrics.output)}</dd>
            <dt className="text-text-muted">{t('webPagesOverview.usageCacheRead')}</dt>
            <dd className="text-right tabular-nums">{formatNumber(metrics.cacheRead)}</dd>
            <dt className="text-text-muted">{t('webPagesOverview.usageCacheWrite')}</dt>
            <dd className="text-right tabular-nums">{formatNumber(metrics.cacheWrite)}</dd>
          </dl>
        </Metric>
        <Metric
          label={t('webPagesOverview.usageSessions')}
          value={formatNumber(metrics.sessions)}
          hint={t('webPagesOverview.usageSessionsHint')}
        />
        <Metric
          label={t('webPagesOverview.usageAverage')}
          value={metrics.avgCost != null ? formatCost(metrics.avgCost) : '—'}
          hint={t('webPagesOverview.usageAverageHint')}
        />
      </div>

      <TopSessionsChart rows={visible} />
      <ModelGrid rows={visible} />

      <section aria-labelledby="usage-sessions-title" className="flex flex-col gap-2">
        <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 id="usage-sessions-title" className="font-semibold">
            {t('webPagesOverview.usageTable')}
          </h2>
          <span className="text-small text-text-muted">
            {tPlural('webPagesOverview.usageSessionCount', visible.length)}
          </span>
          {hidden ? <span className="text-small text-text-muted">{hidden}</span> : null}
        </header>
        {visible.length === 0 ? (
          <EmptyState
            icon={<BarChart3 />}
            title={t('webPagesOverview.usageRangeEmpty')}
            description={t('webPagesOverview.usageRangeEmptyHint')}
          />
        ) : (
          <SessionsTable rows={visible} />
        )}
      </section>
    </div>
  )
}

export function UsagePage() {
  const rpc = useRpc()
  const connection = useDisplayedConnection()
  const visibleTab = useDocumentVisible()
  const [range, setRange] = useState<UsageRange>(readRange)

  const query = useQuery({
    queryKey: USAGE_QUERY_KEY,
    queryFn: async () => {
      await rpc.waitForConnection()
      const status = await rpc.call<UsageStatus>('usage.status', {})
      return status?.sessions ?? []
    },
    // The 60 s poll pauses while the document is hidden.
    refetchInterval: visibleTab ? USAGE_POLL_MS : false,
    refetchIntervalInBackground: false,
  })

  const all = query.data
  const inRange = useMemo(() => (all ? visibleSessions(all, range) : []), [all, range])
  const refresh = gate(requireConnection(connection))
  const exportGate = gate(
    !all || inRange.length === 0 ? blockedBecause(t('webPagesOverview.usageExportEmpty')) : null,
  )

  const pickRange = (next: UsageRange) => {
    setRange(next)
    try {
      localStorage.setItem(RANGE_KEY, next)
    } catch {
      // Storage unavailable: the choice still holds for this page.
    }
  }

  const exportCsv = () => {
    const blob = new Blob([buildCsv(inRange)], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = csvFilename(range)
    a.click()
    URL.revokeObjectURL(url)
  }

  let content: ReactNode
  if (query.isPending) {
    content = (
      <LoadingRegion label={t('webPagesOverview.usageLoading')} className="flex flex-col gap-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-28" />
          ))}
        </div>
        <Skeleton className="h-64" />
      </LoadingRegion>
    )
  } else if (all && all.length === 0) {
    content = (
      <EmptyState
        icon={<BarChart3 />}
        title={t('webPagesOverview.usageEmpty')}
        description={t('webPagesOverview.usageEmptyHint')}
        action={
          <Button variant="primary" asChild>
            <Link to="/new">
              <MessageSquarePlus aria-hidden />
              {t('webPagesOverview.usageStartSession')}
            </Link>
          </Button>
        }
      />
    )
  } else if (all) {
    content = <UsageBody all={all} range={range} />
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 md:p-8">
      <PageHeader
        title={t('webPagesOverview.usageTitle')}
        badge={connection !== 'connected' && all ? <StaleBadge /> : null}
        description={t('webPagesOverview.usageSubtitle')}
        actions={
          <>
            <Button
              variant="outline"
              disabled={exportGate.disabled}
              title={exportGate.reason ?? undefined}
              onClick={exportCsv}
            >
              <Download aria-hidden />
              {t('webPagesOverview.usageExport')}
            </Button>
            <Button
              variant="outline"
              disabled={refresh.disabled || query.isFetching}
              title={refresh.reason ?? undefined}
              onClick={() => void query.refetch()}
            >
              <RefreshCw className={query.isFetching ? 'animate-spin' : undefined} aria-hidden />
              {t('webPagesOverview.refresh')}
            </Button>
          </>
        }
        footer={
          refresh.reason || (all && exportGate.reason) ? (
            <p className="text-small text-text-muted">{refresh.reason ?? exportGate.reason}</p>
          ) : null
        }
      />

      <div
        role="group"
        aria-label={t('webPagesOverview.usageRange')}
        className="flex w-fit flex-wrap gap-1 rounded-lg border border-border bg-surface-1 p-1"
      >
        {RANGES.map((value) => (
          <button
            key={value}
            type="button"
            aria-pressed={range === value}
            onClick={() => pickRange(value)}
            className={cn(
              'rounded-md px-3 py-1 text-small',
              range === value ? 'bg-accent text-accent-fg' : 'text-text-muted hover:bg-surface-3',
            )}
          >
            {rangeLabel(value)}
          </button>
        ))}
      </div>
      <p className="-mt-2 text-small text-text-muted">{t('webPagesOverview.usageRangeNote')}</p>

      {query.isError ? (
        <ErrorState
          title={t('webPagesOverview.usageFailed')}
          message={errorText(query.error)}
          onRetry={() => void query.refetch()}
          retrying={query.isFetching}
        >
          {content}
        </ErrorState>
      ) : (
        content
      )}
    </div>
  )
}
