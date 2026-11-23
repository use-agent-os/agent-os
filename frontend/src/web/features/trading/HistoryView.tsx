import { ArrowRight, History } from 'lucide-react'
import { useId, useState } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webTrading'
import {
  chainName,
  errorText,
  formatAmount,
  groupEntriesByDay,
  shortHash,
} from '@/views/trading/logic'
import type { Entry, Portfolio } from '@/views/trading/types'
import { Card, CardHeader } from '@/web/components/ui/card'
import { EmptyState, ErrorState, Skeleton } from '@/web/components/ui/states'
import { useHistory } from './data'
import {
  Amount,
  ChainChip,
  ExplorerLink,
  formatWhen,
  initiatorLabel,
  kindLabel,
  Money,
} from './parts'
import { clampSym, clampText, txUrl } from './safety'

// History: the ledger, newest day first, under the profit-and-loss figures
// the portfolio already carries. A network filter narrows the read itself.

const DAY = new Intl.DateTimeFormat(undefined, { dateStyle: 'full' })

function Leg({ amount, symbol }: { amount: string | null; symbol: string | undefined }) {
  if (!symbol) return null
  return (
    <>
      <Amount text={formatAmount(amount)} />
      <span className="truncate">{clampSym(symbol)}</span>
    </>
  )
}

function EntryRow({ e }: { e: Entry }) {
  const href = txUrl(e.chainId, e.txHash)
  return (
    <li className="trd-row trd-order" data-testid="trd-entry">
      <div className="trd-order__main">
        <div className="trd-order__title">
          <span className="font-medium">{kindLabel(e.kind)}</span>
          <ChainChip chainId={e.chainId} />
          <span className="trd-note">{initiatorLabel(e.initiator)}</span>
        </div>
        <div className="trd-order__legs">
          <Leg amount={e.amountIn} symbol={e.tokenIn?.symbol} />
          {e.tokenIn && e.tokenOut ? (
            <ArrowRight aria-hidden className="size-3.5 shrink-0 text-text-muted" />
          ) : null}
          <Leg amount={e.amountOut} symbol={e.tokenOut?.symbol} />
        </div>
        {e.note ? <p className="trd-order__note">{clampText(e.note, 120)}</p> : null}
      </div>
      <div className="trd-order__side">
        <Money value={e.valueUsd} />
        {e.gasUsd ? (
          <span className="trd-note">
            {t('webTrading.kindGas')} <Money value={e.gasUsd} />
          </span>
        ) : null}
        <span className="trd-note">{formatWhen(e.ts)}</span>
        {href ? (
          <ExplorerLink href={href} label={t('webTrading.openTx')}>
            <span className="trd-mono">{shortHash(e.txHash)}</span>
          </ExplorerLink>
        ) : null}
      </div>
    </li>
  )
}

export function HistoryView({
  wallet,
  chains,
  portfolio,
}: {
  wallet: string | undefined
  chains: readonly number[]
  portfolio: Portfolio | undefined
}) {
  const [chainId, setChainId] = useState<number | undefined>(undefined)
  const query = useHistory(wallet, chainId, true)
  const pickerId = useId()
  const days = groupEntriesByDay(query.entries)
  const totals = portfolio?.totals

  let body
  if (query.isError && !query.data)
    body = (
      <ErrorState
        title={t('webTrading.errorTitle')}
        message={errorText(query.error)}
        onRetry={() => void query.refetch()}
      />
    )
  else if (query.isPending) body = <Skeleton className="h-48 rounded-card" />
  else if (days.length === 0)
    body = (
      <EmptyState
        size="sm"
        icon={<History />}
        title={t('webTrading.historyEmpty')}
        description={t('webTrading.historyEmptyBody')}
      />
    )
  else
    body = days.map(({ day, entries }) => {
      const label = DAY.format(entries[0]!.ts)
      return (
        <Card asChild padding="none" key={day}>
          <section aria-label={label}>
            <CardHeader title={label} as="h3" className="px-4 pt-4 pb-2" />
            <ul className="trd-rows">
              {entries.map((e) => (
                <EntryRow key={e.id} e={e} />
              ))}
            </ul>
          </section>
        </Card>
      )
    })

  return (
    <div className="flex flex-col gap-4" data-testid="trd-history">
      {totals ? (
        <Card asChild>
          <section aria-label={t('webTrading.pnlSummary')} className="flex flex-col gap-3">
            <CardHeader title={t('webTrading.pnlSummary')} />
            <dl className="trd-pnl">
              <div>
                <dt>{t('webTrading.realized')}</dt>
                <dd>
                  <Money value={totals.realizedUsd} signed toned />
                </dd>
              </div>
              <div>
                <dt>{t('webTrading.unrealized')}</dt>
                <dd>
                  <Money value={totals.unrealizedUsd} signed toned />
                </dd>
              </div>
              <div>
                <dt>{t('webTrading.gasSpent')}</dt>
                <dd>
                  <Money value={totals.gasUsd} />
                </dd>
              </div>
            </dl>
          </section>
        </Card>
      ) : null}
      {chains.length > 1 ? (
        <label className="trd-picker self-start" htmlFor={pickerId}>
          <span className="sr-only">{t('webTrading.historyNetwork')}</span>
          <select
            id={pickerId}
            className="trd-select"
            value={chainId ?? ''}
            onChange={(e) => setChainId(e.target.value ? Number(e.target.value) : undefined)}
          >
            <option value="">{t('webTrading.allNetworks')}</option>
            {chains.map((c) => (
              <option key={c} value={c}>
                {chainName(c)}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {body}
    </div>
  )
}
