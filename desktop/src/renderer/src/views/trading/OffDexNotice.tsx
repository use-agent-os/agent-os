import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { Notice } from '~/views/settings/parts'
import { ProviderMark } from './ProviderMark'
import { DEFAULT_PROVIDER } from './types'
import { useDexProvider } from './useDexProvider'

/**
 * The gateway's config names a swap provider other than the AgentOS DEX.
 *
 * The desk offers no choice of route, so this is the only place that state
 * shows: it says why the ticket will not quote and puts it right in a click.
 */
export function OffDexNotice({ testId }: { testId: string }) {
  const dex = useDexProvider()
  return (
    <Notice
      tone="info"
      action={
        <Button disabled={dex.switching} onClick={dex.useDex} data-testid={testId}>
          <ProviderMark id={DEFAULT_PROVIDER} size={13} />
          {t('trading.provider.switch')}
        </Button>
      }
    >
      <b>{t('trading.offDex.title')}</b> {t('trading.offDex.body')}
    </Notice>
  )
}
