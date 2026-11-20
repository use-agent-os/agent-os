import './settings.css'
import {
  Bell,
  Blocks,
  Brain,
  CandlestickChart,
  Cpu,
  Fingerprint,
  Info,
  Keyboard,
  KeyRound,
  ListChecks,
  MessagesSquare,
  Palette,
  Puzzle,
  Route,
  Search,
  Server,
  SlidersHorizontal,
  Wrench,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useSettings } from '~/stores/settings'
import { useUi } from '~/stores/ui'
import { useSectionAttention } from './attention'
import { useMemoryTab } from './memory/tab-store'
import { registerJump } from './overview/jumps'
import { AboutPane } from './panes/AboutPane'
import { AdvancedPane } from './panes/AdvancedPane'
import { AppearancePane } from './panes/AppearancePane'
import { BehaviourPane } from './panes/BehaviourPane'
import { CapabilitiesPane } from './panes/CapabilitiesPane'
import { ChannelsPane } from './panes/ChannelsPane'
import { EnvironmentPane } from './panes/EnvironmentPane'
import { GatewayPane } from './panes/GatewayPane'
import { MemoryPane } from './panes/MemoryPane'
import { NotificationsPane } from './panes/NotificationsPane'
import { OverviewPane } from './panes/OverviewPane'
import { ProvidersPane } from './panes/ProvidersPane'
import { RouterPane } from './panes/RouterPane'
import { SecurityPane } from './panes/SecurityPane'
import { ShortcutsPane } from './panes/ShortcutsPane'
import { SkillsPane } from './panes/SkillsPane'
import { TradingPane } from './panes/TradingPane'
import { matchSections, SETTINGS_GROUPS, type SettingsSection } from './sections'

const PANE: Record<SettingsSection, () => React.JSX.Element> = {
  overview: OverviewPane,
  providers: ProvidersPane,
  router: RouterPane,
  memory: MemoryPane,
  capabilities: CapabilitiesPane,
  skills: SkillsPane,
  channels: ChannelsPane,
  environment: EnvironmentPane,
  trading: TradingPane,
  gateway: GatewayPane,
  appearance: AppearancePane,
  notifications: NotificationsPane,
  security: SecurityPane,
  behaviour: BehaviourPane,
  shortcuts: ShortcutsPane,
  advanced: AdvancedPane,
  about: AboutPane,
}

// The Overview's Memory row is about setup, so it lands on that tab.
registerJump('memory', () => useMemoryTab.getState().setTab('setup'))

const ICON: Record<SettingsSection, LucideIcon> = {
  overview: ListChecks,
  providers: Cpu,
  router: Route,
  memory: Brain,
  capabilities: Blocks,
  skills: Puzzle,
  channels: MessagesSquare,
  environment: KeyRound,
  trading: CandlestickChart,
  gateway: Server,
  appearance: Palette,
  notifications: Bell,
  security: Fingerprint,
  behaviour: SlidersHorizontal,
  shortcuts: Keyboard,
  advanced: Wrench,
  about: Info,
}

/**
 * Settings as a sheet over the window, the way Scheduled jobs sits over the
 * document: a rail of sections on the left, the chosen one on the right,
 * Escape or Done to leave. Opened from the toolbar gear, ⌘, or the app
 * menu; it reads the `settingsOpen` flag from the UI store.
 */
export function SettingsPanel() {
  const open = useUi((s) => s.settingsOpen)
  const close = useUi((s) => s.closeSettings)
  const titleId = useId()
  if (!open) return null
  return (
    <ModalShell
      role="dialog"
      labelledBy={titleId}
      onClose={close}
      overlayClassName="stg__overlay"
      className="stg-sheet"
    >
      <SettingsBody titleId={titleId} onClose={close} />
    </ModalShell>
  )
}

