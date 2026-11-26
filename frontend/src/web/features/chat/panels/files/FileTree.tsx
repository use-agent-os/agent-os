import {
  ChevronRight,
  File,
  FileCode,
  FileImage,
  FileText,
  Folder,
  FolderOpen,
  Link2,
  LoaderCircle,
} from 'lucide-react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useEffect, useId, useRef, type KeyboardEvent } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webPanel'
import { cn } from '@/lib/utils'
import {
  FILES_QUERY_ROOT,
  filterEntries,
  formatBytes,
  type FileEntry,
  type Listing,
} from '@/lib/workspace-files'
import { failureCopy } from './copy'
import { useListing } from './queries'

const ROW = '[role="treeitem"]'

/** The icon of an entry: a folder (open or closed), a link, or the file's kind. */
export function EntryIcon({
  entry,
  open = false,
  className,
}: {
  entry: Pick<FileEntry, 'type' | 'kind'>
  open?: boolean
  className?: string
}) {
  const props = { className, 'aria-hidden': true, 'data-kind': entry.kind ?? entry.type }
  if (entry.type === 'directory') return open ? <FolderOpen {...props} /> : <Folder {...props} />
  if (entry.type === 'symlink') return <Link2 {...props} />
  if (entry.kind === 'image') return <FileImage {...props} />
  if (entry.kind === 'code') return <FileCode {...props} />
  if (entry.kind === 'markdown' || entry.kind === 'text' || entry.kind === 'pdf')
    return <FileText {...props} />
  return <File {...props} />
}

function blockedReason(entry: FileEntry): string | null {
  if (entry.openable) return null
  if (entry.type === 'symlink' || entry.reason === 'symlink')
    return t('webPanel.filesBlockedSymlink')
  if (entry.reason === 'hardlink') return t('webPanel.filesBlockedHardlink')
  return t('webPanel.filesBlockedOther')
}

export interface TreeProps {
  agentId: string
  query: string
  expanded: ReadonlySet<string>
  selected: string | null
  onToggle(path: string): void
  onOpen(entry: FileEntry): void
}

/**
 * The workspace as an outline. Each open folder reads its own listing, so a
 * large tree costs only what is disclosed. Arrow keys walk the visible rows:
 * Right opens a folder (or steps in), Left closes it (or steps out), Enter
 * opens a file; one row at a time is in the tab order.
 */
export function FileTree(props: TreeProps) {
  const ref = useRef<HTMLUListElement>(null)

  // One tab stop for the whole tree: the selected row, else the first.
  useEffect(() => {
    const rows = Array.from(ref.current?.querySelectorAll<HTMLElement>(ROW) ?? [])
    const target = rows.find((row) => row.getAttribute('aria-selected') === 'true') ?? rows[0]
    for (const row of rows) row.tabIndex = row === target ? 0 : -1
  })

  function onKeyDown(event: KeyboardEvent<HTMLUListElement>) {
    const rows = Array.from(ref.current?.querySelectorAll<HTMLElement>(ROW) ?? [])
    const current = (event.target as HTMLElement).closest<HTMLElement>(ROW)
    if (!current || rows.length === 0) return
    const index = rows.indexOf(current)
    const focus = (next: number) => {
      const row = rows[Math.max(0, Math.min(rows.length - 1, next))]
      if (!row) return
      for (const other of rows) other.tabIndex = other === row ? 0 : -1
      row.focus()
    }
    const isDir = current.dataset.type === 'directory'
    const isOpen = current.getAttribute('aria-expanded') === 'true'
    const rtl = getComputedStyle(current).direction === 'rtl'
    const inward = rtl ? 'ArrowLeft' : 'ArrowRight'
    const outward = rtl ? 'ArrowRight' : 'ArrowLeft'
    switch (event.key) {
      case 'ArrowDown':
        focus(index + 1)
        break
      case 'ArrowUp':
        focus(index - 1)
        break
      case 'Home':
        focus(0)
        break
      case 'End':
        focus(rows.length - 1)
        break
      case inward:
        if (isDir && !isOpen) props.onToggle(current.dataset.path ?? '')
        else if (isDir) focus(index + 1)
        break
      case outward: {
        if (isDir && isOpen) {
          props.onToggle(current.dataset.path ?? '')
          break
        }
        const level = Number(current.getAttribute('aria-level') ?? 1)
        for (let i = index - 1; i >= 0; i -= 1) {
          if (Number(rows[i]?.getAttribute('aria-level') ?? 1) < level) {
            focus(i)
            break
          }
        }
        break
      }
      case 'Enter':
      case ' ':
        current.click()
        break
      default:
        return
    }
    event.preventDefault()
  }

  return (
    <ul
      ref={ref}
      role="tree"
      aria-label={t('webPanel.filesTree')}
      onKeyDown={onKeyDown}
      className="ws-tree"
    >
      <Level {...props} path="" level={1} />
    </ul>
  )
}

