import '../overview.css'
import { t } from '~/i18n'
import { Head } from '../parts'

export function OverviewPane() {
  return (
    <Head title={t('settings.section.overview')} blurb={t('settings.section.overview.blurb')} />
  )
}
