import { useQueryClient } from '@tanstack/react-query'
import { FolderOpen, FolderX, RefreshCw, Search, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webPanel'
import { cn } from '@/lib/utils'
import {
  ancestors,
  baseName,
  FILES_QUERY_ROOT,
  parentPath,
  type FileEntry,
} from '@/lib/workspace-files'
import { EmptyState, ErrorState, Skeleton } from '@/web/components/ui/states'
import { SimpleTooltip } from '@/web/components/ui/tooltip'
import '@/web/styles/workspace-panel.css'
import { useElementSize } from '../hooks'
import { usePanels } from '../panel-store'
import type { ToolTabProps } from '../registry'
import { failureCopy } from './copy'
import { EMPTY_VIEW, useFilesStore, type FilesView } from './files-store'
import { FileTree } from './FileTree'
import { FileViewer } from './FileViewer'
import { useListing } from './queries'

/** A value for a quoted attribute selector. */
function cssString(value: string): string {
  return typeof CSS !== 'undefined' && CSS.escape
    ? CSS.escape(value)
    : value.replace(/["\\]/g, '\\$&')
}

/** At this width and up the tree and the file sit side by side. */
export const SPLIT_MIN_WIDTH = 640

function useView(key: string) {
  const view = useFilesStore((s) => s.views[key]) ?? EMPTY_VIEW
  const update = useFilesStore((s) => s.update)
  const set = useCallback(
    (patch: Partial<FilesView> | ((view: FilesView) => Partial<FilesView>)) => update(key, patch),
    [key, update],
  )
  return [view, set] as const
}

function TreeSkeleton() {
  return (
    <div className="flex flex-col gap-2.5 px-4 py-3" aria-hidden>
      {[70, 54, 62, 48, 66, 40].map((width, i) => (
        <Skeleton key={i} className="h-4" style={{ width: `${width}%` }} />
      ))}
    </div>
  )
}

/**
 * The Files tab: the session agent's workspace as a lazy tree with a filter,
 * and the selected file beside it (or in front of it when the panel is
 * narrow). Read-only; what the gateway hides (credentials, keys, internal
 * state, links) never appears.
 */
export function FilesTab({ sessionKey, agentId }: ToolTabProps) {
  const key = `${sessionKey}\u0000${agentId}`
  const [view, setView] = useView(key)
  const queryClient = useQueryClient()
  const root = useListing(agentId, '')
  const box = useRef<HTMLDivElement>(null)
  const { width } = useElementSize(box)
  const split = width >= SPLIT_MIN_WIDTH
  const expanded = useMemo(() => new Set(view.expanded), [view.expanded])
  const reveal = usePanels((s) => s.reveal)

  const toggle = useCallback(
    (path: string) =>
      setView((v) => ({
        expanded: v.expanded.includes(path)
          ? v.expanded.filter((p) => p !== path && !p.startsWith(`${path}/`))
          : [...v.expanded, path],
      })),
    [setView],
  )
  const openFile = useCallback(
    (entry: Pick<FileEntry, 'path'>) => setView({ selected: entry.path, mode: 'file' }),
    [setView],
  )
  /** Show a folder in the tree, or open a file, by path. */
  const showPath = useCallback(
    (path: string, kind?: 'directory' | 'file') => {
      setView((v) => {
        const open = new Set([...v.expanded, ...ancestors(path)])
        if (kind === 'directory' && path) open.add(path)
        return {
          expanded: [...open],
          query: '',
          // A folder opens in the tree beside whatever file is showing.
          ...(kind === 'directory' || !path
            ? { mode: 'tree' as const }
            : { mode: 'file' as const, selected: path }),
        }
      })
      if (kind === 'directory' && path) {
        requestAnimationFrame(() => {
          const row = box.current?.querySelector<HTMLElement>(
            `[role="treeitem"][data-path="${cssString(path)}"]`,
          )
          row?.scrollIntoView?.({ block: 'nearest' })
          row?.focus()
        })
      }
    },
    [setView],
  )

  // A path from the transcript (or a Markdown link): its folder's listing
  // says whether it is a folder to open or a file to show.
  const revealParent = reveal ? parentPath(reveal.path) : ''
  const parentListing = useListing(agentId, revealParent, reveal !== null)
  const handled = useRef<number | null>(null)
  useEffect(() => {
    if (!reveal || handled.current === reveal.nonce) return
    if (reveal.path === '') {
      handled.current = reveal.nonce
      showPath('', 'directory')
      return
    }
    if (parentListing.isPending) return
    handled.current = reveal.nonce
    const name = baseName(reveal.path)
    const entry = parentListing.data?.entries.find((e) => e.name === name)
    showPath(reveal.path, entry?.type === 'directory' ? 'directory' : 'file')
  }, [reveal, parentListing.isPending, parentListing.data, showPath])

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: [FILES_QUERY_ROOT, agentId] })
  }
  const selectedEntry = useMemo(() => {
    if (!view.selected) return undefined
    const listing = queryClient.getQueryData<{ entries: FileEntry[] }>([
      FILES_QUERY_ROOT,
      agentId,
      'list',
      parentPath(view.selected),
    ])
    return listing?.entries.find((e) => e.path === view.selected)
  }, [queryClient, agentId, view.selected])
  const selectedIsFile = view.selected !== null && selectedEntry?.type !== 'directory'

  let tree: React.ReactNode
  if (root.isPending) tree = <TreeSkeleton />
  else if (root.isError) {
    const copy = failureCopy(root.error)
    tree =
      copy.kind === 'outdated' ? (
        <div className="ws-state">
          <EmptyState size="sm" icon={<FolderX />} title={copy.title} description={copy.body} />
        </div>
      ) : (
        <div className="p-3">
          <ErrorState
            title={copy.title}
            message={copy.body}
            onRetry={() => void root.refetch()}
            retrying={root.isFetching}
          />
        </div>
      )
  } else if (!root.data.exists || root.data.entries.length === 0) {
    tree = (
      <div className="ws-state">
        <EmptyState
          size="sm"
          icon={<FolderOpen />}
          title={root.data.exists ? t('webPanel.filesEmptyTitle') : t('webPanel.filesMissingTitle')}
          description={
            root.data.exists ? t('webPanel.filesEmptyBody') : t('webPanel.filesMissingBody')
          }
        />
      </div>
    )
  } else {
    tree = (
      <>
        <FileTree
          agentId={agentId}
          query={view.query}
          expanded={expanded}
          selected={view.selected}
          onToggle={toggle}
          onOpen={openFile}
        />
        {view.query.trim() &&
        !root.data.entries.some((e) =>
          e.name.toLowerCase().includes(view.query.trim().toLowerCase()),
        ) &&
        view.expanded.length === 0 ? (
          <p className="ws-tree-empty">
            {t('webPanel.filesNoMatches', { query: view.query.trim() })}
          </p>
        ) : null}
      </>
    )
  }

  const showViewer = selectedIsFile && (split || view.mode === 'file')
  const showTree = split || !showViewer

  return (
    <div ref={box} className={cn('ws-files', split && 'ws-files--split')}>
      {showTree ? (
        <div className="ws-files-tree">
          <div className="ws-toolbar">
            <div className="ws-field-wrap">
              <Search aria-hidden />
              <input
                type="search"
                value={view.query}
                onChange={(e) => setView({ query: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === 'Escape' && view.query) {
                    e.preventDefault()
                    setView({ query: '' })
                  }
                }}
                aria-label={t('webPanel.filesFilter')}
                placeholder={t('webPanel.filesFilterPlaceholder')}
                className="ws-field"
              />
            </div>
            {view.query ? (
              <SimpleTooltip side="bottom" label={t('webPanel.filesClearFilter')}>
                <button
                  type="button"
                  aria-label={t('webPanel.filesClearFilter')}
                  onClick={() => setView({ query: '' })}
                  className="ws-icon-button target"
                >
                  <X />
                </button>
              </SimpleTooltip>
            ) : null}
            <SimpleTooltip side="bottom" label={t('webPanel.filesRefresh')}>
              <button
                type="button"
                aria-label={t('webPanel.filesRefresh')}
                onClick={refresh}
                className={cn('ws-icon-button target', root.isFetching && 'ws-spinning')}
              >
                <RefreshCw />
              </button>
            </SimpleTooltip>
          </div>
          <div className="ws-files-scroll scroll-contained">{tree}</div>
        </div>
      ) : null}
      {showViewer && view.selected ? (
        <FileViewer
          key={view.selected}
          agentId={agentId}
          path={view.selected}
          entry={selectedEntry}
          narrow={!split}
          onBack={() => setView({ mode: 'tree' })}
          onRevealFolder={(path) => showPath(path, 'directory')}
          onOpenPath={(path) => usePanels.getState().revealFile(path)}
        />
      ) : split ? (
        <div className="ws-viewer ws-viewer--empty">
          <EmptyState size="sm" icon={<FolderOpen />} title={t('webPanel.filesSelect')} />
        </div>
      ) : null}
      {root.isError && failureCopy(root.error).kind !== 'outdated' ? null : (
        <span className="sr-only" aria-live="polite">
          {root.isFetching && !root.isPending ? t('webPanel.filesLoading') : ''}
        </span>
      )}
    </div>
  )
}
