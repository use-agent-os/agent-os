import { create } from 'zustand'

/** The resolved theme actually painted. Desktop reads this through `useTheme`. */
export type ThemeMode = 'dark' | 'light'
/** What the user picked. `system` follows the OS color scheme live. */
export type ThemePreference = 'system' | ThemeMode
const STORAGE_KEY = 'agentos-theme'
const DARK_QUERY = '(prefers-color-scheme: dark)'

function systemDefault(): ThemeMode {
  try {
    return window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

function apply(mode: ThemeMode): void {
  document.documentElement.setAttribute('data-theme', mode)
}

function isPreference(value: unknown): value is ThemePreference {
  return value === 'system' || value === 'dark' || value === 'light'
}

function readPreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    // A stored `dark`/`light` from the previous console carries over as-is;
    // anything else (unset, junk) means System.
    return isPreference(stored) ? stored : 'system'
  } catch {
    return 'system'
  }
}

function writePreference(preference: ThemePreference): void {
  try {
    localStorage.setItem(STORAGE_KEY, preference)
  } catch {
    /* storage unavailable: the choice still applies for this page */
  }
}

function resolve(preference: ThemePreference): ThemeMode {
  return preference === 'system' ? systemDefault() : preference
}

interface ThemeState {
  mode: ThemeMode
  preference: ThemePreference
  /** Pick an explicit mode (legacy API; equivalent to setPreference(mode)). */
  set(m: ThemeMode): void
  setPreference(p: ThemePreference): void
  toggle(): void
}

export const useTheme = create<ThemeState>((set, get) => ({
  mode: 'light',
  preference: 'system',
  set(mode) {
    if (mode !== 'dark' && mode !== 'light') return
    get().setPreference(mode)
  },
  setPreference(preference) {
    if (!isPreference(preference)) return
    writePreference(preference)
    const mode = resolve(preference)
    apply(mode)
    set({ preference, mode })
  },
  toggle() {
    get().set(get().mode === 'dark' ? 'light' : 'dark')
  },
}))

let detachSystemListener: (() => void) | null = null

/** Follow OS scheme changes while the preference is System. Idempotent. */
function watchSystemScheme(): void {
  if (detachSystemListener) return
  let query: MediaQueryList
  try {
    query = window.matchMedia(DARK_QUERY)
  } catch {
    return
  }
  if (!query || typeof query.addEventListener !== 'function') return
  const onChange = (event: MediaQueryListEvent) => {
    if (useTheme.getState().preference !== 'system') return
    const mode: ThemeMode = event.matches ? 'dark' : 'light'
    apply(mode)
    useTheme.setState({ mode })
  }
  query.addEventListener('change', onChange)
  detachSystemListener = () => {
    query.removeEventListener('change', onChange)
    detachSystemListener = null
  }
}

export function initTheme(): void {
  const preference = readPreference()
  const mode = resolve(preference)
  apply(mode)
  useTheme.setState({ preference, mode })
  watchSystemScheme()
}

/** Test seam: drop the OS listener so a fresh initTheme() re-attaches. */
export function resetThemeForTests(): void {
  detachSystemListener?.()
}
