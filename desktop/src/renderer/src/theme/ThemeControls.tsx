import { Check, Monitor, Moon, Search, Sun, type LucideIcon } from 'lucide-react'
import { useState } from 'react'
import { PALETTE_IDS, THEME_PREFERENCES, type PaletteId, type ThemePreference } from '@shared/theme'
import { cn } from '~/lib/utils'
import { t } from '~/i18n'
import { PALETTES } from './palettes'
import { useTheme } from './theme-store'

const MODE_ICON: Record<ThemePreference, LucideIcon> = { system: Monitor, light: Sun, dark: Moon }

/** Mode row (Auto / Light / Dark) for a host's row recipe. */
export function ThemeModeRow({ rowClass = 'mac-group-row' }: { rowClass?: string }) {
  const { preference, resolved, setPreference } = useTheme()
  return (
    <div className={rowClass}>
      <div>
        <div>{t('theme.mode')}</div>
        <div className="mac-help">{t(`theme.resolved.${resolved}`)}</div>
      </div>
      <div role="radiogroup" aria-label={t('theme.mode')} className="mac-segmented">
        {THEME_PREFERENCES.map((mode) => {
          const Icon = MODE_ICON[mode]
          return (
            <button
              key={mode}
              type="button"
              role="radio"
              aria-checked={mode === preference}
              className="mac-segment app-no-drag"
              onClick={() => void setPreference(mode)}
            >
              <Icon className="size-3.5" strokeWidth={1.75} aria-hidden />
              {t(`theme.mode.${mode}`)}
            </button>
          )
        })}
      </div>
    </div>
  )
}

/** Palette matches when the query is in its label or description. */
export function filterPalettes(query: string): PaletteId[] {
  const q = query.trim().toLowerCase()
  if (!q) return [...PALETTE_IDS]
  return PALETTE_IDS.filter((id) => {
    const def = PALETTES[id]
    return def.label.toLowerCase().includes(q) || def.description.toLowerCase().includes(q)
  })
}

/**
 * Gallery of palettes: a miniature window per palette in the mode that is
 * showing right now, with its name and one line about it. A search field
 * narrows the grid.
 */
export function PaletteGallery() {
  const { palette, resolved, setPalette } = useTheme()
  const [query, setQuery] = useState('')
  const ids = filterPalettes(query)

  return (
    <div className="stg-themes">
      <label className="mac-search app-no-drag stg-themes__search">
        <Search className="size-3.5 shrink-0" strokeWidth={1.75} aria-hidden />
        <input
          type="search"
          placeholder={t('theme.palette.search')}
          aria-label={t('theme.palette.search')}
          autoComplete="off"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>
      {ids.length === 0 ? (
        <div className="stg-themes__empty">{t('theme.palette.empty')}</div>
      ) : (
        <div role="radiogroup" aria-label={t('theme.palette')} className="stg-themes__grid">
          {ids.map((id) => {
            const def = PALETTES[id]
            const c = def[resolved]
            const active = id === palette
            return (
              <button
                key={id}
                type="button"
                role="radio"
                aria-checked={active}
                className={cn('stg-theme app-no-drag')}
                onClick={() => void setPalette(id)}
              >
                <span
                  className="stg-theme__preview"
                  style={{ background: c.background, borderColor: c.hairline }}
                  aria-hidden
                >
                  <span className="stg-theme__side" style={{ background: c.sidebar }}>
                    <span style={{ background: c.muted }} />
                    <span style={{ background: c['sidebar-accent'] }} />
                    <span style={{ background: c.muted }} />
                  </span>
                  <span className="stg-theme__main">
                    <span
                      className="stg-theme__line"
                      style={{ background: c.foreground, opacity: 0.75 }}
                    />
                    <span
                      className="stg-theme__line"
                      style={{ background: c.muted, width: '58%' }}
                    />
                    <span className="stg-theme__pill" style={{ background: c.primary }} />
                  </span>
                  {active ? (
                    <span
                      className="stg-theme__check"
                      style={{ background: c.primary, color: c['primary-foreground'] }}
                    >
                      <Check className="size-3" strokeWidth={3} />
                    </span>
                  ) : null}
                </span>
                <span className="stg-theme__name">{def.label}</span>
                <span className="stg-theme__desc">{def.description}</span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
