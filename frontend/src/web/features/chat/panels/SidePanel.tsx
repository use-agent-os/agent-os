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
    return <pre className="font-mono text-small whitespace-pre-wrap">{item.result ?? ''}</pre>
  }
  if (item.kind === 'thinkingRef') {
    return <div className="whitespace-pre-wrap">{item.text ?? ''}</div>
  }
  if (item.kind === 'artifact' || item.kind === 'card') {
    const url = artifactDownloadUrl(item.artifact)
    if (isImageArtifact(item.artifact)) return <img src={url} alt="" className="max-w-full" />
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
          className="h-full min-h-[60dvh] w-full rounded border border-border bg-white"
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
        'flex flex-col border-l border-border bg-surface-1',
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
          className="absolute inset-y-0 -left-1 w-2 cursor-col-resize hover:bg-accent/40"
        />
      ) : null}
      <div
        role="tablist"
        aria-label={t('webChat.panelTitle')}
        className="flex items-center gap-1 overflow-x-auto border-b border-border px-2 py-1"
      >
        {tabs.map((tab) => (
          <div key={tab.id} className="flex items-center">
            <button
              type="button"
              role="tab"
              aria-selected={tab.id === active.id}
              onClick={() => setActive(tab.id)}
              className={cn(
                'target max-w-48 truncate rounded px-2 text-ui',
                tab.id === active.id ? 'bg-surface-3' : 'text-text-muted',
              )}
            >
              {tab.title}
            </button>
            <button
              type="button"
              aria-label={t('webChat.panelCloseTab', { title: tab.title })}
              onClick={() => closeTab(tab.id)}
              className="target inline-flex items-center justify-center rounded text-text-muted hover:text-text"
            >
              <X className="size-3.5" aria-hidden />
            </button>
          </div>
        ))}
        <span className="flex-1" />
        <Button size="icon-sm" variant="ghost" aria-label={t('webChat.panelClose')} onClick={close}>
          <X />
        </Button>
      </div>
      <div role="tabpanel" className="scroll-contained flex-1 p-3">
        <TabBody item={findItem(transcript, active.id)} />
      </div>
    </aside>
  )
}
