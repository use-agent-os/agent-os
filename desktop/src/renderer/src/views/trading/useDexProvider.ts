import { toast } from 'sonner'
import { t } from '~/i18n'
import { useSetProvider } from '~/stores/trading'
import { errorText } from './logic'
import { DEFAULT_PROVIDER, providerLabel } from './types'

/**
 * Put the gateway's swaps back on the AgentOS DEX.
 *
 * The desk swaps through the AgentOS DEX and offers no other route. A gateway
 * whose config still names another provider (set from the CLI, or by an older
 * build of this app) is shown a notice, and this is its button: one explicit
 * click, never a silent rewrite of someone's config.
 */
export function useDexProvider(): { useDex: () => void; switching: boolean } {
  const setProvider = useSetProvider()
  return {
    switching: setProvider.isPending,
    useDex: () =>
      setProvider.mutate(DEFAULT_PROVIDER, {
        onSuccess: (res) =>
          toast.success(
            `${t('trading.provider.saved')}: ${providerLabel(res?.provider ?? DEFAULT_PROVIDER)}`,
            { id: 'trd-provider' },
          ),
        onError: (err) => toast.error(errorText(err), { id: 'trd-provider-err' }),
      }),
  }
}
