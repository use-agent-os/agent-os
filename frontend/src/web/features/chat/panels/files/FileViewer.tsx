import { useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Copy, Download, ExternalLink, FileWarning, Info, RefreshCw } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { toast } from 'sonner'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webPanel'
import { apiUrl } from '@/lib/api-origin'
import { cn } from '@/lib/utils'
import {
  baseName,
  FILES_QUERY_ROOT,
  formatBytes,
  formatModified,
  parentPath,
  TEXT_PREVIEW_BYTES,
  type FileEntry,
  type FileKind,
  type ReadResult,
} from '@/lib/workspace-files'
import { SegmentedControl } from '@/web/components/ui/segmented-control'
import { EmptyState, Skeleton } from '@/web/components/ui/states'
import { SimpleTooltip } from '@/web/components/ui/tooltip'
import { Markdown } from '@/web/features/chat/markdown'
import { failureCopy } from './copy'
import { EntryIcon } from './FileTree'
import { highlightSource } from './highlight'
import { useFileText, useFileTicket } from './queries'

export interface ViewerProps {
  agentId: string
  path: string
  /** The entry as its folder listed it, when known: saves a read for an image. */
  entry?: FileEntry
  /** A narrow panel shows the viewer alone, with a way back to the tree. */
  narrow: boolean
  onBack(): void
  /** Show a folder in the tree (a breadcrumb). */
  onRevealFolder(path: string): void
  /** Open a path a relative Markdown link names (a file or a folder). */
  onOpenPath(path: string): void
}

function IconButton(props: {
  label: string
  onClick?: () => void
  href?: string
  download?: string
  disabled?: boolean
  spinning?: boolean
  children: React.ReactNode
}) {
  const inner = props.href ? (
    <a
      href={props.href}
      download={props.download}
      target={props.download === undefined ? '_blank' : undefined}
      rel="noopener noreferrer"
      aria-label={props.label}
      className="ws-icon-button target"
    >
      {props.children}
    </a>
  ) : (
    <button
      type="button"
      aria-label={props.label}
      disabled={props.disabled}
      onClick={props.onClick}
      className={cn('ws-icon-button target', props.spinning && 'ws-spinning')}
    >
      {props.children}
    </button>
  )
  return (
    <SimpleTooltip side="bottom" label={props.label}>
      {inner}
    </SimpleTooltip>
  )
}

