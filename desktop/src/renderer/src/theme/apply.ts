import type { PaletteId, ResolvedTheme } from '@shared/theme'
import { COLOR_TOKEN_KEYS, paletteTokens } from './palettes'

/**
 * Paint a resolved theme onto <html>. Pure DOM, no React, so it can run
 * before the first render (main.tsx) and inside tests.
 *
 *  - data-theme="dark|light"  drives CSS selectors and Tailwind's `dark:` variant
 *  - data-palette="<id>"      lets CSS special-case a palette if it must
 *  - --<token> custom props   are the actual colours, read by tokens.css
 *  - color-scheme             makes native form controls and scrollbars match
 */
export function applyTheme(mode: ResolvedTheme, palette: PaletteId, root?: HTMLElement): void {
  const el = root ?? document.documentElement
  const tokens = paletteTokens(palette, mode)
  el.setAttribute('data-theme', mode)
  el.setAttribute('data-palette', palette)
  el.style.colorScheme = mode
  for (const key of COLOR_TOKEN_KEYS) el.style.setProperty(`--${key}`, tokens[key])
}
