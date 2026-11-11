import { useQuery } from '@tanstack/react-query'
import {
  ArrowUp,
  BarChart3,
  Calculator,
  ChevronDown,
  Coins,
  Download,
  Info,
  MessageSquare,
  MessageSquarePlus,
  RefreshCw,
  Wallet,
} from 'lucide-react'
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
import { Card, StatCard } from '@/web/components/ui/card'
import { PageHeader } from '@/web/components/ui/page-header'
import { SegmentedControl } from '@/web/components/ui/segmented-control'
import {
  EmptyState,
  ErrorState,
  LoadingRegion,
  Skeleton,
  StaleBadge,
} from '@/web/components/ui/states'
import { useCountUp } from '@/web/components/ui/use-count-up'
import { IconTile, Meter, PAGE_CLASS, STAT_CARD } from '@/web/features/home/overview-ui'
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
              <Meter className="h-1.5 w-20" segments={[{ pct: m.sharePct }]} />
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

// Cells carry data-col (the layout hooks in overview.css) and, for the
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

/** The layout hooks of one cell (see `.ov-table` in overview.css). */
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
    <Card variant="plain" padding="none" className="ov-table-wrap overflow-hidden shadow-card">
      {/* Scrolls on its own, so the header can stick while the rows move.
          The columns fit the card at desktop widths; a narrower card drops
          the cache columns, and a phone-width one turns rows into cards. */}
      <div className="scroll-contained max-h-[36rem] overflow-auto">
        <table className="ov-table">
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
                            'ov-sort size-3.5',
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
    </Card>
  )
}

const CHART_MODES: readonly ChartMode[] = ['tokens', 'cost']

/** The colour key of the chart: what each fill colour stands for. */
function ChartLegend({ mode }: { mode: ChartMode }) {
  const item = (series: 'input' | 'output', label: string) => (
    <span className="inline-flex items-center gap-1.5">
      <span className="ov-swatch" data-series={series} />
      {label}
    </span>
  )
  return (
    <p className="flex items-center gap-4 text-small text-text-muted" aria-hidden>
      {mode === 'tokens' ? (
        <>
          {item('input', t('webPagesOverview.usageChartInput'))}
          {item('output', t('webPagesOverview.usageChartOutput'))}
        </>
      ) : (
        item('input', t('webPagesOverview.usageChartCost'))
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
    <Card asChild padding="none">
      <section aria-labelledby="usage-chart-title" className="flex flex-col gap-4 p-5">
        <header className="flex flex-wrap items-center gap-x-4 gap-y-3">
          <div className="min-w-0">
            <h2 id="usage-chart-title" className="text-section">
              {t('webPagesOverview.usageChartTitle')}
            </h2>
            <p className="mt-0.5 text-small text-text-muted">
              {t('webPagesOverview.usageChartShown', { shown: chart.shown, total: chart.poolSize })}
            </p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-2">
            <ChartLegend mode={mode} />
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
          </div>
        </header>
        <ol role="list" className="flex flex-col gap-1">
          {chart.bars.map((bar, i) => (
            <li
              key={bar.key}
              aria-label={t('webPagesOverview.usageChartBar', {
                session: bar.key,
                value: bar.valueLabel,
              })}
              className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-0.5 rounded-control sm:grid-cols-[minmax(7rem,13rem)_minmax(0,1fr)_4.5rem]"
            >
              {/* A full-height row target (24 px; 44 px on touch). */}
              <Link
                to={sessionKeyToPath(bar.key)}
                title={bar.key}
                className="target inline-flex min-w-0 items-center font-mono text-small text-text underline-offset-2 transition-colors duration-[var(--duration-fast)] hover:text-accent hover:underline"
              >
                <KeyLabel value={bar.key} />
              </Link>
              <Meter
                index={i}
                className="col-span-2 row-start-2 h-2.5 sm:col-span-1 sm:row-start-auto"
                segments={[
                  { pct: bar.inputPct, series: 'input' },
                  { pct: bar.outputPct, series: 'output' },
                ]}
              />
              <span className="text-right text-small font-medium tabular-nums">
                {bar.valueLabel}
              </span>
            </li>
          ))}
        </ol>
      </section>
    </Card>
  )
}

/** A figure that counts up on arrival and on change. */
function CountedCost({ value }: { value: number }) {
  return <>{formatCost(useCountUp(value))}</>
}

/** Usage summed per model across the sessions in range. */
function ModelGrid({ rows }: { rows: UsageRow[] }) {
  const grid = useMemo(() => modelBreakdownGrid(rows), [rows])
  if (grid.models.length === 0) return null
  return (
    <section aria-labelledby="usage-models-title" className="flex flex-col gap-3">
      <h2 id="usage-models-title" className="text-section">
        {t('webPagesOverview.usageModelsTitle')}
      </h2>
      <ul role="list" className="anim-stagger grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {grid.models.map((m, i) => (
          <Card asChild key={m.model} padding="none">
            <li aria-label={m.model} className="flex min-w-0 flex-col gap-3 p-5">
              <p className="truncate font-mono text-small" title={m.model}>
                {m.provider && m.name !== m.model ? (
                  <span className="text-text-muted">{m.provider}/</span>
                ) : null}
                {m.name}
              </p>
              <div className="flex flex-col gap-2">
                <p className="text-stat tabular-nums">
                  <CountedCost value={m.costUsd} />
                </p>
                <span className="flex items-center gap-2.5">
                  <Meter index={i} className="flex-1" segments={[{ pct: m.sharePct }]} />
                  <span className="shrink-0 text-small font-medium tabular-nums">
                    {t('webPagesOverview.usageShare', { share: m.sharePct.toFixed(1) })}
                  </span>
                </span>
              </div>
              <div className="flex flex-col gap-0.5 border-t border-border pt-3 text-small text-text-muted">
                <p className="tabular-nums">
                  {t('webPagesOverview.usageModelTokens', {
                    tokens: formatNumber(m.totalTokens),
                    input: formatNumber(m.inputTokens),
                    output: formatNumber(m.outputTokens),
                  })}
                </p>
                <p>{tPlural('webPagesOverview.usageModelSessions', m.sessions)}</p>
              </div>
            </li>
          </Card>
        ))}
      </ul>
    </section>
  )
}

