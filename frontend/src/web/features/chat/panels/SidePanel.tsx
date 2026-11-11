import { X } from 'lucide-react'
import { useRef, type KeyboardEvent, type PointerEvent } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { cn } from '@/lib/utils'
import {
  artifactDownloadUrl,
  artifactMime,
  isImageArtifact,
} from '@/views/chat/transcript/artifacts'
import { Button } from '@/web/components/ui/button'
import { Markdown } from '../markdown'
import type { Item, TranscriptState } from '../model/types'
import { usePanels, SIDE_PANEL_MAX, SIDE_PANEL_MIN } from './panel-store'

function findItem(state: TranscriptState, id: string): Item | undefined {
  for (const turn of state.turns) {
    const item = turn.items.find((i) => i.id === id)
    if (item) return item
  }
  return undefined
}

function TabBody({ item }: { item: Item | undefined }) {
  if (!item) return <p className="text-text-muted">{t('webChat.panelEmpty')}</p>
  if (item.kind === 'toolCall') {
    return <pre className="chat-well">{item.result ?? ''}</pre>
  }
  if (item.kind === 'thinkingRef') {
    return <div className="whitespace-pre-wrap text-text-muted">{item.text ?? ''}</div>
  }
  if (item.kind === 'artifact' || item.kind === 'card') {
    const url = artifactDownloadUrl(item.artifact)
    if (isImageArtifact(item.artifact))
      return <img src={url} alt="" className="max-w-full rounded-card border border-border" />
    const mime = artifactMime(item.artifact)
    if (
      mime === 'text/markdown' ||
      mime === 'text/plain' ||
      mime === 'application/pdf' ||
      mime === 'text/html'
    ) {
      return (
        <iframe
          title={String(item.artifact.name ?? '')}
          src={url}
          sandbox=""
          className="h-full min-h-[60dvh] w-full rounded-card border border-border bg-white shadow-card"
        />
      )
    }
    return (
      <a href={url} download className="text-accent underline">
        {String(item.artifact.name ?? url)}
      </a>
    )
  }
  if (item.kind === 'text') return <Markdown text={item.text} />
  return null
}

/**
 * The side panel (chat spec "Side panel"): tool output, artifacts and
 * reasoning as tabs, resizable (width remembered), Escape closes and returns
 * focus to the opener. A full-height sheet on phones.
 */
export function SidePanel({ transcript, phone }: { transcript: TranscriptState; phone: boolean }) {
  const { tabs, activeId, width, setActive, closeTab, closeAll, setWidth, opener } = usePanels()
  const drag = useRef<{ x: number; w: number } | null>(null)
  if (tabs.length === 0) return null

  const close = () => {
    closeAll()
    if (opener?.isConnected) opener.focus()
  }
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.stopPropagation()
      close()
    }
  }
  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    drag.current = { x: e.clientX, w: width }
    e.currentTarget.setPointerCapture?.(e.pointerId)
  }
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    if (drag.current) setWidth(drag.current.w + (drag.current.x - e.clientX), false)
  }
  const onPointerUp = (e: PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return
    setWidth(drag.current.w + (drag.current.x - e.clientX))
    drag.current = null
  }
  const active = tabs.find((tab) => tab.id === activeId) ?? tabs[0]!

  return (
    <aside
      aria-label={t('webChat.panelTitle')}
      onKeyDown={onKeyDown}
      style={phone ? undefined : { width }}
      className={cn(
        'chat-panel-in chat-side flex flex-col border-l border-border bg-surface-1',
        phone ? 'fixed inset-0 z-[var(--z-drawer)]' : 'relative h-full shrink-0',
      )}
    >
      {!phone ? (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label={t('webChat.panelResize')}
          aria-valuemin={SIDE_PANEL_MIN}
          aria-valuemax={SIDE_PANEL_MAX}
          aria-valuenow={width}
          tabIndex={0}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onKeyDown={(e) => {
            if (e.key === 'ArrowLeft') setWidth(width + 16)
            else if (e.key === 'ArrowRight') setWidth(width - 16)
          }}
          className="chat-resize"
        />
      ) : null}
      <div
        role="tablist"
        aria-label={t('webChat.panelTitle')}
        className="flex min-h-12 items-center gap-1 overflow-x-auto border-b border-border px-2 py-1.5"
      >
        {tabs.map((tab) => (
          <div
            key={tab.id}
            className={cn(
              'flex items-center rounded-control transition-colors duration-[var(--duration-fast)]',
              tab.id === active.id ? 'bg-surface-3 shadow-highlight' : 'hover:bg-surface-2',
            )}
          >
            <button
              type="button"
              role="tab"
              aria-selected={tab.id === active.id}
              onClick={() => setActive(tab.id)}
              className={cn(
                'target max-w-48 truncate rounded-control py-1 pr-1 pl-2.5 text-ui',
                tab.id === active.id ? 'font-medium text-text' : 'text-text-muted',
              )}
            >
              {tab.title}
            </button>
            <button
              type="button"
              aria-label={t('webChat.panelCloseTab', { title: tab.title })}
              onClick={() => closeTab(tab.id)}
              className="target mr-0.5 inline-flex items-center justify-center rounded-md text-text-muted hover:text-text"
            >
              <X className="size-3.5" aria-hidden />
            </button>
          </div>
        ))}
        <span className="flex-1" />
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label={t('webChat.panelClose')}
          onClick={close}
          className="text-text-muted hover:text-text"
        >
          <X />
        </Button>
      </div>
      <div
        role="tabpanel"
        tabIndex={0}
        className="scroll-contained flex-1 p-4 focus-visible:-outline-offset-2"
      >
        <TabBody item={findItem(transcript, active.id)} />
      </div>
    </aside>
  )
}
