// Pure usage-view helpers ported 1:1 from the legacy view
// (src/agentos/gateway/static/js/views/usage.js). Each function carries the
// legacy line range it mirrors so the parity matrix stays auditable. RPC
// calls, polling, DOM/render and event wiring live in UsagePage.tsx; this
// module owns the pure derivations (range filtering, aggregation, formatting,
// sort keys, chart bar-scaling math, cost-source mapping, model grouping, CSV).

/** A per-model breakdown entry (usage.status sessions[].modelBreakdown[]).
 * Backend emits camelCase here; snake variants tolerated defensively. */
import { t, tPlural } from '@/i18n'
import '@/i18n/en/usage'

export interface ModelBreakdownEntry {
  model?: string
  inputTokens?: number
  outputTokens?: number
  cacheReadTokens?: number
  cacheWriteTokens?: number
  costUsd?: number
  costSource?: string
  cost_source?: string
  [key: string]: unknown
}

/** A raw usage row from usage.status.sessions (all fields optional; both
 * snake_case and camelCase variants appear across backend/CLI payloads). */
export interface UsageRow {
  session?: string
  sessionKey?: string
  key?: string
  model?: string
  input_tokens?: number | string | null
  inputTokens?: number | string | null
  output_tokens?: number | string | null
  outputTokens?: number | string | null
  cache_read_tokens?: number | string | null
  cacheReadTokens?: number | string | null
  cache_write_tokens?: number | string | null
  cacheWriteTokens?: number | string | null
  cost_usd?: number | string | null
  costUsd?: number | string | null
  billed_cost_usd?: number | string | null
  billedCostUsd?: number | string | null
  estimated_cost_usd?: number | string | null
  estimatedCostUsd?: number | string | null
  cost_source?: string
  costSource?: string
  cost_ephemeral?: boolean
  costEphemeral?: boolean
  missing_cost_entries?: number | string | null
  missingCostEntries?: number | string | null
  endedAt?: number | string
  ended_at?: number | string
  updatedAt?: number | string
  updated_at?: number | string
  startedAt?: number | string
  started_at?: number | string
  createdAt?: number | string
  created_at?: number | string
  modelBreakdown?: ModelBreakdownEntry[]
  [key: string]: unknown
}

export type UsageRange = 'all' | '7' | '14' | '30'
export type ChartMode = 'tokens' | 'cost'

const DAY_MS = 86_400_000

// ── Range ────────────────────────────────────────────────────────────────────

/** — validate a range string to {all,7,14,30}; default 7. */
export function normalizeRange(range: unknown): UsageRange {
  const value = String(range ?? '7')
  return (['all', '7', '14', '30'] as const).includes(value as UsageRange)
    ? (value as UsageRange)
    : '7'
}

// ── Row-value accessors ───────────────────────────────────

/** — first non-null value across candidate keys. */
export function rowVal(row: Record<string, unknown>, ...keys: string[]): unknown {
  for (const key of keys) {
    const v = row[key]
    if (v != null) return v
  }
  return null
}