/**
 * Whether an open folder holds a match for the filter in what has been
 * loaded of it. A folder whose contents are not loaded yet keeps showing.
 */
function holdsMatch(
  client: QueryClient,
  agentId: string,
  path: string,
  needle: string,
  expanded: ReadonlySet<string>,
  depth = 0,
): boolean {
  const listing = client.getQueryData<Listing>([FILES_QUERY_ROOT, agentId, 'list', path])
  if (!listing || depth > 16) return true
  return listing.entries.some(
    (entry) =>
      entry.name.toLowerCase().includes(needle) ||
      (entry.type === 'directory' &&
        expanded.has(entry.path) &&
        holdsMatch(client, agentId, entry.path, needle, expanded, depth + 1)),
  )
}

function Level(props: TreeProps & { path: string; level: number }) {
  const listing = useListing(props.agentId, props.path)
  const client = useQueryClient()
  const indent = { paddingInlineStart: `${12 + (props.level - 1) * 16}px` }

  if (listing.isPending) {
    return (
      <li role="none" className="ws-tree-note" style={indent}>
        <LoaderCircle className="motion-status size-3.5 animate-spin" aria-hidden />
        {t('webPanel.filesLoading')}
      </li>
    )
  }
  if (listing.isError) {
    // The root's failure is the whole tab's state (FilesTab); a folder's is its own.
    if (props.level === 1) return null
    const copy = failureCopy(listing.error)
    return (
      <li role="none" className="ws-tree-note" data-tone="danger" style={indent}>
        <span className="min-w-0 truncate">{copy.title}</span>
        <button type="button" className="ws-link" onClick={() => void listing.refetch()}>
          {t('webCommon.retry')}
        </button>
      </li>
    )
  }
  const needle = props.query.trim().toLowerCase()
  const entries = filterEntries(listing.data.entries, props.query, props.expanded).filter(
    (entry) =>
      !needle ||
      entry.name.toLowerCase().includes(needle) ||
      holdsMatch(client, props.agentId, entry.path, needle, props.expanded),
  )
  if (listing.data.entries.length === 0 && props.level > 1) {
    return (
      <li role="none" className="ws-tree-note" style={indent}>
        {t('webPanel.filesFolderEmpty')}
      </li>
    )
  }
  return (
    <>
      {entries.map((entry) => (
        <Row key={entry.path} {...props} entry={entry} />
      ))}
      {listing.data.truncated ? (
        <li role="none" className="ws-tree-note" style={indent}>
          {t('webPanel.filesTruncated', { count: listing.data.entries.length })}
        </li>
      ) : null}
    </>
  )
}

function Row(props: TreeProps & { entry: FileEntry; level: number }) {
  const { entry, level } = props
  const isDir = entry.type === 'directory'
  const open = isDir && props.expanded.has(entry.path)
  const blocked = blockedReason(entry)
  const selected = props.selected === entry.path
  // Named by its own row (name, then size): an open folder's rows are not part of it.
  const nameId = useId()
  const sizeId = useId()
  const sized = !isDir && entry.size !== undefined
  return (
    <li
      role="treeitem"
      aria-labelledby={sized ? `${nameId} ${sizeId}` : nameId}
      aria-level={level}
      aria-expanded={isDir ? open : undefined}
      aria-selected={selected}
      aria-disabled={blocked ? true : undefined}
      data-path={entry.path}
      data-type={entry.type}
      tabIndex={-1}
      title={blocked ?? undefined}
      onClick={(e) => {
        e.stopPropagation()
        if (blocked) return
        if (isDir) props.onToggle(entry.path)
        else props.onOpen(entry)
      }}
      className="ws-tree-item"
    >
      <div
        className={cn('ws-tree-row', blocked && 'ws-tree-row--blocked')}
        style={{ paddingInlineStart: `${8 + (level - 1) * 16}px` }}
      >
        <span className="ws-tree-chevron" aria-hidden>
          {isDir ? <ChevronRight data-open={open || undefined} /> : null}
        </span>
        <EntryIcon entry={entry} open={open} className="ws-tree-icon" />
        <span id={nameId} className="ws-tree-name">
          {entry.name}
        </span>
        {sized ? (
          <span id={sizeId} className="ws-tree-size">
            {formatBytes(entry.size)}
          </span>
        ) : null}
      </div>
      {open ? (
        <ul role="group">
          <Level {...props} path={entry.path} level={level + 1} />
        </ul>
      ) : null}
    </li>
  )
}
