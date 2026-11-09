import { create } from 'zustand'

// Browser-local console preferences (overhaul-control-ui D7). These never
// reach the gateway: they describe how this browser renders and types, not how
// the agent behaves. The theme keeps its own store and key (stores/theme.ts).

export const PREFERENCES_KEY = 'agentos.ui.prefs.v1'

export type SendShortcut = 'enter' | 'mod-enter'
export type TextSize = 'small' | 'default' | 'large'

export interface Preferences {
  /** `enter`: Enter sends, Shift+Enter breaks. `mod-enter`: Mod+Enter sends. */
  sendShortcut: SendShortcut
  /** Drives `--text-scale` through `data-text-size` on <html>. */
  textSize: TextSize
  showThinking: boolean
  visualEffects: boolean
}

export const DEFAULT_PREFERENCES: Preferences = {
  sendShortcut: 'enter',
  textSize: 'default',
  showThinking: true,
  visualEffects: true,
}

const VALIDATORS: { [K in keyof Preferences]: (v: unknown) => v is Preferences[K] } = {
  sendShortcut: (v): v is SendShortcut => v === 'enter' || v === 'mod-enter',
  textSize: (v): v is TextSize => v === 'small' || v === 'default' || v === 'large',
  showThinking: (v): v is boolean => typeof v === 'boolean',
  visualEffects: (v): v is boolean => typeof v === 'boolean',
}

/** Parse stored JSON, keeping each valid field and defaulting the rest. */
export function parsePreferences(raw: string | null): Preferences {
  const out: Preferences = { ...DEFAULT_PREFERENCES }
  if (!raw) return out
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return out
  }
  if (!parsed || typeof parsed !== 'object') return out
  const record = parsed as Record<string, unknown>
  for (const key of Object.keys(VALIDATORS) as (keyof Preferences)[]) {
    const value = record[key]
    if (VALIDATORS[key](value)) (out as unknown as Record<string, unknown>)[key] = value
  }
  return out
}

function load(): Preferences {
  try {
    return parsePreferences(localStorage.getItem(PREFERENCES_KEY))
  } catch {
    return { ...DEFAULT_PREFERENCES }
  }
}

function save(prefs: Preferences): void {
  try {
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify(prefs))
  } catch {
    /* storage unavailable or full: the change still applies to this tab */
  }
}

export function applyTextSize(size: TextSize): void {
  const root = document.documentElement
  if (size === 'default') root.removeAttribute('data-text-size')
  else root.setAttribute('data-text-size', size)
}

interface PreferencesState extends Preferences {
  setPreference<K extends keyof Preferences>(key: K, value: Preferences[K]): void
  reset(): void
}

export const usePreferences = create<PreferencesState>((set, get) => ({
  ...load(),
  setPreference(key, value) {
    if (!VALIDATORS[key](value)) return
    const next: Preferences = { ...pick(get()), [key]: value }
    save(next)
    if (key === 'textSize') applyTextSize(value as TextSize)
    set({ [key]: value } as Partial<PreferencesState>)
  },
  reset() {
    save(DEFAULT_PREFERENCES)
    applyTextSize(DEFAULT_PREFERENCES.textSize)
    set({ ...DEFAULT_PREFERENCES })
  },
}))

function pick(state: Preferences): Preferences {
  return {
    sendShortcut: state.sendShortcut,
    textSize: state.textSize,
    showThinking: state.showThinking,
    visualEffects: state.visualEffects,
  }
}

/** Re-read storage (another tab, or boot) and apply the text size. */
export function initPreferences(): void {
  const prefs = load()
  usePreferences.setState(prefs)
  applyTextSize(prefs.textSize)
}