/** — numeric row value (finite), else null. */
function numericRowVal(row: Record<string, unknown>, ...keys: string[]): number | null {
  const value = rowVal(row, ...keys)
  if (value == null || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

/** Coerce a candidate row value to a number, treating null/'' as 0. */
function num(row: Record<string, unknown>, ...keys: string[]): number {
  return Number(rowVal(row, ...keys) ?? 0) || 0
}

/** — first numeric timestamp across ended/updated/started/
 * created (snake + camel), else null. */
export function sessionTimestamp(row: UsageRow): number | null {
  for (const key of [
    'endedAt',
    'ended_at',
    'updatedAt',
    'updated_at',
    'startedAt',
    'started_at',
    'createdAt',
    'created_at',
  ]) {
    const value = numericRowVal(row as Record<string, unknown>, key)
    if (value != null) return value
  }
  return null
}

/** — the cutoff timestamp (ms) for a range, or null for all. */
export function rangeCutoffMs(range: UsageRange): number | null {
  if (range === 'all') return null
  return Date.now() - Number(range) * DAY_MS
}

/** — rows whose timestamp is within the range window. `all`
 * (or a range with no cutoff) returns the input list; undated rows drop out of
 * a dated window. A row is in range when the session was last active in it:
 * its figures are still the session's lifetime totals. */
export function visibleSessions(rows: UsageRow[], range: UsageRange): UsageRow[] {
  const cutoff = rangeCutoffMs(range)
  if (cutoff == null) return rows
  return rows.filter((row) => {
    const timestamp = timestampMs(row)
    return timestamp != null && timestamp >= cutoff
  })
}

/** Local midnight (ms) of the day `now` falls in: the user's day, not UTC's. */
export function startOfLocalDay(now: number = Date.now()): number {
  const d = new Date(now)
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

/** A row timestamp in ms; epoch seconds (below 1e10) are scaled up. */
function timestampMs(row: UsageRow): number | null {
  const ts = sessionTimestamp(row)
  if (ts == null) return null
  return ts < 10_000_000_000 ? ts * 1000 : ts
}

/**
 * How many rows were last active at or after `sinceMs`. Usage rows carry each
 * session's lifetime totals, so this counts sessions; it cannot say how much
 * of their usage fell after `sinceMs`.
 */
export function activeSinceCount(rows: UsageRow[], sinceMs: number): number {
  return rows.filter((row) => {
    const ts = timestampMs(row)
    return ts != null && ts >= sinceMs
  }).length
}

/** — count of undated rows hidden by a dated range (0 on all). */
export function undatedHiddenCount(rows: UsageRow[], range: UsageRange): number {
  if (range === 'all') return 0
  return rows.filter((row) => sessionTimestamp(row) == null).length
}

/** — "N undated legacy session(s) hidden" or '' when none. */
export function rangeHiddenHint(rows: UsageRow[], range: UsageRange): string {
  const hidden = undatedHiddenCount(rows, range)
  if (hidden <= 0) return ''
  return tPlural('usage.hiddenSessions', hidden)
}

// ── Formatting ────────────────────

/** — "$"+toFixed(decimals) (default 4); null → em dash. */
export function formatCost(usd: number | null | undefined, opts?: { decimals?: number }): string {
  if (usd == null) return t('common.dash')
  const decimals = opts?.decimals != null ? opts.decimals : 4
  return '$' + Number(usd).toFixed(decimals)
}

/** — abbreviate a token count (M/K one-decimal); null → dash. */
export function formatNum(n: number | null | undefined): string {
  if (n == null) return t('common.dash')
  const v = Number(n)
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M'
  if (v >= 1_000) return (v / 1_000).toFixed(1) + 'K'
  return String(v)
}

/** (UI.relTime) — relative time. Numeric input is an
 * epoch (seconds when < 1e10, else millis); strings parse as a numeric epoch
 * or ISO. Invalid → "—". */
export function formatRelTime(isoOrTs: string | number): string {
  const numeric =
    typeof isoOrTs === 'number'
      ? isoOrTs
      : typeof isoOrTs === 'string' && isoOrTs.trim() !== ''
        ? Number(isoOrTs)
        : NaN
  const d = Number.isFinite(numeric)
    ? new Date(Math.abs(numeric) < 10_000_000_000 ? numeric * 1000 : numeric)
    : new Date(isoOrTs)
  if (Number.isNaN(d.getTime())) return t('common.dash')
  const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 60) return t('usage.relJustNow')
  if (diff < 3600) return t('usage.relMinutes', { count: Math.floor(diff / 60) })
  if (diff < 86_400) return t('usage.relHours', { count: Math.floor(diff / 3600) })
  return t('usage.relDays', { count: Math.floor(diff / 86_400) })
}

// ── Metrics ───────────────────────────────────────

export interface UsageMetrics {
  input: number
  output: number
  totalTokens: number
  cost: number
  cacheRead: number
  cacheWrite: number
  sessions: number
  /** cost / sessions, or null when there are no sessions. */
  avgCost: number | null
}

/** — sum input/output/cost/cache across rows and the
 * running average cost per session. */
export function usageMetrics(rows: UsageRow[]): UsageMetrics {
  const acc = { input: 0, output: 0, cost: 0, cacheRead: 0, cacheWrite: 0 }
  for (const row of rows) {
    const r = row as Record<string, unknown>
    acc.input += num(r, 'input_tokens', 'inputTokens')
    acc.output += num(r, 'output_tokens', 'outputTokens')
    acc.cost += num(r, 'cost_usd', 'costUsd')
    acc.cacheRead += num(r, 'cache_read_tokens', 'cacheReadTokens')
    acc.cacheWrite += num(r, 'cache_write_tokens', 'cacheWriteTokens')
  }
  const sessions = rows.length
  return {
    input: acc.input,
    output: acc.output,
    totalTokens: acc.input + acc.output,
    cost: acc.cost,
    cacheRead: acc.cacheRead,
    cacheWrite: acc.cacheWrite,
    sessions,
    avgCost: sessions > 0 ? acc.cost / sessions : null,
  }
}

// ── Cost-source badge ─────────────────────────────────────

const KNOWN_COST_SOURCES = [
  'provider_billed',
  'provider_billed_prorated',
  'agentos_estimate',
  'mixed',
  'unavailable',
  'none',
]

/** — the (string) cost source of a row, default 'none'. */
export function costSource(row: Record<string, unknown>): string {
  return String(rowVal(row, 'cost_source', 'costSource') ?? 'none')
}

/** — the CSS class suffix for a source (unknown → 'none'). */
function costSourceClass(source: string): string {
  return KNOWN_COST_SOURCES.includes(source) ? source : 'none'
}

/** The locale-independent identity of a cost source; ephemeral wins. */
type CostSourceToken = 'ephemeral' | 'actual' | 'estimated' | 'mixed' | 'unpriced' | 'none'

function costSourceToken(source: string, ephemeral: boolean): CostSourceToken {
  if (ephemeral) return 'ephemeral'
  switch (source) {
    case 'provider_billed':
      return 'actual'
    case 'provider_billed_prorated':
      return 'actual'
    case 'agentos_estimate':
      return 'estimated'
    case 'mixed':
      return 'mixed'
    case 'unavailable':
      return 'unpriced'
    default:
      return 'none'
  }
}

/** — the human label for a source; ephemeral wins. */
function costSourceLabel(source: string, ephemeral: boolean): string {
  const labels: Record<CostSourceToken, string> = {
    ephemeral: t('usage.sourceEphemeral'),
    actual: t('usage.sourceActual'),
    estimated: t('usage.sourceEstimated'),
    mixed: t('usage.sourceMixed'),
    unpriced: t('usage.sourceUnpriced'),
    none: t('usage.sourceNone'),
  }
  return labels[costSourceToken(source, ephemeral)]
}

/** — the tooltip for a source; ephemeral wins. */
function costSourceTooltip(source: string, ephemeral: boolean): string {
  if (ephemeral) return t('usage.tipEphemeral')
  switch (source) {
    case 'provider_billed':
      return t('usage.tipActual')
    case 'provider_billed_prorated':
      return t('usage.tipProrated')
    case 'agentos_estimate':
      return t('usage.tipEstimated')
    case 'mixed':
      return t('usage.tipMixed')
    case 'unavailable':
      return t('usage.tipUnpriced')
    default:
      return t('usage.tipNone')
  }
}

export interface CostSourceBadge {
  label: string
  tooltip: string
  cls: string
  ephemeral: boolean
}

/** — the rendered cost-source badge descriptor for a row (or a
 * per-model breakdown entry). */
export function costSourceBadge(row: Record<string, unknown>): CostSourceBadge {
  const source = costSource(row)
  const ephemeral = Boolean(rowVal(row, 'cost_ephemeral', 'costEphemeral'))
  return {
    label: costSourceLabel(source, ephemeral),
    tooltip: costSourceTooltip(source, ephemeral),
    cls: costSourceClass(source),
    ephemeral,
  }
}

/** — the cost-composition hint ("actual 2 · estimated 1 · …")
 * over the given rows; only the five counted labels contribute. */
export function sourceCompositionHint(rows: UsageRow[]): string {
  // Counted by token, not by the rendered label: keying on English wording
  // would stop matching the moment the badge label is translated.
  const counts: Record<string, number> = {
    actual: 0,
    estimated: 0,
    mixed: 0,
    unpriced: 0,
    ephemeral: 0,
  }
  rows.forEach((row) => {
    const r = row as Record<string, unknown>
    const token = costSourceToken(
      costSource(r),
      Boolean(rowVal(r, 'cost_ephemeral', 'costEphemeral')),
    )
    if (counts[token] != null) counts[token] += 1
  })
  const words: Record<string, string> = {
    actual: t('usage.compositionActual'),
    estimated: t('usage.compositionEstimated'),
    mixed: t('usage.compositionMixed'),
    unpriced: t('usage.compositionUnpriced'),
    ephemeral: t('usage.compositionEphemeral'),
  }
  return Object.entries(counts)
    .filter(([, n]) => n > 0)
    .map(([token, n]) => t('usage.compositionEntry', { label: words[token]!, count: n }))
    .join(' · ')
}

// ── Sorting ───────────────────────────────────────

export type SortColumn =
  | 'session'
  | 'updated_at'
  | 'input_tokens'
  | 'output_tokens'
  | 'cache_read_tokens'
  | 'cache_write_tokens'
  | 'cost_usd'
  | 'cost_source'
  | 'model'

/** — the comparable value for a row + sort column. */
function sortVal(row: UsageRow, key: SortColumn): number | string {
  const r = row as Record<string, unknown>
  switch (key) {
    case 'session':
      return String(rowVal(r, 'session', 'sessionKey', 'key') ?? '')
    case 'updated_at':
      return sessionTimestamp(row) ?? 0
    case 'input_tokens':
      return num(r, 'input_tokens', 'inputTokens')
    case 'output_tokens':
      return num(r, 'output_tokens', 'outputTokens')
    case 'cache_read_tokens':
      return num(r, 'cache_read_tokens', 'cacheReadTokens')
    case 'cache_write_tokens':
      return num(r, 'cache_write_tokens', 'cacheWriteTokens')
    case 'cost_usd':
      return num(r, 'cost_usd', 'costUsd')
    default:
      return String(rowVal(r, key) ?? '')
  }
}

/** — sort a copy of the rows by a column; string values fold
 * to lowercase; `asc` toggles direction. Never mutates the input. */
export function sortSessions(rows: UsageRow[], column: SortColumn, asc: boolean): UsageRow[] {
  return [...rows].sort((a, b) => {
    let va = sortVal(a, column)
    let vb = sortVal(b, column)
    if (typeof va === 'string') va = va.toLowerCase()
    if (typeof vb === 'string') vb = vb.toLowerCase()
    const cmp = va < vb ? -1 : va > vb ? 1 : 0
    return asc ? cmp : -cmp
  })
}

// ── Chart ─────────────────────────────────────────────────

export interface ChartBar {
  key: string
  label: string
  inputPct: number
  outputPct: number
  totalPct: number
  valueLabel: string
}

export interface ChartData {
  bars: ChartBar[]
  max: number
  /** — size of the non-zero-token pool the chart draws from. */
  poolSize: number
  /** min(20, poolSize) — bars actually shown. */
  shown: number
}

function rowTotalTokens(row: UsageRow): number {
  const r = row as Record<string, unknown>
  return num(r, 'input_tokens', 'inputTokens') + num(r, 'output_tokens', 'outputTokens')
}

/** — the chart bars for a mode. Zero-token rows are dropped,
 * the rest sorted (total tokens or cost, desc) and capped at 20; percentages
 * scale to the max (0 → 1 to avoid divide-by-zero). */
export function chartRows(rows: UsageRow[], mode: ChartMode): ChartData {
  const pool = rows.filter((r) => rowTotalTokens(r) > 0)
  const sorted = [...pool].sort((a, b) => {
    if (mode === 'cost') {
      return (
        num(b as Record<string, unknown>, 'cost_usd', 'costUsd') -
        num(a as Record<string, unknown>, 'cost_usd', 'costUsd')
      )
    }
    return rowTotalTokens(b) - rowTotalTokens(a)
  })
  const top = sorted.slice(0, 20)

  let max = 0
  if (mode === 'cost') {
    max = Math.max(0, ...top.map((r) => num(r as Record<string, unknown>, 'cost_usd', 'costUsd')))
  } else {
    max = Math.max(0, ...top.map((r) => rowTotalTokens(r)))
  }
  if (max === 0) max = 1

  const bars: ChartBar[] = top.map((row) => {
    const r = row as Record<string, unknown>
    const fullLabel = String(rowVal(r, 'session', 'sessionKey', 'key') ?? '—')
    const label = fullLabel.length > 26 ? fullLabel.slice(0, 24) + '…' : fullLabel
    let valueLabel: string
    let inputPct: number
    let outputPct: number
    let totalPct: number
    if (mode === 'cost') {
      const cost = num(r, 'cost_usd', 'costUsd')
      const pct = (cost / max) * 100
      inputPct = pct
      outputPct = 0
      totalPct = pct
      valueLabel = formatCost(cost)
    } else {
      const inp = num(r, 'input_tokens', 'inputTokens')
      const out = num(r, 'output_tokens', 'outputTokens')
      inputPct = (inp / max) * 100
      outputPct = (out / max) * 100
      totalPct = inputPct + outputPct
      valueLabel = formatNum(inp + out)
    }
    return { key: fullLabel, label, inputPct, outputPct, totalPct, valueLabel }
  })

  return { bars, max, poolSize: pool.length, shown: Math.min(20, pool.length) }
}

// ── Model cell + expand ───────────────────────────────────

/** — the model-cell label: "auto · N models" for a multi-model
 * breakdown, else the single model / row model / em dash. */
export function modelDisplayLabel(row: UsageRow): string {
  const bd = row.modelBreakdown
  if (Array.isArray(bd) && bd.length > 0) {
    return bd.length > 1 ? `auto · ${bd.length} models` : (bd[0]?.model ?? row.model ?? '—')
  }
  return row.model ?? '—'
}

/** — whether the model cell should show an expand toggle. */
export function hasModelExpand(row: UsageRow): boolean {
  const bd = row.modelBreakdown
  return Array.isArray(bd) && bd.length > 1
}

export interface ExpandModelRow {
  model: string
  provider: string
  name: string
  tokens: number
  cost: number
  sharePct: number
  badge: CostSourceBadge
}

export interface ExpandData {
  count: number
  totalTokens: number
  totalCost: number
  anyProrated: boolean
  rows: ExpandModelRow[]
}

/** — the expanded per-model breakdown for a row: totals, a
 * prorated flag, and per-model provider/name split with cost share. */
export function sessionExpandRows(row: UsageRow): ExpandData {
  const bd = row.modelBreakdown ?? []
  const totalCost = bd.reduce((acc, m) => acc + (Number(m.costUsd) || 0), 0)
  const totalTokens = bd.reduce(
    (acc, m) => acc + (Number(m.inputTokens) || 0) + (Number(m.outputTokens) || 0),
    0,
  )
  const anyProrated = bd.some((m) => {
    const src = String(m.costSource ?? m.cost_source ?? '')
    return src === 'provider_billed_prorated'
  })
  const rows: ExpandModelRow[] = bd.map((m) => {
    const tokens = (Number(m.inputTokens) || 0) + (Number(m.outputTokens) || 0)
    const cost = Number(m.costUsd) || 0
    const sharePct = totalCost > 0 ? (cost / totalCost) * 100 : 0
    const provider = (m.model ?? '').split('/')[0] ?? ''
    const name = (m.model ?? '').split('/').slice(1).join('/') || m.model || 'unknown'
    return {
      model: m.model ?? '',
      provider,
      name,
      tokens,
      cost,
      sharePct,
      badge: costSourceBadge(m),
    }
  })
  return { count: bd.length, totalTokens, totalCost, anyProrated, rows }
}

// ── By-model breakdown grid ───────────────────────────────

export interface ModelCard {
  model: string
  provider: string
  name: string
  inputTokens: number
  outputTokens: number
  cacheReadTokens: number
  cacheWriteTokens: number
  totalTokens: number
  costUsd: number
  sessions: number
  sharePct: number
}

export interface ModelGrid {
  models: ModelCard[]
  totalCost: number
}

/** — group the visible rows by per-model usage (breakdown when
 * present, else the row's single model), sum tokens/cache/cost + distinct
 * sessions, sort by cost desc, and attach a per-card cost share. */
export function modelBreakdownGrid(rows: UsageRow[]): ModelGrid {
  const map: Record<string, Omit<ModelCard, 'provider' | 'name' | 'totalTokens' | 'sharePct'>> = {}
  rows.forEach((row) => {
    const r = row as Record<string, unknown>
    const breakdown = Array.isArray(row.modelBreakdown) ? row.modelBreakdown : []
    const items: ModelBreakdownEntry[] =
      breakdown.length > 0
        ? breakdown
        : [
            {
              model: row.model ?? 'unknown',
              inputTokens: num(r, 'input_tokens', 'inputTokens'),
              outputTokens: num(r, 'output_tokens', 'outputTokens'),
              cacheReadTokens: num(r, 'cache_read_tokens', 'cacheReadTokens'),
              cacheWriteTokens: num(r, 'cache_write_tokens', 'cacheWriteTokens'),
              costUsd: num(r, 'cost_usd', 'costUsd'),
            },
          ]
    const modelsSeenInSession = new Set<string>()
    items.forEach((item) => {
      const it = item as Record<string, unknown>
      const model = item.model ?? row.model ?? 'unknown'
      if (!map[model]) {
        map[model] = {
          model,
          inputTokens: 0,
          outputTokens: 0,
          cacheReadTokens: 0,
          cacheWriteTokens: 0,
          costUsd: 0,
          sessions: 0,
        }
      }
      const bucket = map[model]!
      bucket.inputTokens += num(it, 'input_tokens', 'inputTokens')
      bucket.outputTokens += num(it, 'output_tokens', 'outputTokens')
      bucket.cacheReadTokens += num(it, 'cache_read_tokens', 'cacheReadTokens')
      bucket.cacheWriteTokens += num(it, 'cache_write_tokens', 'cacheWriteTokens')
      bucket.costUsd += num(it, 'cost_usd', 'costUsd')
      if (!modelsSeenInSession.has(model)) {
        bucket.sessions += 1
        modelsSeenInSession.add(model)
      }
    })
  })

  const sorted = Object.values(map).sort((a, b) => b.costUsd - a.costUsd)
  const totalCost = sorted.reduce((acc, m) => acc + m.costUsd, 0)
  const models: ModelCard[] = sorted.map((m) => {
    const provider = (m.model || '').split('/')[0] ?? ''
    const name = (m.model || '').split('/').slice(1).join('/') || m.model || 'unknown'
    return {
      ...m,
      provider,
      name,
      totalTokens: m.inputTokens + m.outputTokens,
      sharePct: totalCost > 0 ? (m.costUsd / totalCost) * 100 : 0,
    }
  })
  return { models, totalCost }
}

// ── CSV export ────────────────────────────────────────────

const CSV_HEADERS = [
  'session',
  'input_tokens',
  'output_tokens',
  'cache_read_tokens',
  'cache_write_tokens',
  'cost_usd',
  'billed_cost_usd',
  'estimated_cost_usd',
  'cost_source',
  'missing_cost_entries',
  'cost_ephemeral',
  'model',
]

function csvCell(v: unknown): string {
  return '"' + String(v).replace(/"/g, '""') + '"'
}

/** — build the CSV text for the given (visible) rows. */
export function buildCsv(rows: UsageRow[]): string {
  const body = rows.map((row) => {
    const r = row as Record<string, unknown>
    const cost = rowVal(r, 'cost_usd', 'costUsd')
    const billed = rowVal(r, 'billed_cost_usd', 'billedCostUsd')
    const estimated = rowVal(r, 'estimated_cost_usd', 'estimatedCostUsd')
    return [
      rowVal(r, 'session', 'sessionKey', 'key') ?? '',
      rowVal(r, 'input_tokens', 'inputTokens') ?? '',
      rowVal(r, 'output_tokens', 'outputTokens') ?? '',
      rowVal(r, 'cache_read_tokens', 'cacheReadTokens') ?? '',
      rowVal(r, 'cache_write_tokens', 'cacheWriteTokens') ?? '',
      cost != null ? Number(cost).toFixed(6) : '',
      billed != null ? Number(billed).toFixed(6) : '',
      estimated != null ? Number(estimated).toFixed(6) : '',
      costSource(r),
      rowVal(r, 'missing_cost_entries', 'missingCostEntries') ?? '',
      rowVal(r, 'cost_ephemeral', 'costEphemeral') ? 'true' : 'false',
      row.model ?? '',
    ]
  })
  return [CSV_HEADERS, ...body].map((line) => line.map(csvCell).join(',')).join('\n')
}

/** — the CSV filename for a range (`all` or `<N>d`). */
export function csvFilename(range: UsageRange): string {
  const suffix = range === 'all' ? 'all' : `${range}d`
  return `agentos-usage-${suffix}.csv`
}
