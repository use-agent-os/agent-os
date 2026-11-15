import { sha256 } from '@noble/hashes/sha2.js'
import { bytesToHex, utf8ToBytes } from '@noble/hashes/utils.js'
import type { AppInfo } from '@shared/app'
import type { DesktopApi } from '@shared/ipc'
import { STOPPED_GATEWAY, type GatewayStatus } from '@shared/gateway'
import type { NotifyTarget } from '@shared/notify'
import { parseQuickAskSubmission, type QuickAskSubmission } from '@shared/quick-ask'
import {
  DEFAULT_SETTINGS,
  mergeSettings,
  normalizeSettings,
  type DesktopSettings,
} from '@shared/settings'
import { resolveTheme, type ResolvedTheme, type ThemeSettings } from '@shared/theme'
import { IDLE_ENGINE, idleAppState, type AppUpdateState } from '@shared/updates'
import { INITIAL_BOOTSTRAP, type BootstrapState } from '@shared/bootstrap'

const FALLBACK_KEY = 'agentos-desktop.settings'

const BROWSER_INFO: AppInfo = {
  version: 'browser',
  electron: '',
  chrome: '',
  node: '',
  platform: 'browser',
  arch: '',
  packaged: false,
  paths: { userData: '', settings: `localStorage:${FALLBACK_KEY}`, logs: '' },
}

/**
 * Browser/vitest stand-in for the preload bridge. Persists to localStorage so
 * `npm run dev` in a plain browser tab and unit tests still behave; the
 * gateway section is inert because nothing can spawn a process from here.
 */
function createFallbackApi(): DesktopApi {
  let settings: DesktopSettings = load()
  const themeListeners = new Set<(r: ResolvedTheme) => void>()
  const quickAskInbox: QuickAskSubmission[] = []
  const deliverListeners = new Set<() => void>()

  function load(): DesktopSettings {
    try {
      const raw = localStorage.getItem(FALLBACK_KEY)
      return raw ? normalizeSettings(JSON.parse(raw)) : structuredClone(DEFAULT_SETTINGS)
    } catch {
      return structuredClone(DEFAULT_SETTINGS)
    }
  }
  function save(next: DesktopSettings): DesktopSettings {
    settings = normalizeSettings(next)
    try {
      localStorage.setItem(FALLBACK_KEY, JSON.stringify(settings))
    } catch {
      /* storage blocked: keep in memory */
    }
    return structuredClone(settings)
  }
  function systemDark(): boolean {
    try {
      return window.matchMedia('(prefers-color-scheme: dark)').matches
    } catch {
      return false
    }
  }
  const stoppedOnly = async (): Promise<GatewayStatus> => ({ ...STOPPED_GATEWAY })
  const cannotControl = async (): Promise<GatewayStatus> => ({
    ...STOPPED_GATEWAY,
    state: 'error',
    error: 'Gateway control is only available inside the desktop app.',
  })

  return {
    app: {
      version: async () => BROWSER_INFO.version,
      info: async () => structuredClone(BROWSER_INFO),
      openExternal: async (url) => {
        window.open(url, '_blank', 'noopener')
      },
      showItemInFolder: async () => {},
      openPath: async () => 'Only available inside the desktop app.',
      chooseFile: async () => null,
      loginItem: async () => settings.general.openAtLogin,
      // No sensor reaches a browser tab: Touch ID is never available here.
      biometrics: async () => ({ available: false }),
      authenticate: async () => ({ ok: false, reason: 'unavailable' }),
      scrubClipboard: async (digest, afterMs) => scheduleTabScrub(digest, afterMs),
    },
    settings: {
      get: async () => structuredClone(settings),
      update: async (patch) => save(mergeSettings(settings, patch)),
      reset: async () => save(structuredClone(DEFAULT_SETTINGS)),
      onOpenRequested: () => () => {},
    },
    theme: {
      set: async (next: Partial<ThemeSettings>) => {
        const saved = save({ ...settings, theme: { ...settings.theme, ...next } })
        const resolved = resolveTheme(saved.theme.preference, systemDark())
        for (const fn of themeListeners) fn(resolved)
        return saved.theme
      },
      resolved: async () => resolveTheme(settings.theme.preference, systemDark()),
      onChanged: (listener) => {
        themeListeners.add(listener)
        return () => themeListeners.delete(listener)
      },
    },
    pets: {
      manifest: async () => [],
      installed: async () => [],
      install: async () => {
        throw new Error('Pets are only available inside the desktop app.')
      },
      remove: async () => {},
      preview: async () => {
        throw new Error('Pets are only available inside the desktop app.')
      },
      importFolder: async () => {
        throw new Error('Pets are only available inside the desktop app.')
      },
    },
    gateway: {
      status: stoppedOnly,
      start: cannotControl,
      stop: stoppedOnly,
      restart: cannotControl,
      operatorSecret: async () => null,
      onChanged: () => () => {},
    },
    // A browser tab has the web Notification API and nothing else: no Dock,
    // no system sounds, and a click can only focus the tab.
    notify: {
      supported: async () =>
        typeof Notification !== 'undefined' && Notification.permission !== 'denied',
      show: async (request) => {
        if (typeof Notification === 'undefined') return { shown: false }
        if (Notification.permission === 'default') await Notification.requestPermission()
        if (Notification.permission !== 'granted') return { shown: false }
        try {
          const body = [request.subtitle, request.body].filter(Boolean).join('\n')
          const n = new Notification(request.title, { body, tag: request.tag, silent: true })
          n.onclick = () => {
            window.focus()
            for (const fn of activationListeners) fn(request.target)
          }
          return { shown: true }
        } catch {
          return { shown: false }
        }
      },
      sound: async () => {},
      badge: async () => {},
      bounce: async () => {},
      openSystemSettings: async () => {},
      onActivated: (listener) => {
        activationListeners.add(listener)
        return () => activationListeners.delete(listener)
      },
    },
    // Nothing can run an installer or swap an app bundle from a browser tab.
    updates: {
      engine: {
        state: async () => ({ ...IDLE_ENGINE }),
        check: async () => ({
          ...IDLE_ENGINE,
          phase: 'error',
          error: 'Engine updates are only available inside the desktop app.',
        }),
        apply: async () => ({
          ...IDLE_ENGINE,
          phase: 'error',
          error: 'Engine updates are only available inside the desktop app.',
        }),
        onChanged: () => () => {},
      },
      app: {
        state: async (): Promise<AppUpdateState> => ({
          ...idleAppState(BROWSER_INFO.version),
          phase: 'unsupported',
        }),
        check: async () => ({ ...idleAppState(BROWSER_INFO.version), phase: 'unsupported' }),
        download: async () => ({ ...idleAppState(BROWSER_INFO.version), phase: 'unsupported' }),
        install: async () => ({ ...idleAppState(BROWSER_INFO.version), phase: 'unsupported' }),
        onChanged: () => () => {},
      },
    },
    // A browser tab has no engine to install; report "ready" so the shell
    // renders and the setup overlay stays out of the way.
    bootstrap: {
      state: async (): Promise<BootstrapState> => ({ ...INITIAL_BOOTSTRAP, phase: 'ready' }),
      install: async () => ({ ...INITIAL_BOOTSTRAP, phase: 'ready' }),
      cancel: async () => ({ ...INITIAL_BOOTSTRAP, phase: 'ready' }),
      connectExisting: async () => ({ ...INITIAL_BOOTSTRAP, phase: 'ready' }),
      reinstall: async () => ({ ...INITIAL_BOOTSTRAP, phase: 'ready' }),
      uninstallEngine: async () => ({
        ok: false,
        detail: 'Only available inside the desktop app.',
      }),
      openLog: async () => {},
      onChanged: () => () => {},
    },
    // No menu bar in a browser tab: nothing to tell, nothing to click.
    tray: {
      setSummary: () => {},
      onNavigate: () => () => {},
    },
    // No global hotkey in a browser tab. A submission is still validated and
    // delivered to this same page, so the chat's receiving end can be driven
    // from a test or the console.
    quickAsk: {
      submit: async (submission) => {
        const parsed = parseQuickAskSubmission(submission)
        if (!parsed) return false
        quickAskInbox.push(parsed)
        for (const fn of deliverListeners) fn()
        return true
      },
      hide: async () => {},
      ready: async () => {},
      resize: async () => {},
      onShown: () => () => {},
      take: async () => quickAskInbox.splice(0),
      onDeliver: (listener) => {
        deliverListeners.add(listener)
        return () => deliverListeners.delete(listener)
      },
      status: async () => ({
        state: settings.quickAsk.enabled ? 'unavailable' : 'off',
        shortcut: settings.quickAsk.shortcut,
      }),
      onStatusChanged: () => () => {},
    },
  }
}

