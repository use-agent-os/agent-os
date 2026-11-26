import { Brain, FileText, FolderTree, Globe, TerminalSquare, type LucideIcon } from 'lucide-react'
import { lazy, type ComponentType, type LazyExoticComponent } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import '@/i18n/en/webPanel'
import type { PanelItemKind, PanelTabKind, PanelToolKind } from './panel-store'

// The panel's tab registry. A tool is a tab the + menu opens: one entry here
// (its icon, label, the gateway methods it needs and its lazily loaded body)
// is all it takes to add one — a terminal or a desktop viewer would be the
// next. Item tabs show one transcript item and are opened from the
// transcript, never from the menu.

/** What a tool's body gets from the panel. */
export interface ToolTabProps {
  sessionKey: string
  agentId: string
  /** The tab is the one on screen: a hidden tab stops polling. */
  active: boolean
}

export interface PanelToolDef {
  kind: PanelToolKind
  icon: LucideIcon
  label(): string
  hint(): string
  /** Gateway methods the tool calls first; it is offered only when they are advertised. */
  methods: readonly string[]
  component: LazyExoticComponent<ComponentType<ToolTabProps>>
}

export const PANEL_TOOLS: readonly PanelToolDef[] = [
  {
    kind: 'files',
    icon: FolderTree,
    label: () => t('webPanel.toolFiles'),
    hint: () => t('webPanel.toolFilesHint'),
    methods: ['workspace.files.list', 'workspace.files.read'],
    component: lazy(async () => ({ default: (await import('./files/FilesTab')).FilesTab })),
  },
  {
    kind: 'browser',
    icon: Globe,
    label: () => t('webPanel.toolBrowser'),
    hint: () => t('webPanel.toolBrowserHint'),
    methods: ['browser.status', 'browser.snapshot'],
    component: lazy(async () => ({ default: (await import('./browser/BrowserTab')).BrowserTab })),
  },
]

export interface PanelItemDef {
  icon: LucideIcon
  label(): string
}

export const PANEL_ITEMS: Record<PanelItemKind, PanelItemDef> = {
  'tool-output': { icon: TerminalSquare, label: () => t('webChat.panelToolOutput') },
  artifact: { icon: FileText, label: () => t('webChat.panelArtifact') },
  thinking: { icon: Brain, label: () => t('webChat.panelThinking') },
}

export function toolDef(kind: PanelToolKind): PanelToolDef {
  return PANEL_TOOLS.find((tool) => tool.kind === kind)!
}

/** Icon and label for any tab kind. */
export function tabMeta(kind: PanelTabKind): { icon: LucideIcon; label: string } {
  const tool = PANEL_TOOLS.find((entry) => entry.kind === kind)
  if (tool) return { icon: tool.icon, label: tool.label() }
  const item = PANEL_ITEMS[kind as PanelItemKind]
  return { icon: item.icon, label: item.label() }
}

/**
 * Whether a gateway offers a tool. `methods` is the handshake's list; null
 * means the gateway did not say, and the tool is offered and degrades on a
 * METHOD_NOT_FOUND instead.
 */
export function toolAvailable(tool: PanelToolDef, methods: readonly string[] | null): boolean {
  if (!methods) return true
  return tool.methods.every((method) => methods.includes(method))
}