function SettingsBody({ titleId, onClose }: { titleId: string; onClose: () => void }) {
  const section = useUi((s) => s.settingsSection)
  const setSection = useUi((s) => s.setSettingsSection)
  const load = useSettings((s) => s.load)
  const attention = useSectionAttention()
  const [query, setQuery] = useState('')
  const mainRef = useRef<HTMLDivElement>(null)

  // Re-read from disk on open: main may have mirrored OS state meanwhile.
  useEffect(() => void load(), [load])

  // When the focused control is removed or disabled (a row deleted, a button
  // greyed out while saving) focus drops to <body>, outside every dialog, and
  // Escape and Tab stop reaching the sheet. Hand such a key to the topmost
  // dialog so it closes or traps as it would have.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' && e.key !== 'Tab') return
      if (document.activeElement !== document.body) return
      const top = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).at(-1)
      if (!top) return
      top.focus()
      if (e.key === 'Escape') {
        e.preventDefault()
        top.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  // A new section starts at its top, not where the last one was scrolled to.
  useEffect(() => {
    mainRef.current?.scrollTo({ top: 0 })
  }, [section])

  const matches = useMemo(
    () =>
      matchSections(query, (id) => ({
        name: t(`settings.section.${id}`),
        keywords: t(`settings.section.${id}.keywords`),
      })),
    [query],
  )
  // While searching, one flat list, best match first: the top row is the one
  // Return opens.
  const groups: {
    id: (typeof SETTINGS_GROUPS)[number]['id'] | 'results'
    sections: SettingsSection[]
  }[] = query.trim()
    ? matches.length > 0
      ? [{ id: 'results', sections: matches }]
      : []
    : SETTINGS_GROUPS.map((g) => ({ id: g.id, sections: g.sections }))

  const Pane = PANE[section]

  function go(id: SettingsSection) {
    setSection(id)
    setQuery('')
  }

  function onRailKey(e: React.KeyboardEvent) {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    if (matches.length === 0) return
    const fromSearch = e.target instanceof HTMLInputElement
    const i = matches.indexOf(section)
    e.preventDefault()
    const step = e.key === 'ArrowDown' ? 1 : matches.length - 1
    const next = fromSearch || i < 0 ? matches[0] : matches[(i + step) % matches.length]
    if (next) {
      setSection(next)
      if (!fromSearch) document.getElementById(`stg-rail-${next}`)?.focus()
    }
  }

  function onSearchKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter' && matches[0]) {
      e.preventDefault()
      go(matches[0])
    } else if (e.key === 'Escape' && query) {
      // First Escape clears the search; the next one closes the sheet.
      e.preventDefault()
      e.stopPropagation()
      setQuery('')
    }
  }

  return (
    <div className="stg">
      <div className="stg-body">
        <nav className="stg-rail" aria-label={t('settings.sections')} onKeyDown={onRailKey}>
          <span id={titleId} className="stg-rail__title">
            {t('settings.title')}
          </span>
          <label className="stg-rail__search">
            <Search className="size-3.5" strokeWidth={2} aria-hidden />
            <input
              type="search"
              className="app-no-drag"
              value={query}
              placeholder={t('settings.search')}
              aria-label={t('settings.search')}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onSearchKey}
            />
          </label>
          <div className="stg-rail__scroll">
            {groups.map((group) => (
              <div key={group.id} className="stg-rail__group">
                {group.id === 'start' || group.id === 'results' ? null : (
                  <div className="stg-rail__label">{t(`settings.group.${group.id}`)}</div>
                )}
                {group.sections.map((id) => {
                  const Icon = ICON[id]
                  const tone = attention[id]
                  return (
                    <button
                      key={id}
                      id={`stg-rail-${id}`}
                      type="button"
                      className="stg-rail__item app-no-drag"
                      aria-current={id === section ? 'page' : undefined}
                      onClick={() => go(id)}
                    >
                      <Icon className="stg-rail__icon" strokeWidth={1.75} aria-hidden />
                      <span>{t(`settings.section.${id}`)}</span>
                      {tone ? (
                        <span
                          className="stg-rail__dot"
                          data-tone={tone}
                          role="img"
                          aria-label={t('settings.needsAttention')}
                        />
                      ) : null}
                    </button>
                  )
                })}
              </div>
            ))}
            {groups.length === 0 ? (
              <p className="stg-rail__empty">{t('settings.search.empty')}</p>
            ) : null}
          </div>
        </nav>
        <div className="stg-main" ref={mainRef}>
          <div className="stg-main__bar">
            <Button variant="primary" onClick={onClose}>
              {t('settings.done')}
            </Button>
          </div>
          <div className="stg-section" key={section} data-section={section}>
            <Pane />
          </div>
        </div>
      </div>
    </div>
  )
}
