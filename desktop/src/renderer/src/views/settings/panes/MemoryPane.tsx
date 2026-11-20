import '../memory.css'
import { t } from '~/i18n'
import { Head } from '../parts'

export function MemoryPane() {
  return <Head title={t('settings.section.memory')} blurb={t('settings.section.memory.blurb')} />
}
