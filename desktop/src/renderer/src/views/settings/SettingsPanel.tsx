import './settings.css'
import { useEffect, useId } from 'react'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useSettings } from '~/stores/settings'
import { useUi } from '~/stores/ui'
import { AboutPane } from './panes/AboutPane'
import { AdvancedPane } from './panes/AdvancedPane'
import { AppearancePane } from './panes/AppearancePane'
import { BehaviourPane } from './panes/BehaviourPane'
import { GatewayPane } from './panes/GatewayPane'
import { NotificationsPane } from './panes/NotificationsPane'
import { ProvidersPane } from './panes/ProvidersPane'
import { RouterPane } from './panes/RouterPane'
import { ShortcutsPane } from './panes/ShortcutsPane'
import { SkillsPane } from './panes/SkillsPane'
import { SETTINGS_GROUPS, type SettingsSection } from './sections'

const PANE: Record<SettingsSection, () => React.JSX.Element> = {
  providers: ProvidersPane,
  router: RouterPane,
  skills: SkillsPane,
  gateway: GatewayPane,
  appearance: AppearancePane,
  notifications: NotificationsPane,
  behaviour: BehaviourPane,
  shortcuts: ShortcutsPane,
  advanced: AdvancedPane,
  about: AboutPane,
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

  // Re-read from disk on open: main may have mirrored OS state meanwhile.
  useEffect(() => void load(), [load])

  const Pane = PANE[section]

  function onRailKey(e: React.KeyboardEvent) {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    const all = SETTINGS_GROUPS.flatMap((g) => g.sections)
    const i = all.indexOf(section)
    if (i < 0) return
    e.preventDefault()
    const next = all[(i + (e.key === 'ArrowDown' ? 1 : all.length - 1)) % all.length]
    if (next) {
      setSection(next)
      document.getElementById(`stg-rail-${next}`)?.focus()
    }
  }

  return (
    <div className="stg">
      <div className="stg-bar">
        <span id={titleId} className="stg-bar__title">
          {t('settings.title')}
        </span>
        <Button variant="primary" onClick={onClose}>
          {t('settings.done')}
        </Button>
      </div>
      <div className="stg-body">
        <nav className="stg-rail" aria-label={t('settings.sections')} onKeyDown={onRailKey}>
          {SETTINGS_GROUPS.map((group) => (
            <div key={group.id} className="stg-rail__group">
              <div className="stg-rail__label">{t(`settings.group.${group.id}`)}</div>
              {group.sections.map((id) => (
                <button
                  key={id}
                  id={`stg-rail-${id}`}
                  type="button"
                  className="stg-rail__item app-no-drag"
                  aria-current={id === section ? 'page' : undefined}
                  onClick={() => setSection(id)}
                >
                  {t(`settings.section.${id}`)}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="stg-main">
          <div className="stg-section" key={section}>
            <Pane />
          </div>
        </div>
      </div>
    </div>
  )
}
