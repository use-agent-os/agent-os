import { useQuery } from '@tanstack/react-query'
import {
  ArrowUp,
  BarChart3,
  ChevronDown,
  Download,
  Info,
  MessageSquarePlus,
  RefreshCw,
} from 'lucide-react'
import { Fragment, useId, useMemo, useState, type ReactNode } from 'react'
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
  usageMetrics,
  visibleSessions,
  type ChartMode,
  type CostSourceBadge,
  type SortColumn,
  type UsageRange,
  type UsageRow,
} from '@/views/usage/logic'
import { Badge } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { PageHeader } from '@/web/components/ui/page-header'
import { SegmentedControl } from '@/web/components/ui/segmented-control'
import { ErrorState, LoadingRegion, Skeleton, StaleBadge } from '@/web/components/ui/states'
import { useCountUp } from '@/web/components/ui/use-count-up'
import { PAGE_CLASS } from '@/web/features/home/overview-ui'
import { useDocumentVisible } from '@/web/shell/session-data'
import { sessionKeyToPath } from '@/web/shell/session-url'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import '@/web/styles/page-usage.css'
import { Bar, Donut, seriesAt, Swatch } from './charts'

// Usage, as an analytics view over `usage.status` for the sessions active in
// a range (each row is a session's lifetime total, so the range selects
// sessions, it does not slice usage by day): one lead figure (spend) with the
// range beside it and the token mix as one stacked bar; the top sessions as
// bars by tokens or cost beside the spend by model as a ring and a ranked
// list; then a sortable sessions table whose multi-model rows expand into
// their per-model breakdown, and CSV export of the rows in range. Polls every
// 60 s while the tab is visible; a failed refresh keeps the loaded figures
// under its error notice.

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

