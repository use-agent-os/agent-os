import { UI_SCALES } from '@shared/settings'
import { Switch } from '~/components/ui/switch'
import { t } from '~/i18n'
import { useSettings } from '~/stores/settings'
import { PaletteGallery, ThemeModeRow } from '~/theme/ThemeControls'
import { Card, Head, Row, Segmented } from '../parts'
import { PetCard } from './PetCard'

export function AppearancePane() {
  const appearance = useSettings((s) => s.settings.appearance)
  const update = useSettings((s) => s.update)

  return (
    <>
      <Head
        title={t('settings.section.appearance')}
        blurb={t('settings.section.appearance.blurb')}
      />
      <Card title={t('theme.section')} blurb={t('theme.section.blurb')}>
        <ThemeModeRow rowClass="stg-row" />
      </Card>

      <Card title={t('theme.palette')} blurb={t('theme.palette.help')}>
        <div className="stg-card__body">
          <PaletteGallery />
        </div>
      </Card>

      <Card title={t('settings.appearance.window')}>
        <Row label={t('settings.appearance.uiScale')} help={t('settings.appearance.uiScale.help')}>
          <Segmented
            label={t('settings.appearance.uiScale')}
            value={String(appearance.uiScale)}
            options={UI_SCALES.map((scale) => ({ value: String(scale), label: `${scale}%` }))}
            onChange={(v) => {
              const uiScale = UI_SCALES.find((s) => String(s) === v)
              if (uiScale) void update({ appearance: { uiScale } })
            }}
          />
        </Row>
        <Row
          label={t('settings.appearance.reduceTransparency')}
          help={t('settings.appearance.reduceTransparency.help')}
        >
          <Switch
            checked={appearance.reduceTransparency}
            aria-label={t('settings.appearance.reduceTransparency')}
            onCheckedChange={(reduceTransparency) =>
              void update({ appearance: { reduceTransparency } })
            }
          />
        </Row>
      </Card>

      <PetCard />
    </>
  )
}
