import { isNotifySound, type NotifySound } from './notify'
import { clampPetScale, isPetSlug, PET_DEFAULT_SCALE } from './pet'
import { DEFAULT_QUICK_ASK_SHORTCUT, isQuickAskShortcut, type QuickAskShortcut } from './quick-ask'
import { DEFAULT_THEME_SETTINGS, normalizeThemeSettings, type ThemeSettings } from './theme'

/** Where the desktop shell finds (or launches) the AgentOS gateway. */
export interface GatewaySettings {
  /** `managed`: the app spawns `agentos gateway run`. `external`: connect only. */
  mode: 'managed' | 'external'
  /**
   * How the app reaches the gateway: plain `http` (loopback, a LAN bind, an
   * SSH tunnel) or `https` for a gateway behind TLS (a reverse proxy,
   * Tailscale Serve, `[tls]` in its config). The WebSocket follows suit.
   */
  scheme: GatewayScheme
  host: string
  port: number
  /** Bearer token for gateways running with auth.mode = "token". */
  token: string | null
  /** Explicit path to the `agentos` CLI; null = auto-locate on PATH. */
  cliPath: string | null
}

export type GatewayScheme = 'http' | 'https'

export const DEFAULT_GATEWAY_SETTINGS: GatewaySettings = {
  mode: 'managed',
  scheme: 'http',
  host: '127.0.0.1',
  port: 18791,
  token: null,
  cliPath: null,
}

/** The gateway's base URL as the settings describe it, e.g. `https://gw.example.com:443`. */
export function gatewayUrl(gw: Pick<GatewaySettings, 'scheme' | 'host' | 'port'>): string {
  return `${gw.scheme}://${gw.host}:${gw.port}`
}

/** Behaviour of the shell itself: launch, quit, and how the composer sends. */
export interface GeneralSettings {
  /** Register the app as a macOS login item. Mirrored by main on change. */
  openAtLogin: boolean
  /** Stop a managed gateway when the app quits (off: leave it running). */
  stopGatewayOnQuit: boolean
  /** What the window shows at launch: the home screen or the last session. */
  launchView: 'home' | 'last'
  /** Enter sends and Shift+Enter breaks the line; off swaps them (⌘Enter sends). */
  enterToSend: boolean
  /** The status item in the macOS menu bar (main/tray). Off removes it. */
  showInMenuBar: boolean
}

export const DEFAULT_GENERAL_SETTINGS: GeneralSettings = {
  openAtLogin: false,
  stopGatewayOnQuit: true,
  launchView: 'home',
  enterToSend: true,
  showInMenuBar: true,
}

/** Whole-app zoom, percent. Applied by main as the window's zoom factor. */
export const UI_SCALES = [90, 100, 110, 125, 150, 175] as const
export type UiScale = (typeof UI_SCALES)[number]
export const DEFAULT_UI_SCALE: UiScale = 100

export function isUiScale(value: unknown): value is UiScale {
  return typeof value === 'number' && (UI_SCALES as readonly number[]).includes(value)
}

/** The next step up or down the scale ladder; clamps at the ends. */
export function stepUiScale(current: UiScale, direction: 1 | -1): UiScale {
  const i = UI_SCALES.indexOf(current)
  const next = UI_SCALES[Math.min(UI_SCALES.length - 1, Math.max(0, i + direction))]
  return next ?? DEFAULT_UI_SCALE
}

/** Appearance beyond colour: the theme axes live in `ThemeSettings`. */
export interface AppearanceSettings {
  uiScale: UiScale
  /** Opaque sidebar and no window vibrancy (System Settings > Accessibility). */
  reduceTransparency: boolean
}

export const DEFAULT_APPEARANCE_SETTINGS: AppearanceSettings = {
  uiScale: DEFAULT_UI_SCALE,
  reduceTransparency: false,
}

/** What happens when an event lands while the window is in front. */
export type WhenActive = 'skip' | 'banner' | 'system'
/** Scheduled job runs: none, the failures, or every run. */
export type JobsNotify = 'off' | 'failures' | 'all'
/** "Only replies longer than": seconds; 0 = every reply. */
export const REPLY_MIN_SECONDS = [0, 10, 30, 60, 300] as const
export type ReplyMinSeconds = (typeof REPLY_MIN_SECONDS)[number]

export function isReplyMinSeconds(value: unknown): value is ReplyMinSeconds {
  return typeof value === 'number' && (REPLY_MIN_SECONDS as readonly number[]).includes(value)
}

