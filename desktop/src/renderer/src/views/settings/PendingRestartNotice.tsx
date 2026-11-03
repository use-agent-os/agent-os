import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useGateway } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import { Notice } from './parts'

/**
 * The gateway runs an older configuration than config.toml. A managed
 * gateway is the app's to restart, so the notice carries the button; an
 * external one runs on its own host, where the restart has to happen, so
 * the notice only says so.
 */
export function PendingRestartNotice() {
  const restartGateway = useGateway((s) => s.restart)
  const external = useSettings((s) => s.settings.gateway.mode === 'external')
  if (external) return <Notice>{t('settings.pendingRestart.external')}</Notice>
  return (
    <Notice
      action={<Button onClick={() => void restartGateway()}>{t('settings.restartGateway')}</Button>}
    >
      {t('settings.pendingRestart')}
    </Notice>
  )
}