function Crumbs({ path, onReveal }: { path: string; onReveal(path: string): void }) {
  const parts = path.split('/')
  return (
    <nav aria-label={t('webPanel.filesBreadcrumb')} className="ws-crumbs-nav">
      <ol className="ws-crumbs">
        <li>
          <button type="button" className="ws-crumb" onClick={() => onReveal('')}>
            {t('webPanel.filesRoot')}
          </button>
        </li>
        {parts.map((part, index) => {
          const last = index === parts.length - 1
          const target = parts.slice(0, index + 1).join('/')
          return (
            <li key={target}>
              <span className="ws-crumb-sep" aria-hidden>
                /
              </span>
              {last ? (
                <span className="ws-crumb" aria-current="location">
                  {part}
                </span>
              ) : (
                <button type="button" className="ws-crumb" onClick={() => onReveal(target)}>
                  {part}
                </button>
              )}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}

/** Source with a line-number gutter; wrapped prose for plain text. */
function SourceView({
  text,
  language,
  wrap,
  label,
}: {
  text: string
  language?: string
  wrap: boolean
  label: string
}) {
  const html = useMemo(() => highlightSource(text, language), [text, language])
  const lines = useMemo(() => {
    const count = text.endsWith('\n') ? text.split('\n').length - 1 : text.split('\n').length
    return Array.from({ length: Math.max(1, count) }, (_, i) => i + 1).join('\n')
  }, [text])
  return (
    <div
      role="region"
      aria-label={label}
      tabIndex={0}
      className={cn('ws-code scroll-contained', wrap && 'ws-code--wrap')}
    >
      {wrap ? null : (
        <pre className="ws-code-gutter" aria-hidden>
          {lines}
        </pre>
      )}
      <pre className="ws-code-text">
        <code
          className="hljs"
          // Escaped text and hljs spans only (highlightSource sanitizes).
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </pre>
    </div>
  )
}

/** A relative link in a workspace Markdown file, as a workspace path. */
export function relativeLinkTarget(docPath: string, href: string): string | null {
  if (!href || href.startsWith('#') || /^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('//'))
    return null
  const bare = href.split(/[?#]/, 1)[0] ?? ''
  if (!bare) return null
  let decoded: string
  try {
    decoded = decodeURIComponent(bare)
  } catch {
    return null
  }
  const parts = bare.startsWith('/') ? [] : parentPath(docPath).split('/').filter(Boolean)
  for (const part of decoded.split('/')) {
    if (!part || part === '.') continue
    if (part === '..') {
      if (!parts.length) return null
      parts.pop()
    } else parts.push(part)
  }
  return parts.length ? parts.join('/') : null
}

function MarkdownView({
  text,
  path,
  onReveal,
}: {
  text: string
  path: string
  onReveal(path: string): void
}) {
  // A wide code block scrolls on its own: it has to take focus to be scrolled by keyboard.
  const region = useRef<HTMLDivElement>(null)
  useEffect(() => {
    for (const pre of region.current?.querySelectorAll('pre') ?? []) pre.tabIndex = 0
  }, [text])
  const onClickCapture = (e: MouseEvent<HTMLDivElement>) => {
    const anchor = (e.target as HTMLElement).closest('a')
    if (!anchor || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
    const target = relativeLinkTarget(path, anchor.getAttribute('href') ?? '')
    if (target === null) return
    e.preventDefault()
    onReveal(target)
  }
  return (
    <div
      ref={region}
      role="region"
      aria-label={baseName(path)}
      tabIndex={0}
      onClickCapture={onClickCapture}
      className="ws-markdown scroll-contained"
    >
      <Markdown text={text} />
    </div>
  )
}

function Notice(props: {
  title: string
  body?: string
  tone?: 'danger'
  children?: React.ReactNode
}) {
  return (
    <div className="ws-state">
      <EmptyState
        size="sm"
        icon={props.tone ? <FileWarning /> : <Info />}
        title={props.title}
        description={props.body}
        action={props.children}
      />
    </div>
  )
}

function ViewerSkeleton() {
  return (
    <div className="flex flex-col gap-2 p-4" aria-hidden>
      {[92, 76, 84, 60, 88, 70, 40].map((width, i) => (
        <Skeleton key={i} className="h-3.5" style={{ width: `${width}%` }} />
      ))}
    </div>
  )
}

function useBlobUrl(text: string | null, mime: string): string | null {
  const url = useMemo(
    () =>
      text === null || typeof URL.createObjectURL !== 'function'
        ? null
        : URL.createObjectURL(new Blob([text], { type: mime })),
    [text, mime],
  )
  useEffect(() => () => (url ? URL.revokeObjectURL(url) : undefined), [url])
  return url
}

const DOWNLOADED: ReadonlySet<FileKind> = new Set(['image', 'pdf'])

/**
 * One workspace file: a header with its path and actions, then the content
 * the gateway allows — highlighted source, rendered Markdown, an image — or
 * the reason it cannot be shown.
 */
export function FileViewer(props: ViewerProps) {
  const { agentId, path } = props
  const queryClient = useQueryClient()
  const knownKind = props.entry?.kind
  const ticketFirst = knownKind !== undefined && DOWNLOADED.has(knownKind)
  const read = useFileText(agentId, path, !ticketFirst)
  const readKind = read.data?.kind
  const wantsTicket = ticketFirst || (readKind !== undefined && DOWNLOADED.has(readKind))
  const ticket = useFileTicket(agentId, path, wantsTicket)
  const [view, setView] = useState<'preview' | 'source'>('preview')
  // The viewer is keyed by path, so a new file starts without this.
  const [imageFailed, setImageFailed] = useState(false)

  const result: ReadResult | undefined = read.data
  const kind: FileKind = knownKind ?? result?.kind ?? 'other'
  const name = baseName(path)
  const size = result?.size ?? props.entry?.size ?? ticket.data?.size
  const mtime = result?.mtimeMs ?? props.entry?.mtimeMs
  const meta = [formatBytes(size), formatModified(mtime)].filter(Boolean).join(' · ')
  const textComplete =
    result?.previewable && !result.truncated ? result.content : (null as string | null)
  const blobUrl = useBlobUrl(textComplete, 'text/plain;charset=utf-8')
  const ticketUrl = ticket.data ? apiUrl(ticket.data.url) : null
  const downloadHref = wantsTicket ? ticketUrl : blobUrl
  const refreshing = read.isFetching || ticket.isFetching

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: [FILES_QUERY_ROOT, agentId, 'read', path] })
    void queryClient.invalidateQueries({ queryKey: [FILES_QUERY_ROOT, agentId, 'ticket', path] })
    setImageFailed(false)
  }
  const copyPath = () => {
    const clip = navigator.clipboard?.writeText(path)
    if (!clip) return void toast.error(t('webPanel.filesCopyFailed'))
    void clip
      .then(() => toast.success(t('webPanel.filesPathCopied')))
      .catch(() => toast.error(t('webPanel.filesCopyFailed')))
  }

  let body: React.ReactNode
  const failed = wantsTicket
    ? ticket.isError
      ? ticket.error
      : null
    : read.isError
      ? read.error
      : null
  if (failed) {
    const copy = failureCopy(failed)
    body = <Notice title={copy.title} body={copy.body} tone="danger" />
  } else if (wantsTicket) {
    if (!ticket.data) body = <ViewerSkeleton />
    else if (kind === 'image') {
      body = imageFailed ? (
        <Notice title={t('webPanel.filesImageFailed')} tone="danger" />
      ) : (
        <div
          className="ws-image-stage scroll-contained"
          tabIndex={0}
          role="region"
          aria-label={name}
        >
          <img
            src={ticketUrl ?? undefined}
            alt={name}
            onError={() => setImageFailed(true)}
            className="ws-image"
          />
        </div>
      )
    } else {
      body = <Notice title={t('webPanel.filesPdfTitle')} body={t('webPanel.filesPdfBody')} />
    }
  } else if (!result) {
    body = <ViewerSkeleton />
  } else if (!result.previewable) {
    body = (
      <Notice
        title={t('webPanel.filesBinaryTitle')}
        body={t('webPanel.filesBinaryBody', { size: formatBytes(result.size) || '0 B' })}
      />
    )
  } else if (!result.content) {
    body = <Notice title={t('webPanel.filesEmptyFile')} />
  } else if (kind === 'markdown' && view === 'preview') {
    body = <MarkdownView text={result.content} path={path} onReveal={props.onOpenPath} />
  } else {
    body = (
      <SourceView
        text={result.content}
        language={kind === 'markdown' ? 'markdown' : result.language}
        wrap={kind === 'text'}
        label={name}
      />
    )
  }

  // A cut file's line count would undercount: it is left out.
  const lineCount =
    result?.previewable && !result.truncated && result.content
      ? result.content.split('\n').length - (result.content.endsWith('\n') ? 1 : 0)
      : 0

  const metaLine = [meta, lineCount ? tPlural('webPanel.filesLines', lineCount) : '']
    .filter(Boolean)
    .join(' · ')

  return (
    <section className="ws-viewer">
      <div className="ws-toolbar ws-viewer-crumbs">
        {props.narrow ? (
          <IconButton label={t('webPanel.filesBack')} onClick={props.onBack}>
            <ArrowLeft />
          </IconButton>
        ) : null}
        <Crumbs path={path} onReveal={props.onRevealFolder} />
      </div>
      <div className="ws-viewer-bar">
        <span className="chat-tile ws-viewer-tile" aria-hidden>
          <EntryIcon entry={{ type: 'file', kind }} />
        </span>
        <div className="ws-viewer-title">
          <h2 className="ws-viewer-name" title={path}>
            {name}
          </h2>
          <p className="ws-viewer-meta" title={metaLine}>
            {metaLine}
          </p>
        </div>
        <div className="ws-viewer-actions">
          {kind === 'markdown' && result?.previewable && result.content ? (
            <SegmentedControl
              size="sm"
              kind="toggle"
              aria-label={t('webPanel.filesView')}
              value={view}
              onChange={setView}
              options={[
                { value: 'preview', label: t('webPanel.filesPreview') },
                { value: 'source', label: t('webPanel.filesSource') },
              ]}
            />
          ) : null}
          <IconButton label={t('webPanel.filesCopyPath')} onClick={copyPath}>
            <Copy />
          </IconButton>
          {wantsTicket && ticketUrl ? (
            <IconButton label={t('webPanel.filesOpenNewTab')} href={ticketUrl}>
              <ExternalLink />
            </IconButton>
          ) : null}
          {downloadHref ? (
            <IconButton label={t('webPanel.filesDownload')} href={downloadHref} download={name}>
              <Download />
            </IconButton>
          ) : (
            <IconButton label={t('webPanel.filesDownloadUnavailable')} disabled>
              <Download />
            </IconButton>
          )}
          <IconButton label={t('webPanel.filesRefresh')} onClick={refresh} spinning={refreshing}>
            <RefreshCw />
          </IconButton>
        </div>
      </div>
      {result?.truncated ? (
        <p className="ws-note" data-tone="info">
          <Info aria-hidden />
          {t('webPanel.filesTruncatedNotice', {
            shown: formatBytes(TEXT_PREVIEW_BYTES),
            size: formatBytes(result.size),
          })}
        </p>
      ) : null}
      <div className="ws-viewer-body">{body}</div>
    </section>
  )
}
