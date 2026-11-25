import type { MessageKey } from '~/i18n'

/** The gateway's read-only workspace browser (`agentos.gateway.rpc_workspace`). */
export const LIST_METHOD = 'workspace.files.list'
export const READ_METHOD = 'workspace.files.read'
export const DOWNLOAD_METHOD = 'workspace.files.download'

/** Query-key root for everything the browser reads; Refresh invalidates it. */
export const FILES_QUERY_ROOT = 'workspace-files'

export type FileKind = 'markdown' | 'text' | 'code' | 'image' | 'pdf' | 'other'
export type EntryType = 'file' | 'directory' | 'symlink'

export interface FileEntry {
  name: string
  /** Relative to the workspace root, `/`-separated. */
  path: string
  type: EntryType
  kind?: FileKind
  language?: string
  size?: number
  mtimeMs?: number
  openable: boolean
  /** Why it cannot be opened: `symlink`, `hardlink`. */
  reason?: string
}

export interface Listing {
  path: string
  entries: FileEntry[]
  truncated: boolean
  total: number
  exists: boolean
}

export interface ReadResult {
  path: string
  name: string
  kind: FileKind
  language?: string
  size: number
  mtimeMs: number
  previewable: boolean
  truncated: boolean
  content: string
  /** Why there is no content: `binary`, `download`. */
  reason?: string
}

export interface TicketResult {
  url: string
  kind: FileKind
  name: string
  mime: string
  size: number
}

const KINDS: ReadonlySet<string> = new Set(['markdown', 'text', 'code', 'image', 'pdf', 'other'])
const TYPES: ReadonlySet<string> = new Set(['file', 'directory', 'symlink'])

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}
function num(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : undefined
}

/**
 * One entry off the wire, or null when it is not one. A name or a path the
 * gateway would never send (empty, or a path that is not the name under its
 * directory) is dropped rather than rendered.
 */
export function normalizeEntry(raw: unknown, parent: string): FileEntry | null {
  if (!raw || typeof raw !== 'object') return null
  const row = raw as Record<string, unknown>
  const name = str(row.name)
  const type = str(row.type)
  if (!name || name.includes('/') || !TYPES.has(type)) return null
  const path = parent ? `${parent}/${name}` : name
  if (str(row.path) !== path) return null
  const kind = str(row.kind)
  return {
    name,
    path,
    type: type as EntryType,
    kind: KINDS.has(kind) ? (kind as FileKind) : undefined,
    language: str(row.language) || undefined,
    size: num(row.size),
    mtimeMs: num(row.mtimeMs),
    openable: row.openable === true,
    reason: str(row.reason) || undefined,
  }
}

export function normalizeListing(raw: unknown, path: string): Listing {
  const body = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const rows = Array.isArray(body.entries) ? body.entries : []
  const entries = rows
    .map((row) => normalizeEntry(row, path))
    .filter((entry): entry is FileEntry => entry !== null)
  return {
    path,
    entries,
    truncated: body.truncated === true,
    total: num(body.total) ?? entries.length,
    exists: body.exists !== false,
  }
}

export function normalizeRead(raw: unknown, path: string): ReadResult {
  const body = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const kind = str(body.kind)
  return {
    path,
    name: baseName(path),
    kind: KINDS.has(kind) ? (kind as FileKind) : 'other',
    language: str(body.language) || undefined,
    size: num(body.size) ?? 0,
    mtimeMs: num(body.mtimeMs) ?? 0,
    previewable: body.previewable === true,
    truncated: body.truncated === true,
    content: str(body.content),
    reason: str(body.reason) || undefined,
  }
}

