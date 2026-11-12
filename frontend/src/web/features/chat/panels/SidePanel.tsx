import { Brain, FileText, TerminalSquare, X } from 'lucide-react'
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
import { useGlide } from '@/web/components/ui/use-glide'
import '@/web/styles/chat-thread.css'
import { Markdown } from '../markdown'
import type { Item, TranscriptState } from '../model/types'
import { usePanels, SIDE_PANEL_MAX, SIDE_PANEL_MIN, type PanelTabKind } from './panel-store'

const KIND: Record<PanelTabKind, { icon: React.ComponentType; label: () => string }> = {
  'tool-output': { icon: TerminalSquare, label: () => t('webChat.panelToolOutput') },
  artifact: { icon: FileText, label: () => t('webChat.panelArtifact') },
  thinking: { icon: Brain, label: () => t('webChat.panelThinking') },
}

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
    return <pre className="chat-well chat-side-well">{item.result ?? ''}</pre>
  }
  if (item.kind === 'thinkingRef') {
    return <div className="chat-thought-text">{item.text ?? ''}</div>
  }
  if (item.kind === 'artifact' || item.kind === 'card') {
    const url = artifactDownloadUrl(item.artifact)
    if (isImageArtifact(item.artifact))
      return <img src={url} alt="" className="chat-side-media max-w-full" />
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
          className="chat-side-media h-full min-h-[60dvh] w-full bg-white"
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
  const glide = useGlide<HTMLDivElement>()
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
  const ActiveIcon = KIND[active.kind].icon

  return (
    <aside
      aria-label={t('webChat.panelTitle')}
      onKeyDown={onKeyDown}
      style={phone ? undefined : { width }}
      className={cn(
        'chat-panel-in chat-side flex flex-col',
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
      <header className="chat-side-head">
        <span className="chat-tile" aria-hidden>
          <ActiveIcon />
        </span>
        <div className="min-w-0 flex-1">
          <p className="chat-overline">{KIND[active.kind].label()}</p>
          <p className="truncate text-ui font-semibold">{active.title}</p>
        </div>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label={t('webChat.panelClose')}
          onClick={close}
          className="-mr-1 text-text-muted hover:text-text"
        >
          <X />
        </Button>
      </header>
      <div
        ref={glide}
        role="tablist"
        aria-label={t('webChat.panelTitle')}
        className="chat-side-tabs glide-track"
      >
        {tabs.map((tab) => {
          const Icon = KIND[tab.kind].icon
          return (
            <div key={tab.id} data-active={tab.id === active.id} className="chat-side-tab">
              <button
                type="button"
                role="tab"
                aria-selected={tab.id === active.id}
                onClick={() => setActive(tab.id)}
                className="target flex max-w-48 min-w-0 items-center gap-1.5 py-1 pr-0.5 pl-2.5"
              >
                <Icon aria-hidden />
                <span className="truncate">{tab.title}</span>
              </button>
              <button
                type="button"
                aria-label={t('webChat.panelCloseTab', { title: tab.title })}
                onClick={() => closeTab(tab.id)}
                className="chat-side-tab-close target"
              >
                <X aria-hidden />
              </button>
            </div>
          )
        })}
      </div>
      <div
        role="tabpanel"
        tabIndex={0}
        className="scroll-contained flex-1 px-4 pt-3 pb-4 focus-visible:-outline-offset-2"
      >
        <TabBody item={findItem(transcript, active.id)} />
      </div>
    </aside>
  )
}
