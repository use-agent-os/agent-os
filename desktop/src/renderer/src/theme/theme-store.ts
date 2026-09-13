import { create } from 'zustand'
import {
  DEFAULT_PALETTE,
  DEFAULT_THEME_PREFERENCE,
  resolveTheme,
  type PaletteId,
  type ResolvedTheme,
  type ThemePreference,
} from '@shared/theme'
import { desktopApi } from '~/lib/desktop-api'
import { applyTheme } from './apply'

export interface ThemeState {
  /** What the user chose. */
  preference: ThemePreference
  palette: PaletteId
  /** What the OS currently reports. Only matters when preference === 'system'. */
  systemDark: boolean
  /** What is painted right now. Derived; never set directly. */
  resolved: ResolvedTheme
  /** False until persisted settings have been loaded once. */
  ready: boolean

  setPreference(preference: ThemePreference): Promise<void>
  setPalette(palette: PaletteId): Promise<void>
  /** system -> light -> dark -> system. Handy for a single toolbar button. */
  cycle(): Promise<void>
}

const CYCLE: readonly ThemePreference[] = ['system', 'light', 'dark']

function paint(state: Pick<ThemeState, 'preference' | 'palette' | 'systemDark'>): ResolvedTheme {
  const resolved = resolveTheme(state.preference, state.systemDark)
  applyTheme(resolved, state.palette)
  return resolved
}

export const useTheme = create<ThemeState>((set, get) => ({
  preference: DEFAULT_THEME_PREFERENCE,
  palette: DEFAULT_PALETTE,
  systemDark: false,
  resolved: 'light',
  ready: false,

  async setPreference(preference) {
    // Paint optimistically, then let main persist + mirror onto nativeTheme.
    const next = { ...get(), preference }
    set({ preference, resolved: paint(next) })
    await desktopApi().theme.set({ preference })
  },

  async setPalette(palette) {
    const next = { ...get(), palette }
    set({ palette, resolved: paint(next) })
    await desktopApi().theme.set({ palette })
  },

  async cycle() {
    const idx = CYCLE.indexOf(get().preference)
    const next = CYCLE[(idx + 1) % CYCLE.length] ?? 'system'
    await get().setPreference(next)
  },
}))

/**
 * Re-paint from settings that changed outside this store (a reset). Main has
 * already mirrored the new preference onto nativeTheme by the time the
 * settings call resolves, so ask it what the OS resolves to now rather than
 * trusting a matchMedia value that may still reflect the old forced source.
 */
export async function syncThemeFromSettings(theme: {
  preference: ThemePreference
  palette: PaletteId
}): Promise<void> {
  const resolvedFromMain = await desktopApi().theme.resolved()
  const systemDark = theme.preference === 'system' ? resolvedFromMain === 'dark' : readSystemDark()
  const next = { preference: theme.preference, palette: theme.palette, systemDark }
  useTheme.setState({ ...next, resolved: paint(next) })
}

function readSystemDark(): boolean {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  } catch {
    return false
  }
}

/**
 * Load persisted theme settings, paint once, and wire OS-change listeners.
 * Call exactly once from main.tsx before rendering. Returns a disposer.
 */
export async function initTheme(): Promise<() => void> {
  const api = desktopApi()
  const [settings, resolvedFromMain] = await Promise.all([api.settings.get(), api.theme.resolved()])

  const systemDark =
    settings.theme.preference === 'system' ? resolvedFromMain === 'dark' : readSystemDark()
  const base = {
    preference: settings.theme.preference,
    palette: settings.theme.palette,
    systemDark,
  }
  useTheme.setState({ ...base, resolved: paint(base), ready: true })

  const onSystem = (dark: boolean) => {
    const cur = useTheme.getState()
    if (cur.systemDark === dark) return
    const next = { ...cur, systemDark: dark }
    useTheme.setState({ systemDark: dark, resolved: paint(next) })
  }

  // Electron mirrors nativeTheme into prefers-color-scheme, so matchMedia is
  // the primary signal in both the shell and a plain browser. The IPC push
  // is a belt-and-braces path for the shell.
  let mql: MediaQueryList | null = null
  const onMql = (e: MediaQueryListEvent) => onSystem(e.matches)
  try {
    mql = window.matchMedia('(prefers-color-scheme: dark)')
    mql.addEventListener('change', onMql)
  } catch {
    mql = null
  }
  const offIpc = api.theme.onChanged((resolved) => {
    if (useTheme.getState().preference === 'system') onSystem(resolved === 'dark')
  })

  return () => {
    mql?.removeEventListener('change', onMql)
    offIpc()
  }
}
