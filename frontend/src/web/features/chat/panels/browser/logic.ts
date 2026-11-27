// The gateway's browser view (`agentos.gateway.rpc_browser`): method names,
// wire shapes and the address bar's rules. Pure, for tests.

export const STATUS_METHOD = 'browser.status'
export const SNAPSHOT_METHOD = 'browser.snapshot'
export const START_METHOD = 'browser.start'
export const NAVIGATE_METHOD = 'browser.navigate'
export const HISTORY_METHOD = 'browser.history'
export const SELECT_TAB_METHOD = 'browser.tabs.select'

export interface BrowserStatus {
  enabled: boolean
  installed: boolean
  mode: 'managed' | 'attach'
  running: boolean
  attachConfirmed: boolean
  canStart: boolean
  installHint?: string
}

export interface BrowserTabInfo {
  tabId: string
  title: string
  url: string
  active: boolean
}

export interface BrowserSnapshot {
  running: boolean
  tabs: BrowserTabInfo[]
  truncated: boolean
  activeTabId: string | null
  url: string
  title: string
  image: { mime: string; data: string } | null
  /** Set when the page is not captured: `private`. */
  withheld?: string
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function body(raw: unknown): Record<string, unknown> {
  return raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
}

export function normalizeStatus(raw: unknown): BrowserStatus {
  const row = body(raw)
  return {
    enabled: row.enabled === true,
    installed: row.installed === true,
    mode: row.mode === 'attach' ? 'attach' : 'managed',
    running: row.running === true,
    attachConfirmed: row.attachConfirmed !== false,
    canStart: row.canStart === true,
    installHint: str(row.installHint) || undefined,
  }
}

const TAB_ID = /^t[1-9][0-9]{0,8}$/
const IMAGE_MIME = /^image\/(?:jpeg|png|webp)$/
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/

export function normalizeSnapshot(raw: unknown): BrowserSnapshot {
  const row = body(raw)
  const tabs = (Array.isArray(row.tabs) ? row.tabs : [])
    .map((tab) => body(tab))
    .filter((tab) => TAB_ID.test(str(tab.tabId)))
    .map((tab) => ({
      tabId: str(tab.tabId),
      title: str(tab.title),
      url: str(tab.url),
      active: tab.active === true,
    }))
  const image = body(row.image)
  const mime = str(image.mime)
  const data = str(image.data)
  return {
    running: row.running === true,
    tabs,
    truncated: row.truncated === true,
    activeTabId: TAB_ID.test(str(row.activeTabId)) ? str(row.activeTabId) : null,
    url: str(row.url),
    title: str(row.title),
    // Only an image the view can show inline: never a URL, never markup.
    image: IMAGE_MIME.test(mime) && data && BASE64.test(data) ? { mime, data } : null,
    withheld: str(row.withheld) || undefined,
  }
}

/**
 * What the address bar sends, or null when the text is not an address. A bare
 * host gets `https://`; a scheme other than http(s) is refused here, before
 * the gateway refuses it too. Words are not searched: the browser the agent
 * drives would meet a CAPTCHA, and the operator has a browser of their own.
 */
export function normalizeAddress(text: string): string | null {
  const value = text.trim()
  if (!value) return null
  if (value.toLowerCase() === 'about:blank') return 'about:blank'
  if (/^https?:\/\//i.test(value)) {
    try {
      return new URL(value).href
    } catch {
      return null
    }
  }
  if (/^[a-z][a-z0-9+.-]*:(?!\d)/i.test(value)) return null
  if (/\s/.test(value)) return null
  const host = value.split(/[/?#]/, 1)[0] ?? ''
  if (!/^(?:localhost|[\w-]+(?:\.[\w-]+)+|\[[0-9a-f:]+\])(?::\d{1,5})?$/i.test(host)) return null
  try {
    return new URL(`https://${value}`).href
  } catch {
    return null
  }
}

export type BrowserFailure = 'outdated' | 'disabled' | 'not_running' | 'blocked' | 'failed'

/** What an RPC rejection from `browser.*` means for the view. */
export function browserFailure(error: unknown): BrowserFailure {
  const err = body(error) as { code?: unknown; details?: unknown }
  if (err.code === 'METHOD_NOT_FOUND') return 'outdated'
  switch (body(err.details).code) {
    case 'browser.disabled':
    case 'browser.not_installed':
    case 'browser.attach_unconfirmed':
      return 'disabled'
    case 'browser.not_running':
      return 'not_running'
    case 'browser.blocked':
      return 'blocked'
    default:
      return 'failed'
  }
}
