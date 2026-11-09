import { CandlestickChart, Lock, RefreshCw } from 'lucide-react'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { useEffect } from 'react'
import { useRpc } from '@/app/providers'
import type { Artifact } from '@/views/chat/transcript/artifacts'
import { normalizeDcaPayload } from '@/views/chat/transcript/dca'
import { normalizeLpPayload } from '@/views/chat/transcript/lp'
import { normalizeMarketsPayload } from '@/views/chat/transcript/markets'
import { normalizeTriggerPayload } from '@/views/chat/transcript/trigger'
import { Button } from '@/web/components/ui/button'
import { ErrorState } from '@/web/components/ui/states'
import { useArtifactJson } from './payload'

const NORMALIZERS: Record<string, (raw: unknown) => unknown> = {
  dca: normalizeDcaPayload,
  lp: normalizeLpPayload,
  markets: normalizeMarketsPayload,
  trigger: normalizeTriggerPayload,
}

/** Flatten a normalized payload's headline facts into label/value rows. */
export function summarizeTrading(payload: unknown): { label: string; value: string }[] {
  if (!payload || typeof payload !== 'object') return []
  const rows: { label: string; value: string }[] = []
  for (const [key, value] of Object.entries(payload as Record<string, unknown>)) {
    if (['version', 'warnings', 'request', 'fetchedAt'].includes(key)) continue
    if (value === null || value === undefined) continue
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      rows.push({ label: key, value: String(value) })
    } else if (Array.isArray(value)) {
      rows.push({ label: key, value: t('webChat.cardRows', { count: value.length }) })
    }
    if (rows.length >= 12) break
  }
  return rows
}

/**
 * DCA, LP, markets and trigger/bracket cards. Read-only in the web console:
 * they refresh, but offer no write action (chat spec "Trading card stays
 * read-only").
 */
export function TradingCard({ category, artifact }: { category: string; artifact: Artifact }) {
  const rpc = useRpc()
  const query = useArtifactJson(artifact)
  const { refetch } = query

  // Live trading changes refresh the card.
  useEffect(
    () =>
      rpc.on('*', (...args: unknown[]) => {
        const name = String(args[0] ?? '')
        if (name.startsWith('trading.') && name.endsWith('.changed')) void refetch()
      }),
    [rpc, refetch],
  )

  if (query.isPending) {
    return (
      <div
        className="h-24 rounded-lg border border-border bg-surface-1"
        role="status"
        aria-label={t('webChat.cardLoading')}
      />
    )
  }
  if (query.isError) {
    return (
      <ErrorState
        message={t('webChat.cardFailed', {
          message: String((query.error as Error)?.message ?? query.error),
        })}
        onRetry={() => void refetch()}
      />
    )
  }
  const normalize = NORMALIZERS[category]
  const payload = normalize ? normalize(query.data) : null
  if (!payload) return null
  const kind = String((payload as { kind?: unknown }).kind ?? category)
  return (
    <section
      data-trading-card={category}
      className="rounded-lg border border-border bg-surface-1 p-3"
    >
      <header className="mb-2 flex items-center gap-2 text-ui font-medium">
        <CandlestickChart className="size-4 text-text-muted" aria-hidden />
        <h3 className="min-w-0 flex-1 truncate">
          {category.toUpperCase()} · {kind}
        </h3>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => void refetch()}
          disabled={query.isFetching}
        >
          <RefreshCw />
          {t('webChat.cardRefresh')}
        </Button>
      </header>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-small">
        {summarizeTrading(payload).map((row) => (
          <div key={row.label} className="contents">
            <dt className="text-text-muted">{row.label}</dt>
            <dd className="break-all">{row.value}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 flex items-center gap-1 text-small text-text-muted">
        <Lock className="size-3.5" aria-hidden />
        {t('webChat.cardReadOnly')}
      </p>
    </section>
  )
}
