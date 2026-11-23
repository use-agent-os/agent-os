import { Coins, Flame, TrendingUp, Wallet as WalletIcon } from 'lucide-react'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webTrading'
import { allocationSegments, chainName, shortAddress, walletLabel } from '@/views/trading/logic'
import type { Portfolio } from '@/views/trading/types'
import { Card, CardHeader } from '@/web/components/ui/card'
import { IconTile, STAT_LABEL } from '@/web/features/home/overview-ui'
import { ChainChip, ExplorerLink, Money } from './parts'
import { addressUrl, clampSym, clampText, useBalancePrivacy } from './safety'

// The overview: the totals across the shown wallets, how the value splits
// across tokens, and each wallet with its value and networks.

function Figure({
  label,
  icon,
  tone,
  children,
}: {
  label: string
  icon: React.ReactNode
  tone: 'accent' | 'ok' | 'info' | 'warn' | 'neutral'
  children: React.ReactNode
}) {
  return (
    <Card asChild className="trd-figure">
      <section aria-label={label}>
        <div className="flex items-center gap-2.5">
          <IconTile tone={tone}>{icon}</IconTile>
          <h2 className={STAT_LABEL}>{label}</h2>
        </div>
        <p className="trd-figure__value">{children}</p>
      </section>
    </Card>
  )
}

export function OverviewView({ portfolio }: { portfolio: Portfolio }) {
  const { totals } = portfolio
  const hidden = useBalancePrivacy((s) => s.hidden)
  const segments = allocationSegments(portfolio.holdings)
  return (
    <div className="flex flex-col gap-5" data-testid="trd-overview">
      <div className="trd-figures">
        <Figure label={t('webTrading.totalValue')} icon={<WalletIcon />} tone="accent">
          <Money value={totals.valueUsd} />
        </Figure>
        <Figure label={t('webTrading.change24h')} icon={<TrendingUp />} tone="info">
          <Money value={totals.change24hUsd} signed toned />
        </Figure>
        <Figure label={t('webTrading.unrealized')} icon={<Coins />} tone="ok">
          <Money value={totals.unrealizedUsd} signed toned />
        </Figure>
        <Figure label={t('webTrading.realized')} icon={<Coins />} tone="neutral">
          <Money value={totals.realizedUsd} signed toned />
        </Figure>
        <Figure label={t('webTrading.gasSpent')} icon={<Flame />} tone="warn">
          <Money value={totals.gasUsd} />
        </Figure>
      </div>

      {portfolio.unpricedCount ? (
        <p className="trd-note">
          {tPlural('webTrading.unpriced', portfolio.unpricedCount, {
            count: portfolio.unpricedCount,
          })}
        </p>
      ) : null}

      {segments.length > 0 ? (
        <Card asChild>
          <section aria-label={t('webTrading.allocation')} className="flex flex-col gap-3">
            <CardHeader title={t('webTrading.allocation')} />
            <div className="trd-alloc" aria-hidden>
              {segments.map((s, i) => (
                <span
                  key={s.key}
                  className="trd-alloc__seg"
                  data-series={s.key === 'other' ? 'other' : `s${(i % 6) + 1}`}
                  style={{ flexGrow: s.pct }}
                />
              ))}
            </div>
            <ul className="trd-alloc__legend">
              {segments.map((s, i) => (
                <li key={s.key}>
                  <span
                    className="trd-swatch"
                    data-series={s.key === 'other' ? 'other' : `s${(i % 6) + 1}`}
                    aria-hidden
                  />
                  <span className="truncate">
                    {s.key === 'other' ? t('webTrading.other') : clampSym(s.symbol)}
                  </span>
                  <span className="trd-num text-text-muted">
                    {hidden ? '··' : `${s.pct.toFixed(1)}%`}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        </Card>
      ) : null}

      {portfolio.wallets.length > 0 ? (
        <Card asChild padding="none">
          <section aria-label={t('webTrading.wallets')}>
            <CardHeader title={t('webTrading.wallets')} className="px-4 pt-4 pb-2" />
            <ul className="trd-rows">
              {portfolio.wallets.map(({ wallet, totals: wt }) => {
                const name = clampText(walletLabel(wallet), 32)
                const chain = wallet.chains[0] ?? 8453
                return (
                  <li key={wallet.address} className="trd-row trd-wallet">
                    <span className="trd-wallet__name">
                      <span className="truncate font-medium">{name}</span>
                      <ExplorerLink
                        href={addressUrl(chain, wallet.address)}
                        label={t('webTrading.viewOnExplorer', {
                          name,
                          chain: chainName(chain),
                        })}
                      >
                        <span className="trd-mono">{shortAddress(wallet.address)}</span>
                      </ExplorerLink>
                    </span>
                    <span
                      className="trd-wallet__chains"
                      aria-label={t('webTrading.walletNetworks')}
                    >
                      {wallet.chains.map((c) => (
                        <ChainChip key={c} chainId={c} />
                      ))}
                    </span>
                    <Money value={wt.valueUsd} className="trd-wallet__value" />
                  </li>
                )
              })}
            </ul>
          </section>
        </Card>
      ) : null}
    </div>
  )
}
