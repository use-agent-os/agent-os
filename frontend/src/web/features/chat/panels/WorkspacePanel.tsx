import {
  Check,
  Maximize2,
  Minimize2,
  PanelBottom,
  PanelBottomClose,
  PanelRight,
  PanelRightClose,
  Plus,
  SlidersHorizontal,
  X,
} from 'lucide-react'
import {
  Suspense,
  useEffect,
  useId,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
  type PointerEvent,
} from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import '@/i18n/en/webPanel'
import { cn } from '@/lib/utils'
import {
  artifactDownloadUrl,
  artifactMime,
  isImageArtifact,
} from '@/views/chat/transcript/artifacts'
import { Button } from '@/web/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuItemIndicator,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'
import { Skeleton } from '@/web/components/ui/states'
import { SimpleTooltip, Tooltip, TooltipContent, TooltipTrigger } from '@/web/components/ui/tooltip'
import { useGlide } from '@/web/components/ui/use-glide'
import '@/web/styles/chat-thread.css'
import '@/web/styles/workspace-panel.css'
import { Markdown } from '../markdown'
import type { Item, TranscriptState } from '../model/types'
import { useGatewayMethods } from './hooks'
import { dragSize, keySize, maxSize, type Presentation } from './layout'
import {
  isToolKind,
  PANEL_HEIGHT_MIN,
  PANEL_ID,
  PANEL_TOGGLE_ID,
  SIDE_PANEL_MIN,
  usePanels,
  type PanelDock,
  type PanelTab,
  type PanelToolKind,
} from './panel-store'
import { PANEL_TOOLS, tabMeta, toolAvailable, toolDef } from './registry'

const TAB_DRAG_TYPE = 'application/x-agentos-panel-tab'

function domId(prefix: string, id: string): string {
  return `${prefix}-${id.replace(/[^A-Za-z0-9_-]/g, '_')}`
}

function tabTitle(tab: PanelTab): string {
  return tab.title || tabMeta(tab.kind).label
}

function isRtl(node: Element | null): boolean {
  return node ? getComputedStyle(node).direction === 'rtl' : false
}

function findItem(state: TranscriptState, id: string): Item | undefined {
  for (const turn of state.turns) {
    const item = turn.items.find((i) => i.id === id)
    if (item) return item
  }
  return undefined
}

/* ── Item tabs ────────────────────────────────────────────────────────── */