export function normalizeTicket(raw: unknown): TicketResult | null {
  const body = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const url = str(body.url)
  // Only the gateway's own ticket route: never a URL to somewhere else.
  if (!/^\/api\/v1\/artifacts\/download\/[^/?#]+\/[A-Za-z0-9_-]+$/.test(url)) return null
  const kind = str(body.kind)
  return {
    url,
    kind: KINDS.has(kind) ? (kind as FileKind) : 'other',
    name: str(body.name),
    mime: str(body.mime),
    size: num(body.size) ?? 0,
  }
}

/* ── Paths ─────────────────────────────────────────────────────────────── */

export function baseName(path: string): string {
  const cut = path.lastIndexOf('/')
  return cut < 0 ? path : path.slice(cut + 1)
}

export function parentPath(path: string): string {
  const cut = path.lastIndexOf('/')
  return cut < 0 ? '' : path.slice(0, cut)
}

/** Every directory above `path`, nearest last: `a/b/c.md` → `['a', 'a/b']`. */
export function ancestors(path: string): string[] {
  const parts = path.split('/').filter(Boolean)
  const out: string[] = []
  for (let i = 1; i < parts.length; i += 1) out.push(parts.slice(0, i).join('/'))
  return out
}

/* ── Filtering ─────────────────────────────────────────────────────────── */

/**
 * The entries of one directory to show under a filter. A file shows when its
 * name matches. A directory shows when its name matches or when it is open,
 * so a match inside a folder the user opened is never hidden by its parent.
 */
export function filterEntries(
  entries: readonly FileEntry[],
  query: string,
  expanded: ReadonlySet<string>,
): FileEntry[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return [...entries]
  return entries.filter(
    (entry) =>
      entry.name.toLowerCase().includes(needle) ||
      (entry.type === 'directory' && expanded.has(entry.path)),
  )
}

/* ── Formatting ────────────────────────────────────────────────────────── */

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'] as const

/** `1536` → `1.5 KB`. Binary steps, one decimal below 10 of a unit. */
export function formatBytes(bytes: number | undefined): string {
  if (bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return ''
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  if (unit === 0) return `${Math.round(value)} B`
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${UNITS[unit]}`
}

export function formatModified(mtimeMs: number | undefined, locale?: string): string {
  if (!mtimeMs) return ''
  try {
    return new Date(mtimeMs).toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'short' })
  } catch {
    return ''
  }
}

/* ── Errors ────────────────────────────────────────────────────────────── */

export type FailureKind =
  'outdated' | 'denied' | 'unsafe' | 'not_found' | 'too_large' | 'unsupported' | 'failed'

/**
 * What went wrong, from an RPC rejection. `outdated` is a gateway from before
 * the file browser: it answers METHOD_NOT_FOUND.
 */
export function failureKind(error: unknown): FailureKind {
  const err = (error && typeof error === 'object' ? error : {}) as {
    code?: unknown
    details?: unknown
  }
  if (err.code === 'METHOD_NOT_FOUND') return 'outdated'
  const details = (err.details && typeof err.details === 'object' ? err.details : {}) as {
    code?: unknown
  }
  switch (details.code) {
    case 'workspace.denied':
      return 'denied'
    case 'workspace.unsafe':
      return 'unsafe'
    case 'workspace.not_found':
    case 'workspace.not_a_file':
    case 'workspace.not_a_directory':
      return 'not_found'
    case 'workspace.too_large':
      return 'too_large'
    case 'workspace.unsupported':
    case 'workspace.invalid_path':
      return 'unsupported'
    default:
      return 'failed'
  }
}

export const FAILURE_TITLE: Record<FailureKind, MessageKey> = {
  outdated: 'files.error.outdated.title',
  denied: 'files.error.denied.title',
  unsafe: 'files.error.unsafe.title',
  not_found: 'files.error.notFound.title',
  too_large: 'files.error.tooLarge.title',
  unsupported: 'files.error.unsupported.title',
  failed: 'files.error.failed.title',
}

export const FAILURE_BODY: Record<FailureKind, MessageKey> = {
  outdated: 'files.error.outdated.body',
  denied: 'files.error.denied.body',
  unsafe: 'files.error.unsafe.body',
  not_found: 'files.error.notFound.body',
  too_large: 'files.error.tooLarge.body',
  unsupported: 'files.error.unsupported.body',
  failed: 'files.error.failed.body',
}

/** Why a listed entry cannot be opened, for its tooltip. */
export function blockedReason(entry: FileEntry): MessageKey | null {
  if (entry.openable) return null
  if (entry.type === 'symlink' || entry.reason === 'symlink') return 'files.blocked.symlink'
  if (entry.reason === 'hardlink') return 'files.blocked.hardlink'
  return 'files.blocked.other'
}

/** The gateway's cap on previewed text (`MAX_TEXT_BYTES`). */
export const TEXT_PREVIEW_BYTES = 1024 * 1024

/** Past this, source is shown without syntax colouring: highlighting it would stall the UI. */
export const HIGHLIGHT_MAX_CHARS = 200_000
