import { Copy, FileQuestion, FileSearch, Link, LoaderCircle, TriangleAlert } from 'lucide-react'
import { lazy, Suspense, useEffect, useMemo, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { apiUrl } from '@/lib/api-origin'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import {
  baseName,
  failureKind,
  FAILURE_BODY,
  FAILURE_TITLE,
  formatBytes,
  formatModified,
  parentPath,
  TEXT_PREVIEW_BYTES,
  type FailureKind,
  type FileEntry,
  type FileKind,
  type ReadResult,
} from './logic'
import { useFileText, useFileTicket } from './queries'
import { highlightCode, renderMarkdown } from './render'
import { fill } from './text'

// pdf.js is a megabyte of parser: loaded the first time a PDF is opened.
const PdfView = lazy(() => import('./PdfView'))

interface PreviewProps {
  path: string | null
  /** The row that was clicked, when there was one: its kind and size are known up front. */
  entry: FileEntry | null
  /** Ask the tree to disclose the folders above this path. */
  onReveal(path: string): void
}

function isDownloadKind(kind: FileKind | undefined): boolean {
  return kind === 'image' || kind === 'pdf'
}

/** The selected file: a header that never wraps out of its box, then the content. */
export function FilePreview({ path, entry, onReveal }: PreviewProps) {
  // A file remembered from an earlier visit has no row yet: ask the gateway
  // what it is before deciding between text and a download ticket.
  const knownDownload = isDownloadKind(entry?.kind)
  const text = useFileText(path, !knownDownload)
  const viaRead = text.data && !text.data.previewable && text.data.reason === 'download'
  const downloadKind: FileKind | undefined = knownDownload
    ? entry?.kind
    : viaRead
      ? text.data?.kind
      : undefined
  const ticket = useFileTicket(path, downloadKind !== undefined)
  const [mode, setMode] = useState<'rendered' | 'source'>('rendered')

  useEffect(() => {
    if (path) onReveal(path)
    // Once per file: revealing again on every tree change would reopen
    // folders the user has just closed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path])

  if (!path) {
    return (
      <State
        icon={<FileSearch className="size-9 text-dim" strokeWidth={1.25} aria-hidden />}
        title={t('files.pick.title')}
        body={t('files.pick.body')}
      />
    )
  }

  const read = text.data
  const kind: FileKind | undefined = downloadKind ?? read?.kind ?? entry?.kind
  const size = read?.size ?? ticket.data?.size ?? entry?.size
  const mtimeMs = read?.mtimeMs || entry?.mtimeMs
  const hasText = read?.previewable === true

  let body: ReactNode
  if (downloadKind) {
    if (ticket.isPending) body = <Loading />
    else if (ticket.isError) body = <Failure kind={failureKind(ticket.error)} retry={ticket} />
    else if (downloadKind === 'image') body = <ImageView src={apiUrl(ticket.data.url)} />
    else
      body = (
        <Suspense fallback={<Loading />}>
          {/* Keyed by URL: a renewed ticket starts the viewer over. */}
          <PdfView key={ticket.data.url} url={apiUrl(ticket.data.url)} />
        </Suspense>
      )
  } else if (text.isPending) {
    body = <Loading />
  } else if (text.isError) {
    body = <Failure kind={failureKind(text.error)} retry={text} />
  } else if (!hasText || !read) {
    body = (
      <State
        icon={<FileQuestion className="size-9 text-dim" strokeWidth={1.25} aria-hidden />}
        title={t('files.binary.title')}
        body={t('files.binary.body')}
      />
    )
  } else {
    body = <TextView read={read} mode={mode} />
  }

  return (
    <>
      <header className="files-preview__head">
        <div className="files-preview__title">
          <h2 title={path}>{baseName(path)}</h2>
          <p title={path}>
            {[
              parentPath(path) || '/',
              formatBytes(size),
              formatModified(mtimeMs),
              read?.truncated
                ? fill(t('files.meta.truncated'), { size: formatBytes(TEXT_PREVIEW_BYTES) })
                : '',
            ]
              .filter(Boolean)
              .join('  ·  ')}
          </p>
        </div>
        <div className="files-preview__actions">
          {hasText && kind === 'markdown' ? (
            <div className="files-seg" role="radiogroup" aria-label={t('files.view.label')}>
              {(['rendered', 'source'] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={mode === value}
                  className="app-no-drag"
                  onClick={() => setMode(value)}
                >
                  {t(value === 'rendered' ? 'files.view.rendered' : 'files.view.source')}
                </button>
              ))}
            </div>
          ) : null}
          {hasText && read ? (
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('files.copyContents')}
              title={t('files.copyContents')}
              onClick={() => copy(read.content, t('files.copied.contents'))}
            >
              <Copy className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="icon"
            aria-label={t('files.copyPath')}
            title={t('files.copyPath')}
            onClick={() => copy(path, t('files.copied.path'))}
          >
            <Link className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
          </Button>
        </div>
      </header>
      <div className="files-preview__body" data-selectable>
        {body}
      </div>
    </>
  )
}

function copy(text: string, done: string) {
  navigator.clipboard.writeText(text).then(
    () => toast.success(done),
    () => toast.error(t('files.copyFailed')),
  )
}

function State({ icon, title, body }: { icon: ReactNode; title: string; body: string }) {
  return (
    <div className="files-state">
      {icon}
      <p className="files-state__title">{title}</p>
      <p className="files-state__body">{body}</p>
    </div>
  )
}

function Loading() {
  return (
    <div className="files-state" role="status" aria-label={t('files.loading')}>
      <LoaderCircle className="size-5 animate-spin text-dim" strokeWidth={1.75} aria-hidden />
    </div>
  )
}

function Failure({ kind, retry }: { kind: FailureKind; retry: { refetch(): unknown } }) {
  const canRetry = kind === 'failed' || kind === 'not_found'
  return (
    <div className="files-state" role="alert">
      <TriangleAlert className="size-9 text-dim" strokeWidth={1.25} aria-hidden />
      <p className="files-state__title">{t(FAILURE_TITLE[kind])}</p>
      <p className="files-state__body">{t(FAILURE_BODY[kind])}</p>
      {canRetry ? <Button onClick={() => void retry.refetch()}>{t('files.retry')}</Button> : null}
    </div>
  )
}

function TextView({ read, mode }: { read: ReadResult; mode: 'rendered' | 'source' }) {
  const rendered = read.kind === 'markdown' && mode === 'rendered'
  const html = useMemo(() => {
    if (rendered) {
      return renderMarkdown(read.content, {
        remoteImage: t('files.md.remoteImage'),
        localImage: t('files.md.localImage'),
      })
    }
    if (read.kind === 'text') return null
    return highlightCode(read.content, read.kind === 'markdown' ? 'markdown' : read.language)
  }, [rendered, read.content, read.kind, read.language])

  if (rendered) {
    // Sanitized by renderMarkdown: its last step is the sanitizer.
    return <article className="files-md" dangerouslySetInnerHTML={{ __html: html ?? '' }} />
  }
  if (html === null) return <pre className="files-text">{read.content}</pre>
  return (
    <pre className="files-code">
      {/* highlight.js output: escaped text in its own class-only spans. */}
      <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} />
    </pre>
  )
}

function ImageView({ src }: { src: string }) {
  const [failed, setFailed] = useState(false)
  if (failed) {
    return (
      <State
        icon={<TriangleAlert className="size-9 text-dim" strokeWidth={1.25} aria-hidden />}
        title={t('files.error.failed.title')}
        body={t('files.image.failed')}
      />
    )
  }
  return (
    <div className="files-image">
      {/* Always an <img>: an SVG shown this way cannot run script or load anything. */}
      <img src={src} alt="" draggable={false} onError={() => setFailed(true)} />
    </div>
  )
}
