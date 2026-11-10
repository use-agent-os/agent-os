export interface MemoryFileRow {
  path: string
  source?: string
  sizeBytes?: number
  lineCount?: number
  modifiedAt?: string
}

export interface CuratedMemoryData {
  agentId: string
  target: 'memory' | 'user'
  entries: string[]
  usage: string
  charCount: number
  charLimit: number
  loadFailed: boolean
}

export interface MemorySearchResultWire {
  chunkId: string
  path: string
  source: string
  startLine?: number
  endLine?: number
  snippet: string
  score: number
}

export interface MemoryStatusWire {
  fileCount?: number
  chunkCount?: number
  sourceCounts?: Record<string, { files?: number; chunks?: number }>
  vecAvailable?: boolean
  ftsAvailable?: boolean
}

export type MemoryTab = 'curated' | 'knowledge_base' | 'raw_sources' | 'search'
export type MemorySourceFilter = 'all' | 'memory' | 'knowledge_base' | 'sessions'

export function formatBytes(bytes?: number): string {
  if (bytes === undefined || bytes === null || Number.isNaN(bytes)) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function formatUsagePercent(used: number, limit: number): number {
  if (limit <= 0) return 0
  return Math.min(100, Math.round((used / limit) * 100))
}

export function isNearBudget(used: number, limit: number, thresholdPercent = 85): boolean {
  if (limit <= 0) return false
  return (used / limit) * 100 >= thresholdPercent
}

export function filterMemoryFiles(
  files: MemoryFileRow[],
  query: string,
  sourceFilter: MemorySourceFilter = 'all',
): MemoryFileRow[] {
  const q = query.trim().toLowerCase()
  return files.filter((f) => {
    if (sourceFilter !== 'all') {
      const src = (f.source || 'memory').toLowerCase()
      if (src !== sourceFilter) return false
    }
    if (!q) return true
    return f.path.toLowerCase().includes(q)
  })
}

export function filterCuratedEntries(entries: string[], query: string): string[] {
  const q = query.trim().toLowerCase()
  if (!q) return entries
  return entries.filter((e) => e.toLowerCase().includes(q))
}

export function parseCuratedEntries(markdown: string): string[] {
  if (!markdown || !markdown.trim()) return []
  return markdown
    .split(/\n§\n|\n§(?:\r?\n|$)/)
    .map((e) => e.trim())
    .filter(Boolean)
}

export function buildCuratedMarkdown(entries: string[]): string {
  return (
    entries
      .map((e) => e.trim())
      .filter(Boolean)
      .join('\n§\n') + (entries.length > 0 ? '\n' : '')
  )
}

export function formatScore(score: number): string {
  return (score * 100).toFixed(0) + '%'
}