export interface NotificationSettings {
  /** Master switch. Off: nothing is posted, played or recorded. */
  enabled: boolean
  /** Window in front: skip, an in-app banner, or a system notification anyway. */
  whenActive: WhenActive
  /** Do not disturb: epoch ms until which delivery is silent (recorded only). */
  muteUntil: number | null
  /** Put the session title, reply length, job summary in the notification. */
  preview: boolean
  /** A reply finished in any session. */
  replyDone: boolean
  replyMinSeconds: ReplyMinSeconds
  /** A reply failed or timed out. */
  replyFailed: boolean
  /** The agent is waiting for an approval. */
  approvals: boolean
  jobs: JobsNotify
  /** A swap settled: every one, only failures, or none. Approvals use `approvals`. */
  trades: JobsNotify
  /** The gateway stopped on its own. */
  gateway: boolean
  /** Play `soundName` on delivery. Also toggled from the toolbar bell. */
  sound: boolean
  soundName: NotifySound
  /** Unseen count on the Dock icon. */
  badge: boolean
  /** Bounce the Dock icon when the window is in the background. */
  bounce: boolean
}

export const DEFAULT_NOTIFICATION_SETTINGS: NotificationSettings = {
  enabled: true,
  whenActive: 'banner',
  muteUntil: null,
  preview: true,
  replyDone: true,
  replyMinSeconds: 0,
  replyFailed: true,
  approvals: true,
  jobs: 'failures',
  trades: 'all',
  gateway: true,
  sound: true,
  soundName: 'chime',
  badge: true,
  bounce: true,
}

/** The floating petdex mascot (Settings > Appearance > Pet). */
export interface PetSettings {
  enabled: boolean
  /** Installed pet to show; null until one is picked. */
  slug: string | null
  /** On-screen size relative to the 192×208 frame, 0.1–3. */
  scale: number
}

export const DEFAULT_PET_SETTINGS: PetSettings = {
  enabled: false,
  slug: null,
  scale: PET_DEFAULT_SCALE,
}

/** When the desk asks for a fingerprint (Settings › Security). */
export type TouchIdMode = 'off' | 'high' | 'all'
export const TOUCH_ID_MODES: readonly TouchIdMode[] = ['off', 'high', 'all']

export interface SecuritySettings {
  /**
   * `off`: never. `high`: a high-risk approval, a private-key export and a
   * wallet removal. `all`: every approval as well. Off by default: a Mac
   * without a sensor must not be locked out of anything.
   */
  touchId: TouchIdMode
}

export const DEFAULT_SECURITY_SETTINGS: SecuritySettings = {
  touchId: 'off',
}

export function isTouchIdMode(value: unknown): value is TouchIdMode {
  return typeof value === 'string' && (TOUCH_ID_MODES as readonly string[]).includes(value)
}

/** Quick Ask: the global hotkey and its floating prompt (Settings › Behaviour). */
export interface QuickAskSettings {
  /** Register the hotkey. Off: the key is released at once. */
  enabled: boolean
  /** One of a fixed list; mirrored by main as a global shortcut. */
  shortcut: QuickAskShortcut
}

export const DEFAULT_QUICK_ASK_SETTINGS: QuickAskSettings = {
  enabled: true,
  shortcut: DEFAULT_QUICK_ASK_SHORTCUT,
}

export interface DesktopSettings {
  theme: ThemeSettings
  gateway: GatewaySettings
  general: GeneralSettings
  appearance: AppearanceSettings
  notifications: NotificationSettings
  pet: PetSettings
  security: SecuritySettings
  quickAsk: QuickAskSettings
}

export const DEFAULT_SETTINGS: DesktopSettings = {
  theme: DEFAULT_THEME_SETTINGS,
  gateway: DEFAULT_GATEWAY_SETTINGS,
  general: DEFAULT_GENERAL_SETTINGS,
  appearance: DEFAULT_APPEARANCE_SETTINGS,
  notifications: DEFAULT_NOTIFICATION_SETTINGS,
  pet: DEFAULT_PET_SETTINGS,
  security: DEFAULT_SECURITY_SETTINGS,
  quickAsk: DEFAULT_QUICK_ASK_SETTINGS,
}

export const SETTINGS_SECTIONS = [
  'theme',
  'gateway',
  'general',
  'appearance',
  'notifications',
  'pet',
  'security',
  'quickAsk',
] as const satisfies readonly (keyof DesktopSettings)[]

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
}

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback
}