function ItemBody({ item }: { item: Item | undefined }) {
  if (!item) return <p className="text-text-muted">{t('webChat.panelEmpty')}</p>
  if (item.kind === 'toolCall') {
    return (
      <pre tabIndex={0} className="chat-well chat-side-well">
        {item.result ?? ''}
      </pre>
    )
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

function ItemTab({ tab, transcript }: { tab: PanelTab; transcript: TranscriptState }) {
  const meta = tabMeta(tab.kind)
  const Icon = meta.icon
  return (
    <div className="ws-item scroll-contained">
      <div className="ws-item-head">
        <span className="chat-tile" aria-hidden>
          <Icon />
        </span>
        <div className="min-w-0">
          <p className="chat-overline">{meta.label}</p>
          <p className="truncate text-ui font-semibold">{tab.title}</p>
        </div>
      </div>
      <ItemBody item={findItem(transcript, tab.id)} />
    </div>
  )
}

/* ── Tool tabs ────────────────────────────────────────────────────────── */

function ToolSkeleton() {
  return (
    <div className="flex flex-col gap-2 p-4" aria-hidden>
      <Skeleton className="h-8 w-full" />
      <Skeleton className="h-5 w-3/4" />
      <Skeleton className="h-5 w-2/3" />
      <Skeleton className="h-5 w-4/5" />
    </div>
  )
}

function ToolTab(props: {
  kind: PanelToolKind
  sessionKey: string
  agentId: string
  active: boolean
}) {
  const Body = toolDef(props.kind).component
  return (
    <Suspense fallback={<ToolSkeleton />}>
      <Body sessionKey={props.sessionKey} agentId={props.agentId} active={props.active} />
    </Suspense>
  )
}

/* ── Launcher (no tabs) ───────────────────────────────────────────────── */

function Launcher({ methods }: { methods: readonly string[] | null }) {
  const openTool = usePanels((s) => s.openTool)
  return (
    <div className="ws-launcher">
      <p className="ws-launcher-title">{t('webPanel.emptyTitle')}</p>
      <p className="ws-launcher-body">{t('webPanel.emptyBody')}</p>
      <ul className="ws-launcher-list">
        {PANEL_TOOLS.map((tool) => {
          const Icon = tool.icon
          const available = toolAvailable(tool, methods)
          return (
            <li key={tool.kind}>
              <button
                type="button"
                disabled={!available}
                onClick={(e) => openTool(tool.kind, e.currentTarget)}
                className="ws-launch press target"
              >
                <span className="chat-tile" data-tone="accent" aria-hidden>
                  <Icon />
                </span>
                <span className="min-w-0 flex-1 text-start">
                  <span className="block font-medium">{tool.label()}</span>
                  <span className="block truncate text-small text-text-muted">
                    {available ? tool.hint() : t('webPanel.toolUnavailable')}
                  </span>
                </span>
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

/* ── Header: tabs and actions ─────────────────────────────────────────── */

interface DropTarget {
  id: string
  after: boolean
}

function TabStrip({ announce }: { announce(message: string): void }) {
  const tabs = usePanels((s) => s.tabs)
  const activeId = usePanels((s) => s.activeId)
  const setActive = usePanels((s) => s.setActive)
  const closeTab = usePanels((s) => s.closeTab)
  const moveTab = usePanels((s) => s.moveTab)
  const glide = useGlide<HTMLDivElement>('.ws-tab[data-active="true"]')
  const [dragging, setDragging] = useState<string | null>(null)
  const [drop, setDrop] = useState<DropTarget | null>(null)
  const describedBy = useId()

  // The selected tab stays in view when tabs overflow the strip.
  useEffect(() => {
    if (!activeId) return
    const node = document.getElementById(domId('ws-tab', activeId))
    node?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
  }, [activeId, tabs.length])

  const focusTab = (id: string | undefined) => {
    if (!id) return
    requestAnimationFrame(() => document.getElementById(domId('ws-tab', id))?.focus())
  }

  const onTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>, tab: PanelTab, index: number) => {
    const rtl = isRtl(e.currentTarget)
    const next = rtl ? 'ArrowLeft' : 'ArrowRight'
    const prev = rtl ? 'ArrowRight' : 'ArrowLeft'
    const moving = (e.ctrlKey || e.metaKey) && e.shiftKey
    let target: number | null = null
    if (e.key === next) target = index + 1
    else if (e.key === prev) target = index - 1
    else if (e.key === 'Home') target = 0
    else if (e.key === 'End') target = tabs.length - 1
    if (target !== null) {
      e.preventDefault()
      const bounded = Math.max(0, Math.min(tabs.length - 1, target))
      if (moving && (e.key === next || e.key === prev)) {
        if (bounded === index) return
        moveTab(tab.id, bounded)
        announce(
          t('webPanel.tabMoved', {
            title: tabTitle(tab),
            position: bounded + 1,
            count: tabs.length,
          }),
        )
        focusTab(tab.id)
        return
      }
      const to = tabs[bounded]
      if (to) {
        setActive(to.id)
        focusTab(to.id)
      }
      return
    }
    if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault()
      closeTab(tab.id)
      const after = usePanels.getState()
      focusTab(after.activeId ?? undefined)
    }
  }

  const onDragStart = (e: DragEvent<HTMLDivElement>, tab: PanelTab) => {
    e.dataTransfer.setData(TAB_DRAG_TYPE, tab.id)
    e.dataTransfer.effectAllowed = 'move'
    setDragging(tab.id)
  }
  const onDragOver = (e: DragEvent<HTMLDivElement>, tab: PanelTab) => {
    if (!dragging) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    const rect = e.currentTarget.getBoundingClientRect()
    const rtl = isRtl(e.currentTarget)
    const pastMiddle = e.clientX > rect.left + rect.width / 2
    const after = rtl ? !pastMiddle : pastMiddle
    if (drop?.id !== tab.id || drop.after !== after) setDrop({ id: tab.id, after })
  }
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    const id = dragging ?? e.dataTransfer.getData(TAB_DRAG_TYPE)
    if (id && drop && id !== drop.id) {
      const from = tabs.findIndex((t) => t.id === id)
      const over = tabs.findIndex((t) => t.id === drop.id)
      let to = drop.after ? over + 1 : over
      if (from < to) to -= 1
      moveTab(id, to)
      const moved = tabs[from]
      if (moved) {
        announce(
          t('webPanel.tabMoved', { title: tabTitle(moved), position: to + 1, count: tabs.length }),
        )
      }
    }
    setDragging(null)
    setDrop(null)
  }

  return (
    <div
      ref={glide}
      role="tablist"
      aria-label={t('webPanel.tablist')}
      aria-describedby={describedBy}
      className="ws-tabs glide-track"
    >
      {tabs.map((tab, index) => {
        const { icon: Icon } = tabMeta(tab.kind)
        const active = tab.id === activeId
        const title = tabTitle(tab)
        return (
          <div
            key={tab.id}
            data-active={active}
            data-dragging={dragging === tab.id || undefined}
            data-drop={drop?.id === tab.id ? (drop.after ? 'after' : 'before') : undefined}
            draggable
            onDragStart={(e) => onDragStart(e, tab)}
            onDragOver={(e) => onDragOver(e, tab)}
            onDrop={onDrop}
            onDragEnd={() => {
              setDragging(null)
              setDrop(null)
            }}
            onAuxClick={(e) => {
              // Middle-click closes a tab, as in a browser.
              if (e.button === 1) closeTab(tab.id)
            }}
            className="ws-tab"
          >
            <button
              type="button"
              role="tab"
              id={domId('ws-tab', tab.id)}
              aria-selected={active}
              aria-label={title}
              aria-controls={domId('ws-tabpanel', tab.id)}
              aria-keyshortcuts="Delete Control+Shift+ArrowLeft Control+Shift+ArrowRight"
              tabIndex={active ? 0 : -1}
              title={title}
              onClick={() => setActive(tab.id)}
              onKeyDown={(e) => onTabKeyDown(e, tab, index)}
              className="ws-tab-button target"
            >
              <Icon aria-hidden />
              <span className="truncate">{title}</span>
            </button>
            {/* Pointer only: a tablist may hold nothing but tabs, so the
                keyboard closes a tab with Delete (announced on the tab). */}
            <span
              aria-hidden
              title={t('webPanel.closeTab', { title })}
              onClick={() => {
                closeTab(tab.id)
                const after = usePanels.getState()
                focusTab(after.activeId ?? undefined)
              }}
              className="ws-tab-close"
            >
              <X />
            </span>
          </div>
        )
      })}
      <span id={describedBy} hidden>
        {t('webPanel.tabKeys')}
      </span>
    </div>
  )
}

/**
 * An icon button that opens a menu. Its tooltip names it on hover and focus
 * but never sits over its own open menu, nor reappears when focus comes back
 * to it as the menu closes.
 */
function MenuButton({
  label,
  icon,
  children,
  contentClassName,
  onCloseAutoFocus,
}: {
  label: string
  icon: React.ReactNode
  children: React.ReactNode
  contentClassName?: string
  onCloseAutoFocus?: (event: Event) => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [tipOpen, setTipOpen] = useState(false)
  return (
    <DropdownMenu
      open={menuOpen}
      onOpenChange={(next) => {
        setMenuOpen(next)
        setTipOpen(false)
      }}
    >
      <Tooltip open={tipOpen && !menuOpen} onOpenChange={(next) => setTipOpen(next && !menuOpen)}>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button size="icon-sm" variant="ghost" aria-label={label} className="chat-head-action">
              {icon}
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent side="bottom">{label}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent
        align="end"
        className={cn('chat-menu ws-menu', contentClassName)}
        onCloseAutoFocus={(e) => {
          onCloseAutoFocus?.(e)
          // Focus returning to the trigger is not a request for its tooltip.
          requestAnimationFrame(() => setTipOpen(false))
        }}
      >
        {children}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function AddMenu({ methods }: { methods: readonly string[] | null }) {
  const openTool = usePanels((s) => s.openTool)
  const opened = useRef<PanelToolKind | null>(null)
  return (
    <MenuButton
      label={t('webPanel.add')}
      icon={<Plus />}
      onCloseAutoFocus={(e) => {
        // The new tab takes focus, not the + button.
        const kind = opened.current
        opened.current = null
        if (!kind) return
        e.preventDefault()
        document.getElementById(domId('ws-tab', `tool:${kind}`))?.focus()
      }}
    >
      <DropdownMenuLabel>{t('webPanel.addMenuLabel')}</DropdownMenuLabel>
      {PANEL_TOOLS.map((tool) => {
        const Icon = tool.icon
        const available = toolAvailable(tool, methods)
        return (
          <DropdownMenuItem
            key={tool.kind}
            disabled={!available}
            className="chat-menu-item ws-menu-item"
            onSelect={() => {
              opened.current = tool.kind
              openTool(tool.kind, document.getElementById(PANEL_TOGGLE_ID))
            }}
          >
            <Icon aria-hidden />
            <span className="flex min-w-0 flex-col">
              <span>{tool.label()}</span>
              <span className="truncate text-small text-text-muted">
                {available ? tool.hint() : t('webPanel.toolUnavailable')}
              </span>
            </span>
          </DropdownMenuItem>
        )
      })}
    </MenuButton>
  )
}

function LayoutMenu({ presentation }: { presentation: Presentation }) {
  const dock = usePanels((s) => s.dock)
  const expanded = usePanels((s) => s.expanded)
  const setDock = usePanels((s) => s.setDock)
  const setExpanded = usePanels((s) => s.setExpanded)
  return (
    <MenuButton label={t('webPanel.layout')} icon={<SlidersHorizontal />}>
      <DropdownMenuRadioGroup value={dock} onValueChange={(v) => setDock(v as PanelDock)}>
        <DropdownMenuRadioItem value="right" className="chat-menu-item ws-menu-item">
          <PanelRight aria-hidden />
          <span className="flex-1">{t('webPanel.dockRight')}</span>
          <DropdownMenuItemIndicator>
            <Check aria-hidden />
          </DropdownMenuItemIndicator>
        </DropdownMenuRadioItem>
        <DropdownMenuRadioItem value="bottom" className="chat-menu-item ws-menu-item">
          <PanelBottom aria-hidden />
          <span className="flex-1">{t('webPanel.dockBottom')}</span>
          <DropdownMenuItemIndicator>
            <Check aria-hidden />
          </DropdownMenuItemIndicator>
        </DropdownMenuRadioItem>
      </DropdownMenuRadioGroup>
      <DropdownMenuSeparator />
      <DropdownMenuCheckboxItem
        checked={expanded}
        disabled={presentation === 'takeover'}
        onCheckedChange={(checked) => setExpanded(checked === true)}
        className="chat-menu-item ws-menu-item"
      >
        <Maximize2 aria-hidden />
        <span className="flex-1">{t('webPanel.expand')}</span>
        <DropdownMenuItemIndicator>
          <Check aria-hidden />
        </DropdownMenuItemIndicator>
      </DropdownMenuCheckboxItem>
    </MenuButton>
  )
}

/* ── Divider ──────────────────────────────────────────────────────────── */

function Resizer({
  axis,
  size,
  container,
}: {
  axis: 'x' | 'y'
  /** The size shown now (the saved one, fitted to the room). */
  size: number
  container: number
}) {
  const setWidth = usePanels((s) => s.setWidth)
  const setHeight = usePanels((s) => s.setHeight)
  const drag = useRef<{ from: number; start: number; rtl: boolean } | null>(null)
  const set = axis === 'x' ? setWidth : setHeight
  const min = axis === 'x' ? SIDE_PANEL_MIN : PANEL_HEIGHT_MIN
  const max = maxSize(axis, container)
  const at = (e: PointerEvent) => (axis === 'x' ? e.clientX : e.clientY)
  const sized = (e: PointerEvent) => {
    const d = drag.current!
    return Math.max(min, Math.min(max, dragSize(axis, d.start, d.from, at(e), d.rtl)))
  }

  return (
    <div
      role="separator"
      aria-orientation={axis === 'x' ? 'vertical' : 'horizontal'}
      aria-label={t(axis === 'x' ? 'webPanel.resizeWidth' : 'webPanel.resizeHeight')}
      aria-controls={PANEL_ID}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={size}
      tabIndex={0}
      data-axis={axis}
      onPointerDown={(e) => {
        if (e.button !== 0) return
        e.preventDefault()
        drag.current = { from: at(e), start: size, rtl: isRtl(e.currentTarget) }
        e.currentTarget.setPointerCapture?.(e.pointerId)
        document.documentElement.dataset.panelResizing = axis
      }}
      onPointerMove={(e) => {
        if (drag.current) set(sized(e), false)
      }}
      onPointerUp={(e) => {
        if (!drag.current) return
        set(sized(e))
        drag.current = null
        delete document.documentElement.dataset.panelResizing
      }}
      onPointerCancel={() => {
        drag.current = null
        delete document.documentElement.dataset.panelResizing
      }}
      onKeyDown={(e) => {
        const next = keySize(axis, e.key, size, {
          min,
          max,
          rtl: isRtl(e.currentTarget),
          large: e.shiftKey,
        })
        if (next === null) return
        e.preventDefault()
        set(next)
      }}
      className="ws-resize"
    />
  )
}

/* ── The panel ────────────────────────────────────────────────────────── */

export interface WorkspacePanelProps {
  transcript: TranscriptState
  sessionKey: string
  agentId: string
  presentation: Presentation
  /** The panel's size along its dock axis, already fitted to the room. */
  size: number
  /** The workbench's size along the same axis, for the divider's bounds. */
  container: number
}

/**
 * The chat's workspace panel: tool tabs (Files, Browser) and transcript item
 * tabs in one tab strip, docked right or bottom, expanded over the chat, or a
 * full-screen sheet on phones. Escape hides it and returns focus to whatever
 * opened it.
 */
export function WorkspacePanel(props: WorkspacePanelProps) {
  const { presentation, sessionKey } = props
  const bound = usePanels((s) => s.sessionKey)
  const tabs = usePanels((s) => s.tabs)
  const activeId = usePanels((s) => s.activeId)
  const shown = usePanels((s) => s.visible)
  const dock = usePanels((s) => s.dock)
  const focusTick = usePanels((s) => s.focusTick)
  const setVisible = usePanels((s) => s.setVisible)
  const setExpanded = usePanels((s) => s.setExpanded)
  const methods = useGatewayMethods()
  const ref = useRef<HTMLElement>(null)
  const [announcement, setAnnouncement] = useState('')

  const visible = bound === sessionKey && shown
  const active = tabs.find((tab) => tab.id === activeId) ?? tabs[0]

  // Opened on purpose (the toggle, the shortcut, a tool or a transcript
  // button): focus goes to the selected tab, or the panel's first control.
  const seenTick = useRef(focusTick)
  useEffect(() => {
    if (!visible || focusTick === seenTick.current) return
    seenTick.current = focusTick
    const frame = requestAnimationFrame(() => {
      const tab = active ? document.getElementById(domId('ws-tab', active.id)) : null
      // With nothing open, the first tool to open (not a control with a tooltip).
      const target =
        tab ??
        ref.current?.querySelector<HTMLElement>('.ws-launch:not(:disabled)') ??
        ref.current?.querySelector<HTMLElement>('button:not(:disabled)')
      target?.focus()
    })
    return () => cancelAnimationFrame(frame)
  }, [visible, focusTick, active])

  if (!visible) return null

  const hide = () => {
    const opener = usePanels.getState().opener
    setVisible(false)
    const target = opener?.isConnected ? opener : document.getElementById(PANEL_TOGGLE_ID)
    requestAnimationFrame(() => target?.focus())
  }
  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key !== 'Escape' || e.defaultPrevented) return
    // Menus render in a portal; their Escape closes the menu, not the panel.
    if (!ref.current?.contains(e.target as Node)) return
    e.stopPropagation()
    hide()
  }

  const docked = presentation === 'right' || presentation === 'bottom'
  const overlay = presentation === 'expanded' || presentation === 'takeover'
  const HideIcon = !docked ? X : dock === 'bottom' ? PanelBottomClose : PanelRightClose
  const hideLabel = docked ? t('webPanel.minimize') : t('webPanel.backToChat')
  const style =
    presentation === 'right'
      ? { width: props.size }
      : presentation === 'bottom'
        ? { height: props.size }
        : undefined

  return (
    <aside
      ref={ref}
      id={PANEL_ID}
      aria-label={t('webPanel.region')}
      data-presentation={presentation}
      onKeyDown={onKeyDown}
      style={style}
      className="ws-panel chat-panel-in"
    >
      {docked ? (
        <Resizer
          axis={presentation === 'right' ? 'x' : 'y'}
          size={props.size}
          container={props.container}
        />
      ) : null}
      <header className="ws-panel-head">
        {tabs.length ? (
          <TabStrip announce={setAnnouncement} />
        ) : (
          <p className="ws-panel-label">{t('webPanel.region')}</p>
        )}
        <div className="ws-panel-actions">
          <AddMenu methods={methods} />
          {presentation !== 'sheet' ? <LayoutMenu presentation={presentation} /> : null}
          {presentation === 'expanded' ? (
            <SimpleTooltip side="bottom" label={t('webPanel.restore')}>
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={t('webPanel.restore')}
                onClick={() => setExpanded(false)}
                className="chat-head-action"
              >
                <Minimize2 />
              </Button>
            </SimpleTooltip>
          ) : null}
          <SimpleTooltip side="bottom" label={hideLabel}>
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label={hideLabel}
              aria-keyshortcuts="Escape"
              onClick={hide}
              className="chat-head-action"
            >
              <HideIcon />
            </Button>
          </SimpleTooltip>
        </div>
      </header>
      <div className={cn('ws-panel-body', overlay && 'ws-panel-body--wide')}>
        {tabs.length === 0 ? <Launcher methods={methods} /> : null}
        {tabs.map((tab) => {
          const shown = tab.id === active?.id
          return (
            <div
              key={tab.id}
              role="tabpanel"
              id={domId('ws-tabpanel', tab.id)}
              aria-labelledby={domId('ws-tab', tab.id)}
              hidden={!shown}
              className="ws-tabpanel"
            >
              {isToolKind(tab.kind) ? (
                <ToolTab
                  kind={tab.kind}
                  sessionKey={sessionKey}
                  agentId={props.agentId}
                  active={shown}
                />
              ) : shown ? (
                <ItemTab tab={tab} transcript={props.transcript} />
              ) : null}
            </div>
          )
        })}
      </div>
      <div role="status" aria-live="polite" className="sr-only">
        {announcement}
      </div>
    </aside>
  )
}