const activationListeners = new Set<(target: NotifyTarget) => void>()

/**
 * A browser tab's stand-in for main's clipboard scrub: a module-level timer
 * (it outlives any view, as main's does) that blanks the clipboard if it
 * still holds the text with `digest`. Same bounds as main.
 */
const tabScrubs = new Map<string, ReturnType<typeof setTimeout>>()
function scheduleTabScrub(digest: string, afterMs: number): boolean {
  if (!/^[0-9a-f]{64}$/.test(digest) || !Number.isFinite(afterMs)) return false
  const previous = tabScrubs.get(digest)
  if (previous !== undefined) clearTimeout(previous)
  const ms = Math.min(Math.max(afterMs, 1_000), 10 * 60_000)
  tabScrubs.set(
    digest,
    setTimeout(() => {
      tabScrubs.delete(digest)
      void (async () => {
        try {
          const current = await navigator.clipboard.readText()
          if (current && bytesToHex(sha256(utf8ToBytes(current))) === digest)
            await navigator.clipboard.writeText('')
        } catch {
          /* clipboard unreadable: nothing to do */
        }
      })()
    }, ms),
  )
  return true
}

let cached: DesktopApi | null = null

/** True when running inside the Electron shell with the preload bridge. */
export function isDesktop(): boolean {
  return typeof window !== 'undefined' && !!window.agentos
}

export function desktopApi(): DesktopApi {
  if (typeof window !== 'undefined' && window.agentos) return window.agentos
  cached ??= createFallbackApi()
  return cached
}

/** Test hook: drop the fallback instance so each test starts clean. */
export function resetDesktopApiForTests(): void {
  cached = null
}
