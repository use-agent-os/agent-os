import { create } from 'zustand'

export type ThemeMode = 'dark' | 'light'
const STORAGE_KEY = 'agentos-theme'

function systemDefault(): ThemeMode {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

function apply(mode: ThemeMode): void {
  document.documentElement.setAttribute('data-theme', mode)
}

export const useTheme = create<{ mode: ThemeMode; set(m: ThemeMode): void; toggle(): void }>(
  (set, get) => ({
    mode: 'light',
    set(mode) {
      if (mode !== 'dark' && mode !== 'light') return
      localStorage.setItem(STORAGE_KEY, mode)
      apply(mode)
      set({ mode })
    },
    toggle() {
      get().set(get().mode === 'dark' ? 'light' : 'dark')
    },
  }),
)

export function initTheme(): void {
  const stored = localStorage.getItem(STORAGE_KEY)
  const mode: ThemeMode = stored === 'dark' || stored === 'light' ? stored : systemDefault()
  apply(mode)
  useTheme.setState({ mode })
}