/** The range as a segment label: "Last 7 days", shortened to "7 days" on a phone. */
function rangeOption(range: UsageRange): ReactNode {
  if (range === 'all') return rangeLabel(range)
  return (
    <>
      <span className="max-sm:hidden">{rangeLabel(range)}</span>
      <span className="sm:hidden">
        {t('webPagesOverview.usageRangeDaysShort', { days: Number(range) })}
      </span>
    </>
  )
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

const SOURCE_TONE = {
  provider_billed: 'ok',
  provider_billed_prorated: 'ok',
  agentos_estimate: 'info',
  mixed: 'warn',
  unavailable: 'danger',
} as const

/**
 * Where the Spend figure's cost comes from, as a sentence per source:
 * "Estimated for 3 sessions · Actual for 1 session". Sessions with no cost
 * source recorded are left out; null when no session has one.
 */
function spendSourceHint(rows: UsageRow[]): string | null {
  const counts = new Map<string, number>()
  for (const row of rows) {
    const badge = costSourceBadge(row as Record<string, unknown>)
    if (badge.cls === 'none' && !badge.ephemeral) continue
    counts.set(badge.label, (counts.get(badge.label) ?? 0) + 1)
  }
  if (counts.size === 0) return null
  return [...counts]
    .map(([label, count]) => tPlural('webPagesOverview.usageSpendSource', count, { label }))
    .join(' · ')
}

function SourceBadge({ badge }: { badge: CostSourceBadge }) {
  const tone = badge.ephemeral
    ? 'neutral'
    : (SOURCE_TONE[badge.cls as keyof typeof SOURCE_TONE] ?? 'neutral')
  return (
    <Badge
      tone={tone}
      variant={badge.ephemeral ? 'outline' : 'soft'}
      title={badge.tooltip}
      className={badge.ephemeral ? 'border-dashed' : undefined}
    >
      {badge.label}
    </Badge>
  )
}

/** The per-model breakdown of a session that used more than one model. */
function ModelBreakdown({ row }: { row: UsageRow }) {
  const data = sessionExpandRows(row)
  return (
    <div className="flex flex-col gap-2 border-t border-border bg-surface-inset px-4 py-3 shadow-inset">
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
      <ul role="list" className="flex flex-col gap-1.5">
        {data.rows.map((m, i) => (
          <li key={`${m.model}:${i}`} className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <span className="min-w-40 font-mono text-small" title={m.model}>
              {m.provider ? <span className="text-text-muted">{m.provider}/</span> : null}
              {m.name}
            </span>
            <span className="flex items-center gap-2">
              <Bar className="h-1.5 w-20" segments={[{ pct: m.sharePct, series: 's1' }]} />
              <span className="text-small tabular-nums">
                {t('webPagesOverview.usageShare', { share: m.sharePct.toFixed(1) })}
              </span>
            </span>
            <span className="text-small tabular-nums">
              {t('webPagesOverview.usageTokens', { tokens: m.tokens })}
            </span>
            <span className="text-small tabular-nums">{formatCost(m.cost)}</span>
            <SourceBadge badge={m.badge} />
          </li>
        ))}
      </ul>
    </div>
  )
}

// Cells carry data-col (the layout hooks in page-usage.css) and, for the
// narrow card layout, data-label (the column name shown above the value).
const CELL = 'px-2 py-2.5 first:pl-4 last:pr-4'

const COL_ID: Record<Column['key'], string> = {
  session: 'session',
  updated_at: 'updated',
  input_tokens: 'input',
  output_tokens: 'output',
  cache_read_tokens: 'cache',
  cache_write_tokens: 'cache',
  cost_usd: 'cost',
  cost_source: 'source',
  model: 'model',
}

/** The layout hooks of one cell (see `.us-table` in page-usage.css). */
function cellProps(key: Column['key']) {
  return { 'data-col': COL_ID[key], 'data-label': columnLabel(key) }
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
    <div className="us-table-wrap">
      {/* Scrolls on its own, so the header can stick while the rows move.
          The columns fit the card at desktop widths; a narrower card drops
          the cache columns, and a phone-width one turns rows into cards. */}
      <div className="scroll-contained relative max-h-[36rem] overflow-auto">
        <table className="us-table">
          <caption className="sr-only">{t('webPagesOverview.usageTable')}</caption>
          <thead className="text-small text-text-muted">
            <tr>
              {COLUMNS.map((col) => {
                const active = col.key === sortCol
                return (
                  <th
                    key={col.key}
                    scope="col"
                    data-col={COL_ID[col.key]}
                    data-sortable={col.sortable || undefined}
                    aria-sort={
                      col.sortable && active ? (sortAsc ? 'ascending' : 'descending') : undefined
                    }
                    className={cn(
                      'px-2 py-2 font-medium whitespace-nowrap first:pl-4 last:pr-4',
                      col.numeric && 'text-right',
                    )}
                  >
                    {col.sortable ? (
                      <button
                        type="button"
                        className={cn(
                          'group target -mx-1 inline-flex items-center gap-1 rounded-sm px-1 transition-colors duration-[var(--duration-fast)] hover:text-text',
                          col.numeric && 'flex-row-reverse',
                          active && 'text-text',
                        )}
                        onClick={() => sortBy(col.key as SortColumn)}
                      >
                        {columnLabel(col.key)}
                        <ArrowUp
                          aria-hidden
                          className={cn(
                            'us-sort size-3.5',
                            active
                              ? 'text-accent opacity-100'
                              : 'opacity-0 group-hover:opacity-50 group-focus-visible:opacity-50',
                            (!active || !sortAsc) && 'rotate-180',
                          )}
                        />
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
                  <tr
                    className={cn(
                      'border-t border-border transition-colors duration-[var(--duration-fast)] first:border-t-0 hover:bg-surface-2',
                      open && 'bg-surface-2',
                    )}
                  >
                    <td className={CELL} {...cellProps('session')}>
                      {key ? (
                        <Link
                          to={sessionKeyToPath(key)}
                          title={t('webPagesOverview.usageOpenSession', { session: key })}
                          className="target flex items-center font-mono text-small text-accent underline-offset-2 hover:underline"
                        >
                          <KeyLabel value={key} />
                        </Link>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td
                      className={cn(CELL, 'whitespace-nowrap text-text-muted')}
                      {...cellProps('updated_at')}
                    >
                      {ts != null ? formatRelTime(ts) : '—'}
                    </td>
                    <td
                      className={cn(CELL, 'text-right tabular-nums')}
                      {...cellProps('input_tokens')}
                    >
                      {rowNumber(row, 'input_tokens', 'inputTokens')}
                    </td>
                    <td
                      className={cn(CELL, 'text-right tabular-nums')}
                      {...cellProps('output_tokens')}
                    >
                      {rowNumber(row, 'output_tokens', 'outputTokens')}
                    </td>
                    <td
                      className={cn(CELL, 'text-right text-text-muted tabular-nums')}
                      {...cellProps('cache_read_tokens')}
                    >
                      {rowNumber(row, 'cache_read_tokens', 'cacheReadTokens')}
                    </td>
                    <td
                      className={cn(CELL, 'text-right text-text-muted tabular-nums')}
                      {...cellProps('cache_write_tokens')}
                    >
                      {rowNumber(row, 'cache_write_tokens', 'cacheWriteTokens')}
                    </td>
                    <td
                      className={cn(CELL, 'text-right font-medium tabular-nums')}
                      {...cellProps('cost_usd')}
                    >
                      {formatCost(
                        Number(rowVal(row as Record<string, unknown>, 'cost_usd', 'costUsd') ?? 0),
                      )}
                    </td>
                    <td className={CELL} {...cellProps('cost_source')}>
                      <SourceBadge badge={costSourceBadge(row as Record<string, unknown>)} />
                    </td>
                    <td className={CELL} {...cellProps('model')}>
                      {canExpand ? (
                        <button
                          type="button"
                          aria-expanded={open}
                          title={modelDisplayLabel(row)}
                          onClick={() => toggle(key)}
                          className="press target -mx-1.5 inline-flex max-w-full items-center gap-1 rounded-control px-1.5 text-small hover:bg-surface-3"
                        >
                          <span className="truncate">{modelDisplayLabel(row)}</span>
                          <ChevronDown
                            className={cn(
                              'size-3.5 text-text-muted transition-transform duration-[var(--duration-base)] ease-[var(--ease-out)]',
                              open && 'rotate-180',
                            )}
                            aria-hidden
                          />
                        </button>
                      ) : (
                        <span
                          title={modelDisplayLabel(row)}
                          className="block truncate font-mono text-small"
                        >
                          {modelDisplayLabel(row)}
                        </span>
                      )}
                    </td>
                  </tr>
                  {canExpand && open ? (
                    <tr data-breakdown>
                      <td colSpan={COLUMNS.length} className="p-0">
                        <div className="anim-enter">
                          <ModelBreakdown row={row} />
                        </div>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

const CHART_MODES: readonly ChartMode[] = ['tokens', 'cost']

/** The colour key of the chart: what each fill colour stands for. */
function ChartLegend({ mode }: { mode: ChartMode }) {
  const item = (series: 's1' | 's2', label: string) => (
    <span className="inline-flex items-center gap-1.5">
      <Swatch series={series} />
      {label}
    </span>
  )
  return (
    <p className="flex items-center gap-4 text-small text-text-muted" aria-hidden>
      {mode === 'tokens' ? (
        <>
          {item('s1', t('webPagesOverview.usageChartInput'))}
          {item('s2', t('webPagesOverview.usageChartOutput'))}
        </>
      ) : (
        item('s1', t('webPagesOverview.usageChartCost'))
      )}
    </p>
  )
}

/**
 * A session key cut in the middle: keys share a long prefix
 * (`agent:main:webchat:`) and differ at the end, so the head truncates and
 * the tail always shows. Both halves are text, so the accessible name and
 * copy-paste are the whole key.
 */
function KeyLabel({ value }: { value: string }) {
  const cut = Math.max(0, value.length - 10)
  return (
    <>
      <span className="min-w-0 truncate">{value.slice(0, cut)}</span>
      <span className="shrink-0 whitespace-pre">{value.slice(cut)}</span>
    </>
  )
}

/** The sessions with the most usage in range, as bars, by tokens or by cost. */
function TopSessionsChart({ rows }: { rows: UsageRow[] }) {
  const [mode, setMode] = useState<ChartMode>('tokens')
  const chart = useMemo(() => chartRows(rows, mode), [rows, mode])
  if (chart.poolSize === 0) return null
  return (
    <section aria-labelledby="usage-chart-title" className="us-panel us-top">
      <header className="flex flex-wrap items-start gap-x-4 gap-y-3">
        <div className="min-w-0 flex-1">
          <h2 id="usage-chart-title" className="text-section">
            {t('webPagesOverview.usageChartTitle')}
          </h2>
          <p className="mt-0.5 text-small text-text-muted">
            {t('webPagesOverview.usageChartShown', { shown: chart.shown, total: chart.poolSize })}
          </p>
        </div>
        <SegmentedControl
          kind="toggle"
          size="sm"
          aria-label={t('webPagesOverview.usageChartMeasure')}
          value={mode}
          onChange={setMode}
          options={CHART_MODES.map((value) => ({
            value,
            label:
              value === 'tokens'
                ? t('webPagesOverview.usageChartTokens')
                : t('webPagesOverview.usageChartCost'),
          }))}
        />
      </header>
      <ChartLegend mode={mode} />
      <ol role="list" className="us-bars">
        {chart.bars.map((bar, i) => (
          <li
            key={bar.key}
            aria-label={t('webPagesOverview.usageChartBar', {
              session: bar.key,
              value: bar.valueLabel,
            })}
            className="us-bar-row"
          >
            <Link
              to={sessionKeyToPath(bar.key)}
              title={bar.key}
              className="target inline-flex min-w-0 items-center font-mono text-small text-text underline-offset-2 transition-colors duration-[var(--duration-fast)] hover:text-accent hover:underline"
            >
              <KeyLabel value={bar.key} />
            </Link>
            <span className="text-right text-small font-medium tabular-nums">{bar.valueLabel}</span>
            <Bar
              index={i}
              className="col-span-2 h-2"
              segments={[
                { pct: bar.inputPct, series: 's1' },
                { pct: bar.outputPct, series: 's2' },
              ]}
            />
          </li>
        ))}
      </ol>
    </section>
  )
}

/** A figure that counts up on arrival and on change. */
function Counted({ value, format }: { value: number; format: (n: number) => string }) {
  return <>{format(useCountUp(value))}</>
}

/** Spend summed per model across the sessions in range: a ring and a ranked list. */
function ModelsPanel({ rows }: { rows: UsageRow[] }) {
  const grid = useMemo(() => modelBreakdownGrid(rows), [rows])
  if (grid.models.length === 0) return null
  const top = grid.models[0]!
  return (
    <section aria-labelledby="usage-models-title" className="us-panel us-models">
      <header>
        <h2 id="usage-models-title" className="text-section">
          {t('webPagesOverview.usageModelsTitle')}
        </h2>
        <p className="mt-0.5 text-small text-text-muted">{t('webPagesOverview.usageModelsHint')}</p>
      </header>
      <div className="us-models-body">
        <Donut shares={grid.models.map((m, i) => ({ pct: m.sharePct, series: seriesAt(i) }))}>
          <span className="text-section tabular-nums">
            {grid.totalCost > 0 ? `${Math.round(top.sharePct)}%` : '—'}
          </span>
          <span className="max-w-[5.5rem] truncate text-small text-text-muted" title={top.model}>
            {top.name}
          </span>
        </Donut>
        <ol role="list" className="us-models-list">
          {grid.models.map((m, i) => (
            <li key={m.model} aria-label={m.model} className="us-model">
              <span className="flex min-w-0 items-center gap-2">
                <Swatch series={seriesAt(i)} />
                <span className="min-w-0 truncate font-mono text-small" title={m.model}>
                  {m.provider && m.name !== m.model ? (
                    <span className="text-text-muted">{m.provider}/</span>
                  ) : null}
                  {m.name}
                </span>
                <span className="ml-auto shrink-0 font-medium tabular-nums">
                  <Counted value={m.costUsd} format={formatCost} />
                </span>
              </span>
              <Bar
                index={i}
                className="h-1"
                segments={[{ pct: m.sharePct, series: seriesAt(i) }]}
              />
              <span className="flex flex-wrap gap-x-2 text-small text-text-muted tabular-nums">
                <span>{t('webPagesOverview.usageShare', { share: m.sharePct.toFixed(1) })}</span>
                <span aria-hidden>·</span>
                <span>{tPlural('webPagesOverview.usageModelSessions', m.sessions)}</span>
                <span aria-hidden>·</span>
                <span>
                  {t('webPagesOverview.usageTokens', { tokens: formatNumber(m.totalTokens) })}
                </span>
              </span>
            </li>
          ))}
        </ol>
      </div>
    </section>
  )
}

/**
 * The lead: spend, large, with the range beside it; the sessions and the
 * average as secondary figures; the token mix as one stacked bar and its key.
 */
function Lead({
  rows,
  range,
  onRange,
}: {
  rows: UsageRow[]
  range: UsageRange
  onRange: (range: UsageRange) => void
}) {
  const titleId = useId()
  const metrics = useMemo(() => usageMetrics(rows), [rows])
  const composition = useMemo(() => spendSourceHint(rows), [rows])
  const mix = [
    ['s1', t('webPagesOverview.usageInput'), metrics.input],
    ['s2', t('webPagesOverview.usageOutput'), metrics.output],
    ['s3', t('webPagesOverview.usageCacheRead'), metrics.cacheRead],
    ['s4', t('webPagesOverview.usageCacheWrite'), metrics.cacheWrite],
  ] as const
  const mixTotal = mix.reduce((sum, [, , value]) => sum + value, 0)

  return (
    <section aria-labelledby={titleId} className="us-lead">
      <div className="us-lead-top">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 id={titleId} className="us-eyebrow">
            {t('webPagesOverview.usageSpend')}
          </h2>
          <p className="us-lead-figure tabular-nums">
            <Counted value={metrics.cost} format={formatCost} />
          </p>
          <p className="text-small text-text-muted">
            {composition || t('webPagesOverview.usageNoCostSource')}
          </p>
        </div>
        <SegmentedControl
          kind="toggle"
          tone="accent"
          size="sm"
          className="us-range"
          aria-label={t('webPagesOverview.usageRange')}
          value={range}
          onChange={onRange}
          options={RANGES.map((value) => ({ value, label: rangeOption(value) }))}
        />
      </div>

      <dl className="us-lead-stats">
        <div>
          <dt>{t('webPagesOverview.usageTokensTitle')}</dt>
          <dd className="tabular-nums">
            <Counted value={metrics.totalTokens} format={formatNumber} />
          </dd>
        </div>
        <div>
          <dt>{t('webPagesOverview.usageSessions')}</dt>
          <dd className="tabular-nums">
            <Counted value={metrics.sessions} format={formatNumber} />
          </dd>
        </div>
        <div>
          <dt>{t('webPagesOverview.usageAverage')}</dt>
          <dd className="tabular-nums">
            {metrics.avgCost != null ? (
              <Counted value={metrics.avgCost} format={formatCost} />
            ) : (
              '—'
            )}
          </dd>
        </div>
      </dl>

      <div className="flex flex-col gap-2.5">
        <Bar
          className="us-mix"
          segments={mix.map(([series, , value]) => ({
            series,
            pct: mixTotal ? (value / mixTotal) * 100 : 0,
          }))}
        />
        <dl className="us-mix-key">
          {mix.map(([series, label, value]) => (
            <div key={series}>
              <dt>
                <Swatch series={series} />
                {label}
              </dt>
              <dd className="tabular-nums">{formatNumber(value)}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  )
}

function UsageBody({
  all,
  range,
  onRange,
}: {
  all: UsageRow[]
  range: UsageRange
  onRange: (range: UsageRange) => void
}) {
  const visible = useMemo(() => visibleSessions(all, range), [all, range])
  const hidden = rangeHiddenHint(all, range)

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-2">
        <Lead rows={visible} range={range} onRange={onRange} />
        <p className="flex items-start gap-1.5 px-1 text-small text-text-muted">
          <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {t('webPagesOverview.usageRangeNote')}
        </p>
      </div>

      {visible.length === 0 ? (
        <div className="us-range-empty anim-enter">
          <p className="text-section">{t('webPagesOverview.usageRangeEmpty')}</p>
          <p className="text-text-muted">{t('webPagesOverview.usageRangeEmptyHint')}</p>
          {range !== 'all' ? (
            <Button variant="outline" size="sm" className="mt-2" onClick={() => onRange('all')}>
              {t('webPagesOverview.usageShowAll')}
            </Button>
          ) : null}
        </div>
      ) : (
        <>
          <div className="us-grid">
            <TopSessionsChart rows={visible} />
            <ModelsPanel rows={visible} />
          </div>

          <section
            aria-labelledby="usage-sessions-title usage-sessions-count"
            className="us-rise flex flex-col gap-3"
          >
            <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h2 id="usage-sessions-title" className="text-section">
                {t('webPagesOverview.usageTable')}
              </h2>
              <span id="usage-sessions-count" className="text-small text-text-muted tabular-nums">
                {tPlural('webPagesOverview.usageSessionCount', visible.length)}
              </span>
              {hidden ? <span className="text-small text-text-muted">{hidden}</span> : null}
            </header>
            <SessionsTable rows={visible} />
          </section>
        </>
      )}
    </div>
  )
}

/** The loading layout: the same blocks in the same places, so nothing shifts. */
function UsageSkeleton() {
  return (
    <LoadingRegion label={t('webPagesOverview.usageLoading')} className="flex flex-col gap-5">
      <div className="us-lead">
        <div className="us-lead-top">
          <span className="flex flex-col gap-2">
            <Skeleton className="h-3.5 w-16" />
            <Skeleton className="h-12 w-48" />
            <Skeleton className="h-3.5 w-40" />
          </span>
          <Skeleton className="h-8 w-80 max-w-full" />
        </div>
        <Skeleton className="h-3 w-full rounded-full" />
      </div>
      <div className="us-grid">
        {[0, 1].map((i) => (
          <div key={i} className="us-panel">
            <Skeleton className="h-4 w-28" />
            {[92, 64, 40, 22].map((w) => (
              <Skeleton key={w} className="h-2 rounded-full" style={{ width: `${w}%` }} />
            ))}
          </div>
        ))}
      </div>
    </LoadingRegion>
  )
}

/**
 * No usage at all yet: heading, one line and the primary action on the
 * canvas, beside a ghosted preview of what the page shows once sessions run
 * (the shapes only, no figures).
 */
function UsageZero() {
  const titleId = useId()
  return (
    <section data-slot="empty-state" aria-labelledby={titleId} className="us-zero">
      <div className="us-zero-copy anim-enter">
        <span aria-hidden className="us-zero-mark">
          <BarChart3 />
        </span>
        <h2 id={titleId} className="us-zero-title">
          {t('webPagesOverview.usageEmpty')}
        </h2>
        <p className="max-w-[30rem] text-pretty text-text-muted">
          {t('webPagesOverview.usageEmptyHint')}
        </p>
        <Button variant="primary" size="lg" className="mt-2" asChild>
          <Link to="/new">
            <MessageSquarePlus aria-hidden />
            {t('webPagesOverview.usageStartSession')}
          </Link>
        </Button>
      </div>
      <figure aria-hidden className="us-zero-preview anim-fade-in">
        <figcaption className="text-small text-text-muted">
          {t('webPagesOverview.usageEmptyPreview')}
        </figcaption>
        <span className="us-ghost-figure" />
        <Bar
          className="us-mix"
          segments={[
            { pct: 58, series: 's1' },
            { pct: 14, series: 's2' },
            { pct: 22, series: 's3' },
            { pct: 6, series: 's4' },
          ]}
        />
        <span className="us-zero-preview-row">
          <span className="flex flex-1 flex-col gap-3">
            {[92, 64, 45, 28].map((w, i) => (
              <span key={w} className="flex flex-col gap-1.5">
                <span className="us-ghost-line" style={{ width: `${30 + i * 6}%` }} />
                <Bar
                  index={i + 1}
                  className="h-1.5"
                  segments={[
                    { pct: w * 0.8, series: 's1' },
                    { pct: w * 0.2, series: 's2' },
                  ]}
                />
              </span>
            ))}
          </span>
          <Donut
            className="us-zero-donut"
            shares={[
              { pct: 64, series: 's1' },
              { pct: 26, series: 's2' },
              { pct: 10, series: 's3' },
            ]}
          />
        </span>
      </figure>
    </section>
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
  const nothing = !!all && all.length === 0
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
    content = <UsageSkeleton />
  } else if (nothing) {
    content = <UsageZero />
  } else if (all) {
    content = <UsageBody all={all} range={range} onRange={pickRange} />
  }

  return (
    <div className={PAGE_CLASS}>
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
          refresh.reason || (all && !nothing && exportGate.reason) ? (
            <p className="text-small text-text-muted">{refresh.reason ?? exportGate.reason}</p>
          ) : null
        }
      />

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
