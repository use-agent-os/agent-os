/**
 * Theme contract shared by main (nativeTheme + persistence), preload (bridge
 * typing) and renderer (store + CSS application).
 *
 * Two independent axes:
 *  - `ThemePreference` — what the user asked for. `system` follows macOS.
 *  - `ResolvedTheme`   — what is actually painted right now (never `system`).
 *  - `PaletteId`       — which colour family fills the tokens for that mode.
 */
export type ThemePreference = 'system' | 'light' | 'dark'
export type ResolvedTheme = 'light' | 'dark'
export type PaletteId =
  | 'tactical'
  | 'graphite'
  | 'everforest'
  | 'solarized'
  | 'nord'
  | 'midnight'
  | 'slate'
  | 'ember'
  | 'mono'
  | 'cyberpunk'

export const THEME_PREFERENCES: readonly ThemePreference[] = ['system', 'light', 'dark']
export const PALETTE_IDS: readonly PaletteId[] = [
  'tactical',
  'graphite',
  'everforest',
  'solarized',
  'nord',
  'midnight',
  'slate',
  'ember',
  'mono',
  'cyberpunk',
]

export const DEFAULT_THEME_PREFERENCE: ThemePreference = 'system'
export const DEFAULT_PALETTE: PaletteId = 'tactical'

export interface ThemeSettings {
  preference: ThemePreference
  palette: PaletteId
}

export const DEFAULT_THEME_SETTINGS: ThemeSettings = {
  preference: DEFAULT_THEME_PREFERENCE,
  palette: DEFAULT_PALETTE,
}

export function isThemePreference(value: unknown): value is ThemePreference {
  return typeof value === 'string' && (THEME_PREFERENCES as readonly string[]).includes(value)
}

export function isPaletteId(value: unknown): value is PaletteId {
  return typeof value === 'string' && (PALETTE_IDS as readonly string[]).includes(value)
}

/** Coerce anything read from disk into a valid ThemeSettings, dropping junk. */
export function normalizeThemeSettings(raw: unknown): ThemeSettings {
  const obj = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  return {
    preference: isThemePreference(obj.preference) ? obj.preference : DEFAULT_THEME_PREFERENCE,
    palette: isPaletteId(obj.palette) ? obj.palette : DEFAULT_PALETTE,
  }
}

/** Resolve a preference against the OS answer. Pure, so both processes agree. */
export function resolveTheme(preference: ThemePreference, systemDark: boolean): ResolvedTheme {
  if (preference === 'system') return systemDark ? 'dark' : 'light'
  return preference
}
