import { useRef, type KeyboardEvent, type PointerEvent } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { PagesList } from './PagesList'
import { SessionList } from './SessionList'
import { LIST_PANE_MAX, LIST_PANE_MIN, useShell } from './shell-store'

/** The list pane beside the rail: Sessions or Pages, resizable. */
export function ListPane({
  onNavigate,
  resizable = true,
}: {
  onNavigate?: () => void
  resizable?: boolean
}) {
  const view = useShell((s) => s.view)
  const width = useShell((s) => s.paneWidth)
  const setWidth = useShell((s) => s.setPaneWidth)
  const drag = useRef<{ startX: number; startWidth: number } | null>(null)

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    drag.current = { startX: e.clientX, startWidth: width }
    e.currentTarget.setPointerCapture?.(e.pointerId)
  }
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return
    setWidth(drag.current.startWidth + e.clientX - drag.current.startX, false)
  }
  const onPointerUp = (e: PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return
    setWidth(drag.current.startWidth + e.clientX - drag.current.startX)
    drag.current = null
  }
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 48 : 16
    if (e.key === 'ArrowLeft') setWidth(width - step)
    else if (e.key === 'ArrowRight') setWidth(width + step)
    else if (e.key === 'Home') setWidth(LIST_PANE_MIN)
    else if (e.key === 'End') setWidth(LIST_PANE_MAX)
    else return
    e.preventDefault()
  }

  return (
    <div
      data-testid="list-pane"
      aria-label={t('webShell.listPaneLabel')}
      role="region"
      style={resizable ? { width } : undefined}
      className="relative flex h-full min-h-0 shrink-0 flex-col border-r border-border bg-surface-1 max-md:w-full"
    >
      {view === 'sessions' ? (
        <SessionList onNavigate={onNavigate} />
      ) : (
        <PagesList onNavigate={onNavigate} />
      )}
      {resizable ? (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label={t('webShell.listPaneResize')}
          aria-valuemin={LIST_PANE_MIN}
          aria-valuemax={LIST_PANE_MAX}
          aria-valuenow={width}
          tabIndex={0}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onKeyDown={onKeyDown}
          className="pane-resizer absolute inset-y-0 -right-1 z-10 w-2 cursor-col-resize touch-none"
        />
      ) : null}
    </div>
  )
}
