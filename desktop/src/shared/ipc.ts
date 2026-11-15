import type { AppInfo, AuthResult, BiometricsInfo, ChooseFileOptions } from './app'
import type { GatewayStatus } from './gateway'
import type { NotifyRequest, NotifyResult, NotifyTarget, SystemSound } from './notify'
import type { InstalledPet, PetManifestEntry } from './pet'
import type { QuickAskStatus, QuickAskSubmission } from './quick-ask'
import type { DesktopSettings, SettingsPatch } from './settings'
import type { ResolvedTheme, ThemeSettings } from './theme'
import type { AppUpdateState, EngineUpdateState } from './updates'
import type { BootstrapState } from './bootstrap'
import type { TraySummary } from './tray'

export type { SettingsPatch } from './settings'

/**
 * Single source of truth for IPC channel names. Main registers handlers,
 * preload invokes them, renderer only ever sees the typed `DesktopApi`.
 */
export const IPC = {
  settings: {
    get: 'settings:get',
    update: 'settings:update',
    reset: 'settings:reset',
    /** Main -> renderer: the menu bar (⌘,) asked for the Settings window. */
    open: 'settings:open',
  },
  theme: {
    /** Renderer -> main: persist + apply nativeTheme.themeSource. */
    set: 'theme:set',
    /** Renderer -> main: ask what the OS currently resolves to. */
    resolved: 'theme:resolved',
    /** Main -> renderer: OS appearance changed. */
    changed: 'theme:changed',
  },
  gateway: {
    status: 'gateway:status',
    start: 'gateway:start',
    stop: 'gateway:stop',
    restart: 'gateway:restart',
    operatorSecret: 'gateway:operatorSecret',
    /** Main -> renderer: status transitions. */
    changed: 'gateway:changed',
  },
  pets: {
    manifest: 'pets:manifest',
    installed: 'pets:installed',
    install: 'pets:install',
    remove: 'pets:remove',
    preview: 'pets:preview',
    importFolder: 'pets:importFolder',
  },
  app: {
    version: 'app:version',
    info: 'app:info',
    openExternal: 'app:openExternal',
    showItemInFolder: 'app:showItemInFolder',
    openPath: 'app:openPath',
    chooseFile: 'app:chooseFile',
    loginItem: 'app:loginItem',
    biometrics: 'app:biometrics',
    authenticate: 'app:authenticate',
    scrubClipboard: 'app:scrubClipboard',
  },
  notify: {
    supported: 'notify:supported',
    show: 'notify:show',
    sound: 'notify:sound',
    badge: 'notify:badge',
    bounce: 'notify:bounce',
    openSystemSettings: 'notify:openSystemSettings',
    /** Main -> renderer: a notification was clicked; carries its target. */
    activated: 'notify:activated',
  },
  updates: {
    engineState: 'updates:engineState',
    engineCheck: 'updates:engineCheck',
    engineApply: 'updates:engineApply',
    /** Main -> renderer: engine update progress. */
    engineChanged: 'updates:engineChanged',
    appState: 'updates:appState',
    appCheck: 'updates:appCheck',
    appDownload: 'updates:appDownload',
    appInstall: 'updates:appInstall',
    /** Main -> renderer: app update progress. */
    appChanged: 'updates:appChanged',
  },
  bootstrap: {
    state: 'bootstrap:state',
    install: 'bootstrap:install',
    cancel: 'bootstrap:cancel',
    connectExisting: 'bootstrap:connectExisting',
    reinstall: 'bootstrap:reinstall',
    uninstallEngine: 'bootstrap:uninstallEngine',
    openLog: 'bootstrap:openLog',
    /** Main -> renderer: install progress. */
    changed: 'bootstrap:changed',
  },
  tray: {
    /** Renderer -> main (send, no reply): what the menu bar item says. Debounced. */
    summary: 'tray:summary',
    /** Main -> renderer: a menu bar row asked to open something; carries a NotifyTarget. */
    navigate: 'tray:navigate',
  },
  quickAsk: {
    /** Panel -> main: send this text to the main window. */
    submit: 'quickAsk:submit',
    /** Panel -> main: Escape; close without sending. */
    hide: 'quickAsk:hide',
    /** Panel -> main: the view has mounted; the panel may be shown. */
    ready: 'quickAsk:ready',
    /** Panel -> main: the content's height changed; size the panel to it. */
    resize: 'quickAsk:resize',
    /** Main -> panel: the panel is on screen (focus the field, re-read theme). */
    shown: 'quickAsk:shown',
    /** Main -> main window: a submission is waiting; collect it with `take`. */
    deliver: 'quickAsk:deliver',
    /** Main window -> main: every submission not yet collected, oldest first. */
    take: 'quickAsk:take',
    /** Where the hotkey stands (registered, refused, off). */
    status: 'quickAsk:status',
    /** Main -> renderer: the hotkey's status changed. */
    statusChanged: 'quickAsk:statusChanged',
  },
} as const

