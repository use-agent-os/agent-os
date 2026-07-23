import './usage.css'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ActivityIcon,
  ArrowUpRightIcon,
  BarChart3Icon,
  ChevronDownIcon,
  CoinsIcon,
  CpuIcon,
  DownloadIcon,
  RefreshCwIcon,
} from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { useRpc } from '@/app/providers'
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
} from './logic'

const RANGE_KEY = 'agentos-usage-range'
const RANGE_OPTIONS: { value: UsageRange; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: '7', label: '7d' },
  { value: '14', label: '14d' },
  { value: '30', label: '30d' },
]
// usage.js:16-26 — the sessions table columns; a subset is sortable.
const TABLE_COLUMNS: { key: string; label: string; sortable: boolean }[] = [
  { key: 'session', label: 'Session', sortable: true },
  { key: 'updated_at', label: 'Modified', sortable: true },
  { key: 'input_tokens', label: 'Input', sortable: true },
  { key: 'output_tokens', label: 'Output', sortable: true },
  { key: 'cache_read_tokens', label: 'Cache R', sortable: false },
  { key: 'cache_write_tokens', label: 'Cache W', sortable: false },
  { key: 'cost_usd', label: 'Cost', sortable: true },
  { key: 'cost_source', label: 'Source', sortable: false },
  { key: 'model', label: 'Model', sortable: true },
]

interface UsageStatus {
  sessions?: UsageRow[]
}

function num(row: UsageRow, ...keys: string[]): number | null {
  const v = rowVal(row as Record<string, unknown>, ...keys)
  return v == null || v === '' ? null : Number(v)
}
function localized(n: number | null): string {
  return n != null ? n.toLocaleString() : '—'
}

// ── Cost-source badge chip ────────────────────────────────────────────────────
function SourceBadge({ badge }: { badge: CostSourceBadge }) {
  return (
    <span
      className={`usage-source usage-source--${badge.cls}${badge.ephemeral ? ' usage-source--ephemeral' : ''}`}
      title={badge.tooltip}
    >
      {badge.label}
    </span>
  )
}

