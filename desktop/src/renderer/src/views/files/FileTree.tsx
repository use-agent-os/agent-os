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
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useRef, type KeyboardEvent } from 'react'
import { t } from '~/i18n'
import {
  blockedReason,
  failureKind,
  FAILURE_BODY,
  FAILURE_TITLE,
  filterEntries,
  type FileEntry,
  type Listing,
} from './logic'
import { useListing } from './queries'
import { fill } from './text'

interface TreeProps {
  query: string
  expanded: ReadonlySet<string>
  selectedPath: string | null
  onToggle(path: string): void
  onOpen(entry: FileEntry): void
}

const ROW_SELECTOR = '[data-tree-row]'

/**
 * The workspace as an outline. Each open folder reads its own listing, so a
 * large tree costs only what is disclosed. Arrow keys walk the visible rows;
 * Right opens a folder, Left closes it or steps to its parent.
 */
export function FileTree(props: TreeProps) {
  const ref = useRef<HTMLDivElement>(null)
  const root = useListing('')

  // One tab stop for the whole tree: the selected row, else the first.
  useEffect(() => {
    const rows = Array.from(ref.current?.querySelectorAll<HTMLElement>(ROW_SELECTOR) ?? [])
    const target = rows.find((row) => row.getAttribute('aria-selected') === 'true') ?? rows[0]
    for (const row of rows) row.tabIndex = row === target ? 0 : -1
  })

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const rows = Array.from(ref.current?.querySelectorAll<HTMLElement>(ROW_SELECTOR) ?? [])
    const current = (event.target as HTMLElement).closest<HTMLElement>(ROW_SELECTOR)
    if (!current || rows.length === 0) return
    const index = rows.indexOf(current)
    const focus = (next: number) => rows[Math.max(0, Math.min(rows.length - 1, next))]?.focus()
    const isDir = current.dataset.type === 'directory'
    const isOpen = current.getAttribute('aria-expanded') === 'true'
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
      case 'ArrowRight':
        if (isDir && !isOpen) props.onToggle(current.dataset.path ?? '')
        else if (isDir) focus(index + 1)
        break
      case 'ArrowLeft': {
        if (isDir && isOpen) {
          props.onToggle(current.dataset.path ?? '')
          break
        }
        const depth = Number(current.dataset.depth ?? 0)
        for (let i = index - 1; i >= 0; i -= 1) {
          const row = rows[i]
          if (row && Number(row.dataset.depth ?? 0) < depth) {
            row.focus()
            break
          }
        }
        break
      }
      default:
        return
    }
    event.preventDefault()
  }

  let body
  if (root.isPending) {
    body = <TreeSkeleton />
  } else if (root.isError) {
    const kind = failureKind(root.error)
    body = (
      <div className="files-tree__note" role="alert">
        <p className="files-tree__note-title">{t(FAILURE_TITLE[kind])}</p>
        <p>{t(FAILURE_BODY[kind])}</p>
        {kind === 'outdated' ? null : (
          <button
            type="button"
            className="files-link app-no-drag"
            onClick={() => void root.refetch()}
          >
            {t('files.retry')}
          </button>
        )}
      </div>
    )
  } else if (root.data.entries.length === 0) {
    body = (
      <div className="files-tree__note">
        <p className="files-tree__note-title">{t('files.empty.title')}</p>
        <p>{t('files.empty.body')}</p>
      </div>
    )
  } else {
    body = <TreeLevel listing={root.data} depth={0} top {...props} />
  }

  return (
    <div ref={ref} className="files-tree__scroll" role="tree" onKeyDown={onKeyDown}>
      {body}
    </div>
  )
}

function TreeSkeleton() {
  return (
    <div className="files-skeleton" role="status" aria-label={t('files.loading')}>
      {[62, 48, 74, 40, 56, 66].map((width, i) => (
        <span key={i} style={{ width: `${width}%` }} />
      ))}
    </div>
  )
}

function TreeLevel({
  listing,
  depth,
  top = false,
  ...props
}: TreeProps & { listing: Listing; depth: number; top?: boolean }) {
  const rows = filterEntries(listing.entries, props.query, props.expanded)
  const indent = { paddingLeft: 8 + depth * 14 + 18 }
  if (rows.length === 0) {
    return (
      <p className="files-tree__hint" style={top ? undefined : indent}>
        {props.query.trim() && listing.entries.length > 0
          ? t('files.empty.filter')
          : t('files.empty.folder')}
      </p>
    )
  }
  return (
    <>
      {rows.map((entry) => (
        <TreeRow key={entry.path} entry={entry} depth={depth} {...props} />
      ))}
      {listing.truncated ? (
        <p className="files-tree__hint" style={indent}>
          {fill(t('files.truncated'), {
            shown: listing.entries.length,
            total: Math.max(listing.total, listing.entries.length),
          })}
        </p>
      ) : null}
    </>
  )
}

const KIND_ICON: Record<string, LucideIcon> = {
  markdown: FileText,
  text: FileText,
  code: FileCode,
  image: FileImage,
  pdf: FileText,
  other: File,
}

function TreeRow({ entry, depth, ...props }: TreeProps & { entry: FileEntry; depth: number }) {
  const isDir = entry.type === 'directory'
  const open = isDir && props.expanded.has(entry.path)
  const selected = !isDir && props.selectedPath === entry.path
  const blocked = blockedReason(entry)
  const Icon = isDir
    ? open
      ? FolderOpen
      : Folder
    : entry.type === 'symlink'
      ? Link2
      : (KIND_ICON[entry.kind ?? 'other'] ?? File)

  return (
    <>
      <button
        type="button"
        role="treeitem"
        data-tree-row
        data-path={entry.path}
        data-type={entry.type}
        data-depth={depth}
        className="files-row app-no-drag"
        style={{ paddingLeft: 8 + depth * 14 }}
        aria-level={depth + 1}
        aria-expanded={isDir ? open : undefined}
        aria-selected={selected}
        aria-disabled={blocked ? true : undefined}
        title={blocked ? t(blocked) : entry.name}
        tabIndex={-1}
        onClick={() => {
          if (blocked) return
          if (isDir) props.onToggle(entry.path)
          else props.onOpen(entry)
        }}
      >
        <ChevronRight
          className="files-row__chevron"
          data-open={open}
          data-hidden={!isDir}
          strokeWidth={2}
          aria-hidden
        />
        <Icon className="files-row__icon" strokeWidth={1.75} aria-hidden />
        <span className="files-row__name">{entry.name}</span>
      </button>
      {open ? <TreeBranch path={entry.path} depth={depth + 1} {...props} /> : null}
    </>
  )
}

function TreeBranch({ path, depth, ...props }: TreeProps & { path: string; depth: number }) {
  const listing = useListing(path)
  const indent = { paddingLeft: 8 + depth * 14 + 18 }
  if (listing.isPending) {
    return (
      <p className="files-tree__hint" style={indent} role="status">
        <LoaderCircle className="size-3 animate-spin" strokeWidth={2} aria-hidden />
        {t('files.loading')}
      </p>
    )
  }
  if (listing.isError) {
    const kind = failureKind(listing.error)
    return (
      <p className="files-tree__hint" style={indent} role="alert" data-tone="danger">
        {t(FAILURE_TITLE[kind])}
      </p>
    )
  }
  return <TreeLevel listing={listing.data} depth={depth} {...props} />
}
