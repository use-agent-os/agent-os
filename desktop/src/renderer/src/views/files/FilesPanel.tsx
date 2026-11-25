import './files.css'
import { useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, FolderOpen, RefreshCw, Search, X } from 'lucide-react'
import { useCallback, useId, useState } from 'react'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useGateway } from '~/stores/gateway'
import { useUi } from '~/stores/ui'
import { FilePreview } from './FilePreview'
import { FileTree } from './FileTree'
import { ancestors, FILES_QUERY_ROOT, type FileEntry } from './logic'

/**
 * The agent workspace as a panel over the window, the way Skills and
 * Scheduled jobs sit over it: the folder tree on the left, the selected file
 * on the right, Escape or the close button to leave. Read-only by
 * construction: the gateway methods behind it cannot write.
 */
export function FilesPanel() {
  const open = useUi((s) => s.filesOpen)
  const close = useUi((s) => s.closeFiles)
  const titleId = useId()
  if (!open) return null
  return (
    <ModalShell
      role="dialog"
      labelledBy={titleId}
      onClose={close}
      overlayClassName="files-panel__overlay"
      className="files-panel"
    >
      <FilesPanelBody titleId={titleId} onClose={close} />
    </ModalShell>
  )
}

function FilesPanelBody({ titleId, onClose }: { titleId: string; onClose: () => void }) {
  const gatewayState = useGateway((s) => s.status.state)
  const connected = gatewayState === 'running'
  const queryClient = useQueryClient()
  const refresh = useCallback(
    () => void queryClient.invalidateQueries({ queryKey: [FILES_QUERY_ROOT] }),
    [queryClient],
  )

  return (
    <>
      <header className="files-panel__head">
        <div className="min-w-0">
          <h1 id={titleId}>{t('files.title')}</h1>
          <p>{t('files.subtitle')}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {connected ? (
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('files.refresh')}
              title={t('files.refresh')}
              onClick={refresh}
            >
              <RefreshCw className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="icon"
            aria-label={t('files.close')}
            title={t('files.close')}
            onClick={onClose}
          >
            <X className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
          </Button>
        </div>
      </header>
      {connected ? (
        <ConnectedFiles />
      ) : (
        <div className="files-state">
          <FolderOpen className="size-9 text-dim" strokeWidth={1.25} aria-hidden />
          <p className="files-state__title">
            {gatewayState === 'starting'
              ? t('chat.waitingGateway')
              : t(`gateway.state.${gatewayState}`)}
          </p>
          <p className="files-state__body">{t('chat.gatewayDown')}</p>
        </div>
      )}
    </>
  )
}

function ConnectedFiles() {
  const selectedPath = useUi((s) => s.filesSelected)
  const selectFile = useUi((s) => s.selectFile)
  const expanded = useUi((s) => s.filesExpanded)
  const setExpanded = useUi((s) => s.setFilesExpanded)
  const [query, setQuery] = useState('')
  // The entry as listed, so the preview has the kind and size before any read.
  const [selected, setSelected] = useState<FileEntry | null>(null)
  // Narrow windows show one pane at a time; this is which.
  const [pane, setPane] = useState<'tree' | 'preview'>(selectedPath ? 'preview' : 'tree')

  const toggle = useCallback(
    (path: string) => {
      const next = new Set(expanded)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      setExpanded(next)
    },
    [expanded, setExpanded],
  )

  const open = useCallback(
    (entry: FileEntry) => {
      setSelected(entry)
      selectFile(entry.path)
      setPane('preview')
    },
    [selectFile],
  )

  // A file remembered from the last time the panel was open: make sure the
  // folders above it are disclosed so its row is on screen and selected.
  const reveal = useCallback(
    (path: string) => {
      const missing = ancestors(path).filter((dir) => !expanded.has(dir))
      if (missing.length > 0) setExpanded(new Set([...expanded, ...missing]))
    },
    [expanded, setExpanded],
  )

  return (
    <div className="files" data-pane={pane}>
      <section className="files-tree" aria-label={t('files.tree')}>
        <div className="files-tree__head">
          <label className="mac-search app-no-drag flex-1">
            <Search className="size-3.5 shrink-0" strokeWidth={1.75} aria-hidden />
            <input
              type="search"
              placeholder={t('files.filter')}
              aria-label={t('files.filter')}
              autoComplete="off"
              spellCheck={false}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
        </div>
        <FileTree
          query={query}
          expanded={expanded}
          selectedPath={selectedPath}
          onToggle={toggle}
          onOpen={open}
        />
      </section>
      <section className="files-preview">
        <button type="button" className="files-back app-no-drag" onClick={() => setPane('tree')}>
          <ChevronLeft className="size-3.5" strokeWidth={2} aria-hidden />
          {t('files.back')}
        </button>
        <FilePreview
          key={selectedPath ?? ''}
          path={selectedPath}
          entry={selected && selected.path === selectedPath ? selected : null}
          onReveal={reveal}
        />
      </section>
    </div>
  )
}