export function UsagePage() {
  const rpc = useRpc()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [range, setRange] = useState<UsageRange>(() =>
    normalizeRange(typeof localStorage !== 'undefined' ? localStorage.getItem(RANGE_KEY) : null),
  )
  const [chartMode, setChartMode] = useState<ChartMode>('tokens')
  const [sortCol, setSortCol] = useState<SortColumn>('updated_at')
  const [sortAsc, setSortAsc] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  useEffect(() => {
    document.title = 'Usage - AgentOS Control'
  }, [])

  // usage.js:350-366 — usage.status {} after waitForConnection; the view derives
  // every metric from status.sessions. Legacy polls every 60s and skips while
  // the tab is hidden; react-query's refetchInterval + refetchIntervalInBackground
  // false reproduces that pause/resume without a manual visibilitychange handler.
  const usageQuery = useQuery<UsageRow[]>({
    queryKey: ['usage'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const status = await rpc.call<UsageStatus>('usage.status')
      return status.sessions ?? []
    },
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  })

  useEffect(() => {
    if (usageQuery.isError) {
      const err = usageQuery.error
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to load usage: ' + message, { id: 'usage-load-err' })
    }
  }, [usageQuery.isError, usageQuery.error])

  const allSessions = useMemo(() => usageQuery.data ?? [], [usageQuery.data])
  const visible = useMemo(() => visibleSessions(allSessions, range), [allSessions, range])
  const metrics = useMemo(() => usageMetrics(visible), [visible])
  const compositionHint = useMemo(() => sourceCompositionHint(visible), [visible])
  const hiddenHint = useMemo(() => rangeHiddenHint(allSessions, range), [allSessions, range])
  const chart = useMemo(() => chartRows(visible, chartMode), [visible, chartMode])
  const grid = useMemo(() => modelBreakdownGrid(visible), [visible])
  const sorted = useMemo(() => sortSessions(visible, sortCol, sortAsc), [visible, sortCol, sortAsc])

  function pickRange(next: UsageRange) {
    setRange(next)
    try {
      localStorage.setItem(RANGE_KEY, next)
    } catch {
      /* storage unavailable — non-fatal */
    }
    setExpanded(new Set())
  }

  function onSort(col: string) {
    const key = col as SortColumn
    if (sortCol === key) setSortAsc((a) => !a)
    else {
      setSortCol(key)
      setSortAsc(false)
    }
  }
  const sortArrow = (col: string) => (sortCol === col ? (sortAsc ? ' ▲' : ' ▼') : '')
  const ariaSort = (col: string): 'ascending' | 'descending' | 'none' =>
    sortCol === col ? (sortAsc ? 'ascending' : 'descending') : 'none'

  function openChat(key: string) {
    if (key && key !== '—') navigate('/chat?session=' + encodeURIComponent(key))
  }

  function toggleExpand(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function exportCsv() {
    const csv = buildCsv(visible)
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = csvFilename(range)
    a.click()
    URL.revokeObjectURL(url)
  }

  const sessionMeta = [`${sorted.length} session${sorted.length === 1 ? '' : 's'}`, hiddenHint]
    .filter(Boolean)
    .join(' · ')

  const chartCaption =
    (chartMode === 'cost' ? 'Top sessions by cost' : 'Top sessions by total tokens') +
    (chart.poolSize > chart.shown ? ` · showing ${chart.shown} of ${chart.poolSize}` : '')

  const rangeLabel = range === 'all' ? 'All recorded activity' : `Last ${range} days`
  const errorMessage =
    usageQuery.error instanceof Error ? usageQuery.error.message : String(usageQuery.error ?? '')

  return (
    <div className="usage-stage">
      <header className="usage-stage__header">
        <div className="usage-stage__title-block">
          <span className="t-label">Control · Analytics</span>
          <h1 className="t-display">Usage</h1>
          <p className="usage-stage__subtitle">
            Tokens, cost, and per-model spend across every session.
          </p>
          {hiddenHint ? (
            <small className="usage-range-notice" aria-live="polite">
              {hiddenHint}
            </small>
          ) : null}
        </div>
        <div className="usage-stage__actions">
          <Button
            variant="outline"
            title="Download CSV"
            disabled={visible.length === 0}
            onClick={exportCsv}
          >
            <DownloadIcon />
            <span>Export CSV</span>
          </Button>
          <Button
            variant="outline"
            title="Refresh"
            disabled={usageQuery.isFetching}
            onClick={() => void queryClient.invalidateQueries({ queryKey: ['usage'] })}
          >
            <RefreshCwIcon className={usageQuery.isFetching ? 'usage-spin' : undefined} />
            <span>{usageQuery.isFetching ? 'Refreshing' : 'Refresh'}</span>
          </Button>
        </div>
      </header>

      {usageQuery.isPending ? (
        <UsageLoading />
      ) : usageQuery.isError ? (
        <section className="usage-error" role="alert">
          <div className="usage-error__icon" aria-hidden="true">
            <ActivityIcon />
          </div>
          <div>
            <h2>Usage data is unavailable</h2>
            <p>{errorMessage || 'The gateway did not return usage data.'}</p>
          </div>
          <Button variant="outline" onClick={() => void usageQuery.refetch()}>
            <RefreshCwIcon />
            Retry
          </Button>
        </section>
      ) : (
        <>
          <section className="usage-overview" aria-label="Usage summary">
            <div className="usage-overview__toolbar">
              <div>
                <span className="usage-overview__eyebrow">Billing window</span>
                <strong className="usage-overview__window">{rangeLabel}</strong>
              </div>
              <div className="usage-range" role="group" aria-label="Date range">
                {RANGE_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    className={`usage-range__btn${range === opt.value ? ' is-active' : ''}`}
                    aria-pressed={range === opt.value}
                    onClick={() => pickRange(opt.value)}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="usage-overview__body">
              <div className="usage-overview__spend" aria-label="Total cost">
                <span className="usage-overview__metric-label">
                  <CoinsIcon aria-hidden="true" />
                  Period spend
                </span>
                <strong className="usage-overview__spend-value t-data">
                  {formatCost(metrics.cost, { decimals: 4 })}
                </strong>
                <span className="usage-overview__hint">
                  {compositionHint || 'No cost source yet'}
                </span>
              </div>

              <div className="usage-overview__tokens" aria-label="Total tokens">
                <span className="usage-overview__metric-label">
                  <ActivityIcon aria-hidden="true" />
                  Token volume
                </span>
                <strong className="usage-overview__token-value t-data">
                  {metrics.totalTokens.toLocaleString()}
                </strong>
                <dl className="usage-overview__token-grid">
                  <div>
                    <dt>Input</dt>
                    <dd className="t-data">{metrics.input.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt>Output</dt>
                    <dd className="t-data">{metrics.output.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt>Cache read</dt>
                    <dd className="t-data">{metrics.cacheRead.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt>Cache write</dt>
                    <dd className="t-data">{metrics.cacheWrite.toLocaleString()}</dd>
                  </div>
                </dl>
              </div>

              <dl className="usage-overview__supporting">
                <div aria-label="Sessions">
                  <dt>Sessions</dt>
                  <dd className="t-data">{metrics.sessions}</dd>
                  <dd className="usage-overview__supporting-hint">in this window</dd>
                </div>
                <div aria-label="Avg cost / session">
                  <dt>Average / session</dt>
                  <dd className="t-data">
                    {metrics.avgCost != null ? formatCost(metrics.avgCost, { decimals: 4 }) : '—'}
                  </dd>
                  <dd className="usage-overview__supporting-hint">running average</dd>
                </div>
              </dl>
            </div>
          </section>

          <section className="usage-chart" aria-labelledby="usage-chart-title">
            <div className="usage-chart__head">
              <div className="usage-chart__title-wrap">
                <span className="usage-chart__icon" aria-hidden="true">
                  <BarChart3Icon />
                </span>
                <div>
                  <h2 id="usage-chart-title">Session footprint</h2>
                  <p>Compare the highest-consumption sessions in the selected window.</p>
                </div>
              </div>
              <div className="usage-segs" role="group" aria-label="Chart metric">
                <button
                  type="button"
                  className={`usage-seg${chartMode === 'tokens' ? ' is-active' : ''}`}
                  aria-pressed={chartMode === 'tokens'}
                  onClick={() => setChartMode('tokens')}
                >
                  Tokens
                </button>
                <button
                  type="button"
                  className={`usage-seg${chartMode === 'cost' ? ' is-active' : ''}`}
                  aria-pressed={chartMode === 'cost'}
                  onClick={() => setChartMode('cost')}
                >
                  Cost
                </button>
              </div>
            </div>
            <div className="usage-chart__legend">
              <span className="usage-chart__caption" aria-live="polite">
                {chartCaption}
              </span>
              <span className="usage-chart__legend-spacer" />
              <span className="usage-chart__legend-item">
                <span className="usage-chart__swatch usage-chart__swatch--input" />
                Input
              </span>
              {chartMode === 'tokens' ? (
                <span className="usage-chart__legend-item">
                  <span className="usage-chart__swatch usage-chart__swatch--output" />
                  Output
                </span>
              ) : null}
            </div>
            {chart.bars.length === 0 ? (
              <div className="usage-bars__empty">
                <BarChart3Icon className="usage-bars__empty-icon" aria-hidden="true" />
                <strong>No data in the selected window.</strong>
                <span>Choose a wider billing window or run a new session.</span>
              </div>
            ) : (
              <div className="usage-bars" key={`${chartMode}-${range}`}>
                {chart.bars.map((bar, i) => (
                  <button
                    key={bar.key + i}
                    type="button"
                    className="usage-bar-row"
                    title={`Open ${bar.key}`}
                    style={{ '--i': i } as React.CSSProperties}
                    onClick={() => openChat(bar.key)}
                  >
                    <span className="usage-bar-row__rank" aria-hidden="true">
                      {String(i + 1).padStart(2, '0')}
                    </span>
                    <span className="usage-bar-row__label">{bar.label}</span>
                    <span className="usage-bar-row__track" aria-hidden="true">
                      <span
                        className="usage-bar-row__fill usage-bar-row__fill--input"
                        style={{ width: `${bar.inputPct.toFixed(1)}%` }}
                      />
                      {bar.outputPct > 0 ? (
                        <span
                          className="usage-bar-row__fill usage-bar-row__fill--output"
                          style={{ width: `${bar.outputPct.toFixed(1)}%` }}
                        />
                      ) : null}
                    </span>
                    <span className="usage-bar-row__value t-data">{bar.valueLabel}</span>
                    <ArrowUpRightIcon className="usage-bar-row__arrow" aria-hidden="true" />
                  </button>
                ))}
              </div>
            )}
          </section>

          <section className="usage-models">
            <div className="usage-section-head">
              <div>
                <h2 className="usage-section-title">Model allocation</h2>
                <p>Token volume, session reach, and cost contribution by model.</p>
              </div>
              <span className="usage-section-meta t-data">
                {grid.models.length} model{grid.models.length === 1 ? '' : 's'}
              </span>
            </div>
            {grid.models.length === 0 ? (
              <div className="usage-models__empty">No model usage yet.</div>
            ) : (
              <div className="usage-model-grid" key={range} aria-label="By model breakdown">
                {grid.models.map((m, i) => (
                  <article
                    className="usage-model-card"
                    key={m.model + i}
                    style={{ '--i': i } as React.CSSProperties}
                  >
                    <span className="usage-model-card__rank" aria-hidden="true">
                      {String(i + 1).padStart(2, '0')}
                    </span>
                    <div className="usage-model-card__identity">
                      <span className="usage-model-card__icon" aria-hidden="true">
                        <CpuIcon />
                      </span>
                      <div>
                        {m.provider ? (
                          <span className="usage-model-card__provider">{m.provider}</span>
                        ) : null}
                        <h3 className="usage-model-card__name" title={m.model}>
                          {m.name}
                        </h3>
                      </div>
                    </div>
                    <div className="usage-model-card__share" title="Share of total cost">
                      <span className="usage-model-card__share-bar" aria-hidden="true">
                        <span
                          className="usage-model-card__share-fill"
                          style={{ width: `${m.sharePct.toFixed(1)}%` }}
                        />
                      </span>
                      <strong className="t-data">{m.sharePct.toFixed(1)}%</strong>
                      <span>of spend</span>
                    </div>
                    <dl className="usage-model-card__rows">
                      <div>
                        <dt>Tokens</dt>
                        <dd className="t-data">{m.totalTokens.toLocaleString()}</dd>
                      </div>
                      <div>
                        <dt>Input / output</dt>
                        <dd className="t-data">
                          {m.inputTokens.toLocaleString()} / {m.outputTokens.toLocaleString()}
                        </dd>
                      </div>
                      <div>
                        <dt>Sessions</dt>
                        <dd className="t-data">{m.sessions}</dd>
                      </div>
                      <div>
                        <dt>Cost</dt>
                        <dd className="t-data usage-cost">{formatCost(m.costUsd)}</dd>
                      </div>
                    </dl>
                  </article>
                ))}
              </div>
            )}
          </section>

          <section className="usage-sessions">
            <div className="usage-section-head">
              <div>
                <h2 className="usage-section-title">Sessions</h2>
                <p>Auditable usage records with provider billing provenance.</p>
              </div>
              <span className="usage-section-meta t-data">{sessionMeta}</span>
            </div>
            <div className="usage-table-wrap">
              <table className="usage-table">
                <thead>
                  <tr>
                    {TABLE_COLUMNS.map((col) =>
                      col.sortable ? (
                        <th key={col.key} aria-sort={ariaSort(col.key)}>
                          <button
                            type="button"
                            className="usage-th-sort"
                            onClick={() => onSort(col.key)}
                          >
                            {col.label}
                            <span aria-hidden="true">{sortArrow(col.key)}</span>
                          </button>
                        </th>
                      ) : (
                        <th key={col.key}>{col.label}</th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {sorted.length === 0 ? (
                    <tr>
                      <td colSpan={TABLE_COLUMNS.length} className="usage-empty-row">
                        <div className="usage-empty">
                          <BarChart3Icon className="usage-empty__icon" aria-hidden="true" />
                          <div className="usage-empty__title">No usage data yet</div>
                          <p className="usage-empty__msg">
                            Run a session and token spend will appear here automatically.
                          </p>
                        </div>
                      </td>
                    </tr>
                  ) : (
                    sorted.map((row, rowIndex) => {
                      const key = String(
                        rowVal(row as Record<string, unknown>, 'session', 'sessionKey', 'key') ??
                          '',
                      )
                      const ts = sessionTimestamp(row)
                      const badge = costSourceBadge(row as Record<string, unknown>)
                      const modelLabel = modelDisplayLabel(row)
                      const canExpand = hasModelExpand(row)
                      const isOpen = expanded.has(key)
                      return (
                        <ExpandableRow
                          key={key || `row-${rowIndex}`}
                          row={row}
                          sessionKey={key}
                          modified={ts != null ? formatRelTime(ts) : '—'}
                          badge={badge}
                          modelLabel={modelLabel}
                          canExpand={canExpand}
                          isOpen={isOpen}
                          colSpan={TABLE_COLUMNS.length}
                          onOpenChat={() => openChat(key)}
                          onToggle={() => toggleExpand(key)}
                        />
                      )
                    })
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  )
}

function UsageLoading() {
  return (
    <div className="usage-loading" role="status" aria-label="Loading usage data">
      <span className="sr-only">Loading usage data</span>
      <div className="usage-loading__overview" />
      <div className="usage-loading__chart" />
      <div className="usage-loading__rows">
        <span />
        <span />
        <span />
      </div>
    </div>
  )
}

// ── Table row (+ optional inline model-breakdown expansion) ───────────────────
function ExpandableRow({
  row,
  sessionKey,
  modified,
  badge,
  modelLabel,
  canExpand,
  isOpen,
  colSpan,
  onOpenChat,
  onToggle,
}: {
  row: UsageRow
  sessionKey: string
  modified: string
  badge: CostSourceBadge
  modelLabel: string
  canExpand: boolean
  isOpen: boolean
  colSpan: number
  onOpenChat: () => void
  onToggle: () => void
}) {
  return (
    <>
      <tr>
        <td data-label="Session">
          {sessionKey ? (
            <button
              type="button"
              className="usage-sess-link t-data"
              title={`Open chat for ${sessionKey}`}
              onClick={onOpenChat}
            >
              {sessionKey}
            </button>
          ) : (
            '—'
          )}
        </td>
        <td data-label="Modified" className="t-data usage-dim">
          {modified}
        </td>
        <td data-label="Input" className="t-data">
          {localized(num(row, 'input_tokens', 'inputTokens'))}
        </td>
        <td data-label="Output" className="t-data">
          {localized(num(row, 'output_tokens', 'outputTokens'))}
        </td>
        <td data-label="Cache R" className="t-data usage-dim">
          {localized(num(row, 'cache_read_tokens', 'cacheReadTokens'))}
        </td>
        <td data-label="Cache W" className="t-data usage-dim">
          {localized(num(row, 'cache_write_tokens', 'cacheWriteTokens'))}
        </td>
        <td data-label="Cost" className="t-data usage-cost">
          {formatCost(num(row, 'cost_usd', 'costUsd'))}
        </td>
        <td data-label="Source">
          <SourceBadge badge={badge} />
        </td>
        <td data-label="Model">
          {canExpand ? (
            <button
              type="button"
              className={`usage-model-toggle${isOpen ? ' open' : ''}`}
              aria-expanded={isOpen}
              onClick={onToggle}
            >
              <span>{modelLabel}</span>
              <ChevronDownIcon className="usage-model-caret" aria-hidden="true" />
            </button>
          ) : (
            <span className="usage-model-text">{modelLabel}</span>
          )}
        </td>
      </tr>
      {canExpand && isOpen ? (
        <tr className="usage-expand-row">
          <td className="usage-expand-cell" colSpan={colSpan}>
            <ModelExpansion row={row} />
          </td>
        </tr>
      ) : null}
    </>
  )
}

// usage.js:651-724 — the inline per-model breakdown for an expanded session.
function ModelExpansion({ row }: { row: UsageRow }) {
  const ex = sessionExpandRows(row)
  return (
    <div className="usage-expand">
      <div className="usage-expand__head">
        <span className="usage-expand__connector" aria-hidden="true" />
        <span className="usage-expand__eyebrow">Model breakdown</span>
        <span className="usage-expand__count">
          {ex.count} model{ex.count === 1 ? '' : 's'}
        </span>
        <span className="usage-expand__spacer" />
        <span className="usage-expand__total">
          {ex.totalTokens.toLocaleString()} tokens · {formatCost(ex.totalCost)}
        </span>
      </div>
      {ex.anyProrated ? (
        <div className="usage-expand__notice" role="note">
          Per-model split is estimated; total is the actual billed amount.
        </div>
      ) : null}
      <div className="usage-expand__list" role="table" aria-label="Model breakdown">
        {ex.rows.map((m, i) => (
          <div
            className="usage-expand__row"
            role="row"
            key={m.model + i}
            style={{ '--i': i } as React.CSSProperties}
          >
            <div className="usage-expand__model" role="cell" title={m.model}>
              {m.provider ? <span className="usage-expand__provider">{m.provider}/</span> : null}
              <span className="usage-expand__name">{m.name}</span>
            </div>
            <div className="usage-expand__share" role="cell">
              <span className="usage-expand__share-track">
                <span
                  className="usage-expand__share-fill"
                  style={{ width: `${m.sharePct.toFixed(2)}%` }}
                />
              </span>
              <span className="usage-expand__share-pct">{m.sharePct.toFixed(1)}%</span>
            </div>
            <div className="usage-expand__tokens" role="cell">
              {m.tokens.toLocaleString()}
            </div>
            <div className="usage-expand__cost" role="cell">
              {formatCost(m.cost)}
            </div>
            <div className="usage-expand__source" role="cell">
              <SourceBadge badge={m.badge} />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
