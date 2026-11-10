// Pure logs helpers, first ported 1:1 from the retired console's logs.js view
// (the line ranges cited below point into that source). The RPC polling
// cadence and rendering live in the web console's Settings › Logs section
// (web/features/settings/sections/logs.tsx); this module owns the pure
// derivations (level guessing, entry normalization, filtering, counts, ts
// slicing, highlight segmentation, export text).

export type Level = 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR'

export interface LogLine {
  level: Level
  message: string
  ts?: string | number
  raw: string
}

// filter chips, in legacy order.
export const LEVELS: ReadonlyArray<Level> = ['TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR']

// gateway file logging defaults to DEBUG, so TRACE is the only
// level hidden by default.
export const DEFAULT_LEVELS: ReadonlySet<Level> = new Set<Level>(['DEBUG', 'INFO', 'WARN', 'ERROR'])

/** — substring scan ERROR>WARN>INFO>DEBUG>TRACE else INFO. */
export function guessLevel(line: string): Level {
  const u = String(line).toUpperCase()
  if (u.includes('ERROR')) return 'ERROR'
  if (u.includes('WARN')) return 'WARN'
  if (u.includes('INFO')) return 'INFO'
  if (u.includes('DEBUG')) return 'DEBUG'
  if (u.includes('TRACE')) return 'TRACE'
  return 'INFO'
}

/** — response line key fallback: data.lines ?? data.entries ?? []. */
export function extractLines(data: unknown): unknown[] {
  if (!data || typeof data !== 'object') return []
  const record = data as { lines?: unknown; entries?: unknown }
  if (Array.isArray(record.lines)) return record.lines
  if (Array.isArray(record.entries)) return record.entries
  return []
}

/** — a raw tail entry (string or object) -> a LogLine. */
export function normalizeEntry(entry: unknown): LogLine {
  if (typeof entry === 'string') {
    return { level: guessLevel(entry), message: entry, raw: entry }
  }
  const record = (entry ?? {}) as Record<string, unknown>
  const level = String(record.level ?? record.lvl ?? 'INFO').toUpperCase() as Level
  const message =
    typeof record.message === 'string'
      ? record.message
      : typeof record.msg === 'string'
        ? record.msg
        : JSON.stringify(entry)
  const rawTs = record.timestamp ?? record.ts
  const ts = typeof rawTs === 'string' || typeof rawTs === 'number' ? rawTs : undefined
  const raw = typeof record.raw === 'string' ? record.raw : JSON.stringify(entry)
  const line: LogLine = { level, message, raw }
  if (ts !== undefined) line.ts = ts
  return line
}

/** — keep only the last 2000 accumulated lines. */
export function clampBuffer(lines: LogLine[]): LogLine[] {
  return lines.length > 2000 ? lines.slice(lines.length - 2000) : lines
}

/** — a line passes when its level is active and (no search or
 * the search term is a case-insensitive substring of the message). */
export function matchesFilter(
  line: LogLine,
  activeLevels: ReadonlySet<Level>,
  search: string,
): boolean {
  if (!activeLevels.has(line.level)) return false
  if (search && !line.message.toLowerCase().includes(search.toLowerCase())) return false
  return true
}

/** — filter the buffer by active levels + search. */
export function filterLines(
  lines: LogLine[],
  activeLevels: ReadonlySet<Level>,
  search: string,
): LogLine[] {
  return lines.filter((line) => matchesFilter(line, activeLevels, search))
}

export interface LevelCounts {
  total: number
  errors: number
  warns: number
  infos: number
  debug: number
}

/** — level tallies for the stat row (TRACE folds into debug). */
export function countByLevel(lines: LogLine[]): LevelCounts {
  return {
    total: lines.length,
    errors: lines.filter((l) => l.level === 'ERROR').length,
    warns: lines.filter((l) => l.level === 'WARN').length,
    infos: lines.filter((l) => l.level === 'INFO').length,
    debug: lines.filter((l) => l.level === 'DEBUG' || l.level === 'TRACE').length,
  }
}

/** — the filtered ("in view") line count. */
export function visibleCount(
  lines: LogLine[],
  activeLevels: ReadonlySet<Level>,
  search: string,
): number {
  return filterLines(lines, activeLevels, search).length
}

/** — a timestamp sliced to its first 23 chars, "" when absent. */
export function sliceTs(ts: string | number | null | undefined): string {
  if (ts === null || ts === undefined) return ''
  return String(ts).slice(0, 23)
}

export interface HighlightSegment {
  text: string
  match: boolean
}

/** — split a message into text/match segments around a
 * case-insensitive, regex-escaped search term (empty term -> one text seg). */
export function splitHighlight(message: string, search: string): HighlightSegment[] {
  if (!search) return [{ text: message, match: false }]
  const term = search.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const re = new RegExp(`(${term})`, 'gi')
  const segments: HighlightSegment[] = []
  let lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(message)) !== null) {
    if (m.index > lastIndex) {
      segments.push({ text: message.slice(lastIndex, m.index), match: false })
    }
    segments.push({ text: m[0], match: true })
    lastIndex = m.index + m[0].length
    // Zero-width matches can't happen here (the term is non-empty), but guard
    // the lastIndex against a stalled regex just in case.
    if (m.index === re.lastIndex) re.lastIndex++
  }
  if (lastIndex < message.length) {
    segments.push({ text: message.slice(lastIndex), match: false })
  }
  return segments.length ? segments : [{ text: message, match: false }]
}

/** — one export line per filtered entry: "<ts> [LEVEL] msg". */
export function buildExportText(lines: LogLine[]): string {
  return lines
    .map((line) => {
      const ts = line.ts ? sliceTs(line.ts) + ' ' : ''
      return `${ts}[${line.level}] ${line.message}`
    })
    .join('\n')
}