// The tokens card is wider: it carries the input/output/cache breakdown.
const STAT_GRID =
  'grid grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)_minmax(0,1fr)_minmax(0,1fr)]'

function UsageBody({ all, range }: { all: UsageRow[]; range: UsageRange }) {
  const visible = useMemo(() => visibleSessions(all, range), [all, range])
  const metrics = useMemo(() => usageMetrics(visible), [visible])
  const composition = useMemo(() => spendSourceHint(visible), [visible])
  const hidden = rangeHiddenHint(all, range)
  const ioTotal = metrics.input + metrics.output

  return (
    <div className="flex flex-col gap-6">
      <div className={cn('anim-stagger', STAT_GRID)}>
        <StatCard
          className={STAT_CARD}
          label={t('webPagesOverview.usageSpend')}
          icon={
            <IconTile tone="accent">
              <Wallet />
            </IconTile>
          }
          value={metrics.cost}
          format={formatCost}
          hint={composition || t('webPagesOverview.usageNoCostSource')}
        />
        <StatCard
          className={STAT_CARD}
          label={t('webPagesOverview.usageTokensTitle')}
          icon={
            <IconTile>
              <Coins />
            </IconTile>
          }
          value={metrics.totalTokens}
          format={formatNumber}
        >
          <Meter
            className="mt-1 h-1.5"
            segments={[
              { pct: ioTotal ? (metrics.input / ioTotal) * 100 : 0, series: 'input' },
              { pct: ioTotal ? (metrics.output / ioTotal) * 100 : 0, series: 'output' },
            ]}
          />
          <dl className="mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5 text-small">
            {(
              [
                ['input', t('webPagesOverview.usageInput'), metrics.input],
                ['output', t('webPagesOverview.usageOutput'), metrics.output],
                [null, t('webPagesOverview.usageCacheRead'), metrics.cacheRead],
                [null, t('webPagesOverview.usageCacheWrite'), metrics.cacheWrite],
              ] as const
            ).map(([series, label, value]) => (
              <div key={label} className="flex min-w-0 items-center justify-between gap-2">
                <dt className="inline-flex min-w-0 items-center gap-1.5 truncate text-text-muted">
                  {series ? (
                    <span className="ov-swatch size-2" data-series={series} aria-hidden />
                  ) : (
                    <span className="size-2 shrink-0" aria-hidden />
                  )}
                  {label}
                </dt>
                <dd className="tabular-nums">{formatNumber(value)}</dd>
              </div>
            ))}
          </dl>
        </StatCard>
        <StatCard
          className={STAT_CARD}
          label={t('webPagesOverview.usageSessions')}
          icon={
            <IconTile>
              <MessageSquare />
            </IconTile>
          }
          value={metrics.sessions}
          format={formatNumber}
          hint={t('webPagesOverview.usageSessionsHint')}
        />
        <StatCard
          className={STAT_CARD}
          label={t('webPagesOverview.usageAverage')}
          icon={
            <IconTile>
              <Calculator />
            </IconTile>
          }
          value={metrics.avgCost != null ? metrics.avgCost : '—'}
          format={formatCost}
          hint={t('webPagesOverview.usageAverageHint')}
        />
      </div>

      <TopSessionsChart rows={visible} />
      <ModelGrid rows={visible} />

      <section
        aria-labelledby="usage-sessions-title usage-sessions-count"
        className="flex flex-col gap-3"
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
        {visible.length === 0 ? (
          <Card variant="plain" padding="none">
            <EmptyState
              icon={<BarChart3 />}
              title={t('webPagesOverview.usageRangeEmpty')}
              description={t('webPagesOverview.usageRangeEmptyHint')}
            />
          </Card>
        ) : (
          <SessionsTable rows={visible} />
        )}
      </section>
    </div>
  )
}