/**
 * The surface exposed on `window.agentos` by the preload script. Kept here so
 * main, preload and renderer type-check against the same shape.
 */
export interface DesktopApi {
  app: {
    version(): Promise<string>
    info(): Promise<AppInfo>
    /** Open an http(s) URL in the default browser. Other schemes are refused. */
    openExternal(url: string): Promise<void>
    /** Reveal a file in Finder. */
    showItemInFolder(path: string): Promise<void>
    /** Open a file or folder with its default app. Resolves to '' or an error message. */
    openPath(path: string): Promise<string>
    /** Native open sheet; null when cancelled. */
    chooseFile(options?: ChooseFileOptions): Promise<string | null>
    /** What macOS reports for the login item, not what settings say. */
    loginItem(): Promise<boolean>
    /** Whether Touch ID can be asked for right now (sensor present, enrolled, lid open). */
    biometrics(): Promise<BiometricsInfo>
    /**
     * Ask for a fingerprint through the macOS Touch ID sheet, which shows
     * `reason` (≤ 120 characters). Biometrics only: never a password fallback.
     */
    authenticate(reason: string): Promise<AuthResult>
    /**
     * Blank the clipboard in `afterMs` (clamped to 1 s – 10 min), and at
     * once if the app quits first, when it still holds the text whose
     * SHA-256 hex is `digest`. Owned by main: it outlives the view that
     * copied. False when the arguments were refused.
     */
    scrubClipboard(digest: string, afterMs: number): Promise<boolean>
  }
  settings: {
    get(): Promise<DesktopSettings>
    update(patch: SettingsPatch): Promise<DesktopSettings>
    reset(): Promise<DesktopSettings>
    onOpenRequested(listener: () => void): () => void
  }
  theme: {
    set(next: Partial<ThemeSettings>): Promise<ThemeSettings>
    resolved(): Promise<ResolvedTheme>
    onChanged(listener: (resolved: ResolvedTheme) => void): () => void
  }
  pets: {
    /** Every approved pet on petdex.dev (cached; empty when offline). */
    manifest(): Promise<PetManifestEntry[]>
    installed(): Promise<InstalledPet[]>
    /** Download pet.json + spritesheet into the pets directory. */
    install(slug: string): Promise<InstalledPet>
    remove(slug: string): Promise<void>
    /** Make the sheet loadable for a gallery preview; resolves to its URL. */
    preview(slug: string): Promise<string>
    /**
     * Ask for a folder (`pet.json` + `spritesheet.webp`) and copy it into
     * the pets directory. Null when the dialog was cancelled.
     */
    importFolder(): Promise<InstalledPet | null>
  }
  gateway: {
    status(): Promise<GatewayStatus>
    start(): Promise<GatewayStatus>
    stop(): Promise<GatewayStatus>
    restart(): Promise<GatewayStatus>
    /**
     * The secret this app handed the gateway it spawned; presented at the
     * WebSocket handshake so the gateway knows the connection is the
     * operator's, never an agent's. Null for an adopted or external gateway.
     */
    operatorSecret(): Promise<string | null>
    onChanged(listener: (status: GatewayStatus) => void): () => void
  }
  notify: {
    /** Whether this platform can post native notifications at all. */
    supported(): Promise<boolean>
    /** Post one. The renderer has already decided it should be shown. */
    show(request: NotifyRequest): Promise<NotifyResult>
    /** Play a macOS alert sound, notification or not. */
    sound(name: SystemSound): Promise<void>
    /** Dock badge: 0 clears it. */
    badge(count: number): Promise<void>
    /** One informational bounce of the Dock icon. */
    bounce(): Promise<void>
    /** System Settings › Notifications, where the user allows the app. */
    openSystemSettings(): Promise<void>
    onActivated(listener: (target: NotifyTarget) => void): () => void
  }
  updates: {
    /** The engine: the `use-agent-os` package behind `agentos gateway run`. */
    engine: {
      state(): Promise<EngineUpdateState>
      /** `agentos upgrade --check`: what is installed, what is published. */
      check(): Promise<EngineUpdateState>
      /** `agentos upgrade`, then restart the managed gateway. Resolves when done. */
      apply(): Promise<EngineUpdateState>
      onChanged(listener: (state: EngineUpdateState) => void): () => void
    }
    /** This app, via electron-updater and GitHub Releases. */
    app: {
      state(): Promise<AppUpdateState>
      check(): Promise<AppUpdateState>
      download(): Promise<AppUpdateState>
      /** Stops the gateway, quits, installs, relaunches. */
      install(): Promise<AppUpdateState>
      onChanged(listener: (state: AppUpdateState) => void): () => void
    }
  }
  /** First-run engine install: main drives install.sh stage by stage. */
  bootstrap: {
    state(): Promise<BootstrapState>
    /** Run every stage, then start the gateway. Resolves when the run ends. */
    install(): Promise<BootstrapState>
    cancel(): Promise<BootstrapState>
    /** Skip installing: switch to external mode and connect. */
    connectExisting(): Promise<BootstrapState>
    /** Install this app's engine again over whatever is there. */
    reinstall(): Promise<BootstrapState>
    /** Stop the gateway and `uv tool uninstall use-agent-os`. */
    uninstallEngine(): Promise<{ ok: boolean; detail: string }>
    /** Reveal the per-run log in Finder. */
    openLog(): Promise<void>
    onChanged(listener: (state: BootstrapState) => void): () => void
  }
  /** The macOS menu bar item (main/tray). */
  tray: {
    /**
     * Tell main what the menu should say about the gateway's work. Fire and
     * forget; main validates and clamps it. The first push also tells main
     * this window is ready for `onNavigate`.
     */
    setSummary(summary: TraySummary): void
    /** A menu bar row (New chat, approvals, next DCA buy) points somewhere. */
    onNavigate(listener: (target: NotifyTarget) => void): () => void
  }
  /**
   * Quick Ask. The panel half (submit, hide, ready, resize, onShown) is used
   * by the floating prompt; the main window collects what it sent (take,
   * onDeliver). Text crosses IPC only; the panel has no gateway connection.
   */
  quickAsk: {
    /** Resolves false when main refused the payload (empty, too long, malformed). */
    submit(submission: QuickAskSubmission): Promise<boolean>
    hide(): Promise<void>
    ready(): Promise<void>
    resize(height: number): Promise<void>
    onShown(listener: () => void): () => void
    take(): Promise<QuickAskSubmission[]>
    onDeliver(listener: () => void): () => void
    status(): Promise<QuickAskStatus>
    onStatusChanged(listener: (status: QuickAskStatus) => void): () => void
  }
}
