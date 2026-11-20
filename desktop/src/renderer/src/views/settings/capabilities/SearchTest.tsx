import { useMutation } from '@tanstack/react-query'
import { AlertTriangle, Check, Copy, LoaderCircle } from 'lucide-react'
import { useId, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { Button } from '~/components/ui/button'
import { t, type MessageKey } from '~/i18n'
import { Row } from '../parts'
import { searchFailure, type SearchFailure } from './logic'

/** What the test asks: plain, harmless, and sure to have answers. */
export const SEARCH_TEST_QUERY = 'AgentOS'

interface SearchQueryResult {
  ok?: boolean
  results?: unknown[]
  error?: { kind?: string; message?: string } | string | null
  errorMessage?: string
}

const FAILURE: Record<SearchFailure, MessageKey> = {
  noKey: 'settings.capabilities.test.fail.noKey',
  rejected: 'settings.capabilities.test.fail.rejected',
  refused: 'settings.capabilities.test.fail.refused',
  rateLimit: 'settings.capabilities.test.fail.rateLimit',
  timeout: 'settings.capabilities.test.fail.timeout',
  network: 'settings.capabilities.test.fail.network',
  server: 'settings.capabilities.test.fail.server',
  other: 'settings.capabilities.test.fail.other',
}

function fill(key: MessageKey, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce(
    (text, [name, value]) => text.replace(`{${name}}`, String(value)),
    t(key),
  )
}

/** The failure as the gateway gave it: its kind and the provider's raw words. */
function failureOf(res: SearchQueryResult): { kind: string; message: string } {
  if (typeof res.error === 'string') return { kind: '', message: res.error }
  return {
    kind: res.error?.kind ?? '',
    message: res.error?.message || res.errorMessage || '',
  }
}

/**
 * Web search's "does it work": one real search through the gateway with the
 * saved settings (`search.query`), the same path the agent's tool takes. Shown
 * while the card is saved: a pasted key is tested once it is saved. A failure
 * reads as one sentence about what it most likely means; the provider's own
 * words wait behind "Show details" for whoever needs them.
 */
export function SearchTest({
  providerId,
  label,
  disabled,
}: {
  providerId: string
  label: string
  disabled: boolean
}) {
  const rpc = useRpc()
  const [details, setDetails] = useState(false)
  const rawId = useId()
  const probe = useMutation({
    mutationFn: async () => {
      const started = performance.now()
      const res = await rpc.call<SearchQueryResult>('search.query', {
        query: SEARCH_TEST_QUERY,
        provider: providerId,
        limit: 3,
      })
      return { res, ms: Math.round(performance.now() - started) }
    },
    onMutate: () => setDetails(false),
  })
  const data = probe.data
  const verdict = probe.isPending
    ? 'checking'
    : probe.isError || (data && !data.res.ok)
      ? 'bad'
      : data
        ? 'ok'
        : 'idle'
  const count = data?.res.results?.length ?? 0
  const seconds = data ? (data.ms / 1000).toFixed(1) : '0'
  const failure =
    verdict !== 'bad'
      ? null
      : probe.isError
        ? {
            kind: '',
            message: probe.error instanceof Error ? probe.error.message : String(probe.error),
          }
        : data
          ? failureOf(data.res)
          : null

  return (
    <Row
      label={t('settings.capabilities.test.label')}
      help={
        verdict === 'idle' ? (
          fill('settings.capabilities.test.help', { label })
        ) : verdict === 'checking' ? (
          <span className="cap-test" data-verdict="checking" data-testid="search-test-result">
            <LoaderCircle className="stg-spin size-3" strokeWidth={2} aria-hidden />
            {t('settings.capabilities.test.running')}
          </span>
        ) : verdict === 'ok' ? (
          <span className="cap-test" data-verdict="ok" data-testid="search-test-result">
            <Check className="size-3" strokeWidth={2.5} aria-hidden />
            {count > 0
              ? fill(
                  count === 1
                    ? 'settings.capabilities.test.okOne'
                    : 'settings.capabilities.test.ok',
                  { n: count, label, s: seconds },
                )
              : fill('settings.capabilities.test.empty', { label, s: seconds })}
          </span>
        ) : (
          <span className="cap-test-fail" data-testid="search-test-result">
            <span className="cap-test" data-verdict="bad">
              <AlertTriangle className="size-3" strokeWidth={2} aria-hidden />
              <span>{fill(FAILURE[searchFailure(failure ?? {})], { label })}</span>
            </span>
            {failure?.message ? (
              <>
                <span className="cap-test-fail__actions">
                  <button
                    type="button"
                    className="prov-keylink app-no-drag"
                    aria-expanded={details}
                    aria-controls={rawId}
                    onClick={() => setDetails((v) => !v)}
                  >
                    {details
                      ? t('settings.capabilities.test.hideDetails')
                      : t('settings.capabilities.test.showDetails')}
                  </button>
                  {details ? (
                    <button
                      type="button"
                      className="prov-keylink app-no-drag"
                      onClick={() => {
                        void navigator.clipboard?.writeText(failure.message)
                        toast.success(t('settings.copied'), { id: 'stg-copy' })
                      }}
                    >
                      <Copy className="size-3" strokeWidth={2} aria-hidden />
                      {t('settings.capabilities.test.copy')}
                    </button>
                  ) : null}
                </span>
                {details ? (
                  <code id={rawId} className="cap-test-fail__raw">
                    {failure.message}
                  </code>
                ) : null}
              </>
            ) : null}
          </span>
        )
      }
      align="start"
    >
      <Button disabled={disabled || probe.isPending} onClick={() => probe.mutate()}>
        {probe.isPending
          ? t('settings.capabilities.test.running')
          : t('settings.capabilities.test.button')}
      </Button>
    </Row>
  )
}
