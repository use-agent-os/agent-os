import { useCallback, useRef } from 'react'
import { t } from '~/i18n'
import { SIDEBAR_COLLAPSE_AT, SIDEBAR_MAX, SIDEBAR_MIN, useUi } from '~/stores/ui'

/**
 * The grab strip on the sidebar's right edge. Drag to resize; drag past the
 * collapse threshold and the sidebar snaps closed (dragging back out reopens
 * it); double-click restores the default width. Pointer capture keeps the
 * drag alive when the cursor outruns the strip.
 */
export function SidebarResizer() {
  const setWidth = useUi((s) => s.setSidebarWidth)
  const reset = useUi((s) => s.resetSidebarWidth)
  const toggle = useUi((s) => s.toggleSidebar)
  const stripRef = useRef<HTMLDivElement>(null)

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (e.button !== 0) return
      e.preventDefault()
      stripRef.current?.setAttribute('data-dragging', 'true')
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'

      // Listeners live on the document, not the strip: collapsing unmounts
      // the sidebar (and this strip) mid-drag, and the drag must survive
      // that so dragging back out reopens it.
      const onMove = (ev: PointerEvent) => {
        // The sidebar starts at the window's left edge, so the pointer's x
        // is its would-be width.
        const x = ev.clientX
        if (x < SIDEBAR_COLLAPSE_AT) {
          if (useUi.getState().sidebarOpen) useUi.setState({ sidebarOpen: false })
          return
        }
        setWidth(Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, x)))
      }
      const onUp = () => {
        stripRef.current?.removeAttribute('data-dragging')
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
        document.removeEventListener('pointermove', onMove)
        document.removeEventListener('pointerup', onUp)
        document.removeEventListener('pointercancel', onUp)
      }
      document.addEventListener('pointermove', onMove)
      document.addEventListener('pointerup', onUp)
      document.addEventListener('pointercancel', onUp)
    },
    [setWidth],
  )

  return (
    <div
      ref={stripRef}
      role="separator"
      aria-orientation="vertical"
      aria-label={t('sidebar.resize')}
      title={t('sidebar.resize')}
      className="mac-resizer app-no-drag"
      onPointerDown={onPointerDown}
      onDoubleClick={reset}
      onKeyDown={(e) => {
        if (e.key === 'ArrowLeft') setWidth(useUi.getState().sidebarWidth - 16)
        if (e.key === 'ArrowRight') setWidth(useUi.getState().sidebarWidth + 16)
        if (e.key === 'Enter') toggle()
      }}
      tabIndex={0}
    />
  )
}
