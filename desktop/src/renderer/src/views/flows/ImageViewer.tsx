import { ChevronLeft, ChevronRight, X } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { ModalShell } from '@/components/ModalShell'
import { t } from '~/i18n'
import { useFlowMedia, useFlowThumb } from '~/stores/flows'
import type { ImageRef } from './types'

/** The images to page through and where to start: the exact list the opener showed. */
export interface ViewerSource {
  images: ImageRef[]
  index: number
}

/**
 * One image at full size, through `flows.media` without a size cap. The
 * thumbnail already in the cache stands in while the full blob arrives.
 * ←/→ step through the run's images; Escape closes.
 */
export function ImageViewer({ source, onClose }: { source: ViewerSource; onClose: () => void }) {
  const images = source.images
  const [index, setIndex] = useState(source.index)
  const at = images.length ? Math.min(index, images.length - 1) : 0
  const image = images[at] ?? null
  const titleId = useId()

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight' && images.length > 1) setIndex((i) => (i + 1) % images.length)
      if (e.key === 'ArrowLeft' && images.length > 1)
        setIndex((i) => (i - 1 + images.length) % images.length)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [images.length])

  return (
    <ModalShell
      role="dialog"
      labelledBy={titleId}
      onClose={onClose}
      overlayClassName="flw-viewer__overlay"
      className="flw-viewer"
    >
      <h2 id={titleId} className="sr-only">
        {t('flows.viewer.label')}
      </h2>
      <div className="flw-viewer__stage">
        {image ? <FullImage key={image.sha256} image={image} /> : <ViewerNote loading={false} />}
      </div>
      <div className="flw-viewer__bar">
        <span className="flw-viewer__meta">
          {image
            ? `${image.width}×${image.height} · ${image.mime.replace('image/', '').toUpperCase()}`
            : ''}
          {images.length > 1 ? ` · ${at + 1} / ${images.length}` : ''}
        </span>
        <span className="flex-1" />
        {images.length > 1 ? (
          <>
            <button
              type="button"
              className="flw-icon-btn"
              aria-label={t('flows.viewer.prev')}
              onClick={() => setIndex((i) => (i - 1 + images.length) % images.length)}
            >
              <ChevronLeft className="size-4" strokeWidth={1.75} aria-hidden />
            </button>
            <button
              type="button"
              className="flw-icon-btn"
              aria-label={t('flows.viewer.next')}
              onClick={() => setIndex((i) => (i + 1) % images.length)}
            >
              <ChevronRight className="size-4" strokeWidth={1.75} aria-hidden />
            </button>
          </>
        ) : null}
        <button
          type="button"
          className="flw-icon-btn"
          aria-label={t('flows.viewer.close')}
          onClick={onClose}
        >
          <X className="size-4" strokeWidth={1.75} aria-hidden />
        </button>
      </div>
    </ModalShell>
  )
}

function FullImage({ image }: { image: ImageRef }) {
  const thumb = useFlowThumb(image)
  const full = useFlowMedia(image.sha256)
  const src = full.src ?? thumb.src
  if (!src) return <ViewerNote loading={!full.isError} />
  return (
    <img
      src={src}
      alt=""
      draggable={false}
      className="flw-viewer__img"
      data-preview={!full.src || undefined}
      style={{ aspectRatio: `${image.width || 1} / ${image.height || 1}` }}
    />
  )
}

function ViewerNote({ loading }: { loading: boolean }) {
  return (
    <p className="flw-viewer__note">
      {loading ? t('flows.viewer.loading') : t('flows.viewer.failed')}
    </p>
  )
}