/** A host is a bare hostname or IP literal: no scheme, path, port or spaces. */
export function isValidHost(value: unknown): value is string {
  if (typeof value !== 'string') return false
  const host = value.trim()
  if (!host || host.length > 253) return false
  // Bracketed IPv6 literal, the only place a colon is allowed.
  if (host.startsWith('[') && host.endsWith(']')) return /^[0-9A-Fa-f:.]+$/.test(host.slice(1, -1))
  if (/[\s/\\:@?#[\]]/.test(host)) return false
  return /^[A-Za-z0-9.-]+$/.test(host)
}

export function isValidPort(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 && value < 65536
}

function normalizeGateway(raw: unknown): GatewaySettings {
  const obj = asRecord(raw)
  const port = Number(obj.port)
  return {
    mode: obj.mode === 'external' ? 'external' : 'managed',
    // A file written before the scheme existed described a plain-http gateway.
    scheme: obj.scheme === 'https' ? 'https' : 'http',
    host: isValidHost(obj.host) ? obj.host.trim() : DEFAULT_GATEWAY_SETTINGS.host,
    port: isValidPort(port) ? port : DEFAULT_GATEWAY_SETTINGS.port,
    token: typeof obj.token === 'string' && obj.token.trim() ? obj.token.trim() : null,
    cliPath: typeof obj.cliPath === 'string' && obj.cliPath.trim() ? obj.cliPath.trim() : null,
  }
}

function normalizeGeneral(raw: unknown): GeneralSettings {
  const obj = asRecord(raw)
  const d = DEFAULT_GENERAL_SETTINGS
  return {
    openAtLogin: bool(obj.openAtLogin, d.openAtLogin),
    stopGatewayOnQuit: bool(obj.stopGatewayOnQuit, d.stopGatewayOnQuit),
    launchView: obj.launchView === 'last' ? 'last' : 'home',
    enterToSend: bool(obj.enterToSend, d.enterToSend),
    showInMenuBar: bool(obj.showInMenuBar, d.showInMenuBar),
  }
}

function normalizeAppearance(raw: unknown): AppearanceSettings {
  const obj = asRecord(raw)
  const d = DEFAULT_APPEARANCE_SETTINGS
  return {
    uiScale: isUiScale(obj.uiScale) ? obj.uiScale : d.uiScale,
    reduceTransparency: bool(obj.reduceTransparency, d.reduceTransparency),
  }
}

function normalizeNotifications(raw: unknown): NotificationSettings {
  const obj = asRecord(raw)
  const d = DEFAULT_NOTIFICATION_SETTINGS
  const mute = Number(obj.muteUntil)
  return {
    enabled: bool(obj.enabled, d.enabled),
    whenActive:
      obj.whenActive === 'skip' || obj.whenActive === 'system' ? obj.whenActive : d.whenActive,
    muteUntil: Number.isFinite(mute) && mute > 0 ? mute : null,
    preview: bool(obj.preview, d.preview),
    replyDone: bool(obj.replyDone, d.replyDone),
    replyMinSeconds: isReplyMinSeconds(obj.replyMinSeconds)
      ? obj.replyMinSeconds
      : d.replyMinSeconds,
    replyFailed: bool(obj.replyFailed, d.replyFailed),
    approvals: bool(obj.approvals, d.approvals),
    jobs: obj.jobs === 'off' || obj.jobs === 'all' ? obj.jobs : d.jobs,
    trades:
      obj.trades === 'off' || obj.trades === 'failures' || obj.trades === 'all'
        ? obj.trades
        : d.trades,
    gateway: bool(obj.gateway, d.gateway),
    sound: bool(obj.sound, d.sound),
    soundName: isNotifySound(obj.soundName) ? obj.soundName : d.soundName,
    badge: bool(obj.badge, d.badge),
    bounce: bool(obj.bounce, d.bounce),
  }
}

function normalizePet(raw: unknown): PetSettings {
  const obj = asRecord(raw)
  const d = DEFAULT_PET_SETTINGS
  return {
    enabled: bool(obj.enabled, d.enabled),
    slug: isPetSlug(obj.slug) ? obj.slug : null,
    scale: typeof obj.scale === 'number' ? clampPetScale(obj.scale) : d.scale,
  }
}

function normalizeSecurity(raw: unknown): SecuritySettings {
  const obj = asRecord(raw)
  return {
    touchId: isTouchIdMode(obj.touchId) ? obj.touchId : DEFAULT_SECURITY_SETTINGS.touchId,
  }
}

function normalizeQuickAsk(raw: unknown): QuickAskSettings {
  const obj = asRecord(raw)
  const d = DEFAULT_QUICK_ASK_SETTINGS
  return {
    enabled: bool(obj.enabled, d.enabled),
    shortcut: isQuickAskShortcut(obj.shortcut) ? obj.shortcut : d.shortcut,
  }
}

/** Validate a settings blob read from disk. Unknown keys are dropped. */
export function normalizeSettings(raw: unknown): DesktopSettings {
  const obj = asRecord(raw)
  return {
    theme: normalizeThemeSettings(obj.theme),
    gateway: normalizeGateway(obj.gateway),
    general: normalizeGeneral(obj.general),
    appearance: normalizeAppearance(obj.appearance),
    notifications: normalizeNotifications(obj.notifications),
    pet: normalizePet(obj.pet),
    security: normalizeSecurity(obj.security),
    quickAsk: normalizeQuickAsk(obj.quickAsk),
  }
}

/** Deep partial used by `settings:update` so callers patch one section. */
export type SettingsPatch = {
  [K in keyof DesktopSettings]?: Partial<DesktopSettings[K]>
}

/** Shallow-merge a patch section by section, then validate the result. */
export function mergeSettings(current: DesktopSettings, patch: SettingsPatch): DesktopSettings {
  const merged: Record<string, unknown> = {}
  for (const section of SETTINGS_SECTIONS) {
    merged[section] = { ...current[section], ...(patch[section] ?? {}) }
  }
  return normalizeSettings(merged)
}
