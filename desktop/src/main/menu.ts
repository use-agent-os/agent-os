import { app, Menu, shell, type MenuItemConstructorOptions } from 'electron'
import { DEFAULT_UI_SCALE, stepUiScale } from '@shared/settings'
import { requestOpenSettings } from './ipc/app'
import type { SettingsStore } from './settings/store'

const REPO_URL = 'https://github.com/use-agent-os/agent-os'

/**
 * Standard macOS menu bar. "Settings…" sits where every Mac app keeps it
 * (app menu, ⌘,) and opens the renderer's Settings window over IPC. The
 * View menu's zoom items step Settings > Appearance > UI scale and persist
 * it, instead of Electron's per-load zoom that a reload forgets.
 */
export function installAppMenu(settings: SettingsStore): void {
  const zoom = (direction: 1 | -1 | 0) => {
    const current = settings.get().appearance.uiScale
    const uiScale = direction === 0 ? DEFAULT_UI_SCALE : stepUiScale(current, direction)
    if (uiScale !== current) settings.update({ appearance: { uiScale } })
  }
  const template: MenuItemConstructorOptions[] = [
    {
      label: app.name,
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { label: 'Settings…', accelerator: 'Command+,', click: () => requestOpenSettings() },
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    { role: 'fileMenu' },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { label: 'Actual Size', accelerator: 'CommandOrControl+0', click: () => zoom(0) },
        { label: 'Zoom In', accelerator: 'CommandOrControl+=', click: () => zoom(1) },
        // ⌘+ is Shift+= on most layouts; a hidden twin keeps the menu tidy
        // while Settings > Shortcuts can honestly list ⌘+ (macOS fires
        // accelerators of hidden items by default).
        {
          label: 'Zoom In',
          accelerator: 'CommandOrControl+Shift+=',
          visible: false,
          click: () => zoom(1),
        },
        { label: 'Zoom Out', accelerator: 'CommandOrControl+-', click: () => zoom(-1) },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
    {
      role: 'help',
      submenu: [
        { label: 'AgentOS on GitHub', click: () => void shell.openExternal(REPO_URL) },
        {
          label: 'Report an Issue…',
          click: () => void shell.openExternal(`${REPO_URL}/issues/new`),
        },
        { type: 'separator' },
        { label: `AgentOS ${app.getVersion()}`, enabled: false },
      ],
    },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}