/** The loading layout: the same cards in the same places, so nothing shifts. */
function UsageSkeleton() {
  const statLines = (
    <>
      <span className="flex items-center gap-2">
        <Skeleton className="size-8 rounded-control" />
        <Skeleton className="h-3.5 w-20" />
      </span>
      <Skeleton className="mt-1 h-8 w-28" />
      <Skeleton className="h-4 w-36" />
    </>
  )
  return (
    <LoadingRegion label={t('webPagesOverview.usageLoading')} className="flex flex-col gap-6">
      <div className={STAT_GRID}>
        {[0, 1, 2, 3].map((i) => (
          <Card key={i} className="flex h-36 flex-col gap-1.5">
            {statLines}
          </Card>
        ))}
      </div>
      <Card padding="none" className="flex flex-col gap-4 p-5">
        <span className="flex flex-col gap-1.5">
          <Skeleton className="h-4 w-28" />
          <Skeleton className="h-3.5 w-40" />
        </span>
        {[92, 64, 40, 22].map((w) => (
          <span
            key={w}
            className="grid grid-cols-[minmax(7rem,13rem)_minmax(0,1fr)] items-center gap-3"
          >
            <Skeleton className="h-3.5 w-28" />
            <Skeleton className="h-2.5 rounded-full" style={{ width: `${w}%` }} />
          </span>
        ))}
      </Card>
    </LoadingRegion>
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
    content = <UsageSkeleton />
  } else if (all && all.length === 0) {
    content = (
      <Card variant="plain" padding="none">
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
      </Card>
    )
  } else if (all) {
    content = <UsageBody all={all} range={range} />
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
          refresh.reason || (all && exportGate.reason) ? (
            <p className="text-small text-text-muted">{refresh.reason ?? exportGate.reason}</p>
          ) : null
        }
      />

      <div className="flex flex-col gap-2">
        <SegmentedControl
          kind="toggle"
          tone="accent"
          aria-label={t('webPagesOverview.usageRange')}
          value={range}
          onChange={pickRange}
          options={RANGES.map((value) => ({ value, label: rangeLabel(value) }))}
        />
        <p className="flex items-start gap-1.5 text-small text-text-muted">
          <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {t('webPagesOverview.usageRangeNote')}
        </p>
      </div>

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
