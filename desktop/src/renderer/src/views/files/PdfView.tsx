import { LoaderCircle, TriangleAlert } from 'lucide-react'
import {
  getDocument,
  GlobalWorkerOptions,
  type PDFDocumentProxy,
  type RenderTask,
} from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { useEffect, useRef, useState } from 'react'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { fill } from './text'

// The parser runs in a worker bundled with the app (script-src 'self'): a
// malformed PDF can at worst stall that worker, never the window.
GlobalWorkerOptions.workerSrc = workerUrl

/** Pages laid out at once; the rest come in on request. */
const PAGE_BATCH = 20
/** Canvas pixels per CSS pixel, capped: a retina page at 3x is 9x the memory. */
const MAX_SCALE = 2
/** Widths are rounded to this, so dragging the window does not re-render per pixel. */
const WIDTH_STEP = 48

type State =
  { status: 'loading' } | { status: 'failed' } | { status: 'ready'; doc: PDFDocumentProxy }

/**
 * A PDF drawn to canvases by pdf.js. The bytes come from a ticketed gateway
 * URL; nothing in the document can script, navigate or fetch: there is no
 * annotation layer, no forms, no embedded-file handling, only pixels.
 */
export default function PdfView({ url }: { url: string }) {
  const [state, setState] = useState<State>({ status: 'loading' })
  const [shown, setShown] = useState(PAGE_BATCH)
  const [width, setWidth] = useState(0)
  const frame = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()
    let task: ReturnType<typeof getDocument> | null = null
    ;(async () => {
      try {
        const response = await fetch(url, { credentials: 'omit', signal: controller.signal })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const data = new Uint8Array(await response.arrayBuffer())
        if (cancelled) return
        task = getDocument({
          data,
          // No WebAssembly: the window's CSP does not allow it, and a preview
          // does not need the speed.
          useWasm: false,
          // Never reach out for anything the file names.
          useWorkerFetch: false,
          enableXfa: false,
          stopAtErrors: false,
        })
        const doc = await task.promise
        if (cancelled) {
          void task.destroy()
          return
        }
        setState({ status: 'ready', doc })
      } catch {
        if (!cancelled) setState({ status: 'failed' })
      }
    })()
    return () => {
      cancelled = true
      controller.abort()
      void task?.destroy()
    }
  }, [url])

  useEffect(() => {
    const element = frame.current
    if (!element) return
    const measure = () => {
      const next = Math.floor(element.clientWidth / WIDTH_STEP) * WIDTH_STEP
      setWidth((current) => (next > 0 && next !== current ? next : current))
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [state.status])

  if (state.status === 'failed') {
    return (
      <div className="files-state" role="alert">
        <TriangleAlert className="size-9 text-dim" strokeWidth={1.25} aria-hidden />
        <p className="files-state__title">{t('files.error.failed.title')}</p>
        <p className="files-state__body">{t('files.pdf.failed')}</p>
      </div>
    )
  }
  if (state.status === 'loading') {
    return (
      <div className="files-state" role="status" aria-label={t('files.loading')}>
        <LoaderCircle className="size-5 animate-spin text-dim" strokeWidth={1.75} aria-hidden />
      </div>
    )
  }

  const total = state.doc.numPages
  const count = Math.min(shown, total)
  return (
    <div ref={frame} className="files-pdf">
      <p className="files-pdf__count">{fill(t('files.pdf.pages'), { count: total })}</p>
      {width > 0
        ? Array.from({ length: count }, (_, i) => (
            <PdfPage key={`${i}:${width}`} doc={state.doc} number={i + 1} width={width} />
          ))
        : null}
      {count < total ? (
        <Button onClick={() => setShown((n) => n + PAGE_BATCH)}>
          {fill(t('files.pdf.more'), { count: Math.min(PAGE_BATCH, total - count) })}
        </Button>
      ) : null}
    </div>
  )
}

/** One page: reserves its box at once, draws when it scrolls near the viewport. */
function PdfPage({ doc, number, width }: { doc: PDFDocumentProxy; number: number; width: number }) {
  const holder = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const [ratio, setRatio] = useState(1.3)
  const [near, setNear] = useState(false)

  useEffect(() => {
    const element = holder.current
    if (!element) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setNear(true)
          observer.disconnect()
        }
      },
      { rootMargin: '600px 0px' },
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!near) return
    let cancelled = false
    let task: RenderTask | null = null
    ;(async () => {
      try {
        const page = await doc.getPage(number)
        if (cancelled || !canvas.current) return
        const base = page.getViewport({ scale: 1 })
        setRatio(base.height / base.width)
        const scale = (width / base.width) * Math.min(window.devicePixelRatio || 1, MAX_SCALE)
        const viewport = page.getViewport({ scale })
        const target = canvas.current
        target.width = Math.floor(viewport.width)
        target.height = Math.floor(viewport.height)
        task = page.render({ canvas: target, viewport })
        await task.promise
      } catch {
        // A cancelled or failed page leaves its blank box; the others still draw.
      }
    })()
    return () => {
      cancelled = true
      task?.cancel()
    }
  }, [doc, number, width, near])

  return (
    <div
      ref={holder}
      className="files-pdf__page"
      style={{ width, height: Math.round(width * ratio) }}
      role="img"
      aria-label={fill(t('files.pdf.page'), { page: number })}
    >
      <canvas ref={canvas} />
    </div>
  )
}
