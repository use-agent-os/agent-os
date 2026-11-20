import '../capabilities.css'
import { t } from '~/i18n'
import { Head } from '../parts'

export function CapabilitiesPane() {
  return (
    <Head
      title={t('settings.section.capabilities')}
      blurb={t('settings.section.capabilities.blurb')}
    />
  )
}
