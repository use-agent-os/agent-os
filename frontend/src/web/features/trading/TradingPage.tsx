import { Eye, EyeOff, Lock, Power, Info, Vault, Wallet as WalletIcon } from 'lucide-react'
import { useId, useState, type ReactNode } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webTrading'
import { activeFirst, errorText, walletLabel } from '@/views/trading/logic'
import { Button } from '@/web/components/ui/button'
import { PageHeader } from '@/web/components/ui/page-header'
import { SegmentedControl } from '@/web/components/ui/segmented-control'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { PAGE_CLASS } from '@/web/features/home/overview-ui'
import '@/web/styles/page-trading.css'
import {
  usePortfolio,
  useTradingInvalidation,
  useTradingStatus,
  useVaultStatus,
  useWallets,
} from './data'
import { HoldingsView } from './HoldingsView'
import { HistoryView } from './HistoryView'
import { OrdersView } from './OrdersView'
import { OverviewView } from './OverviewView'
import { clampText, useBalancePrivacy } from './safety'

// The Trading page: the engine's wallets, holdings, orders, history, markets
// and liquidity positions, read-only. It walks the same gates the desktop
// desk does (engine on, vault created, vault unlocked, a wallet exists) and
// says at each one why nothing shows and where to change that; it never
// offers the change itself. Every gateway call goes through `data.ts`.

export type TradingView = 'overview' | 'holdings' | 'orders' | 'history'

const VIEWS: readonly { value: TradingView; key: Parameters<typeof t>[0] }[] = [
  { value: 'overview', key: 'webTrading.viewOverview' },
  { value: 'holdings', key: 'webTrading.viewHoldings' },
  { value: 'orders', key: 'webTrading.viewOrders' },
  { value: 'history', key: 'webTrading.viewHistory' },
]

function PrivacyToggle() {
  const hidden = useBalancePrivacy((s) => s.hidden)
  const toggle = useBalancePrivacy((s) => s.toggle)
  return (
    <Button variant="outline" size="sm" aria-pressed={hidden} onClick={toggle}>
      {hidden ? <Eye aria-hidden /> : <EyeOff aria-hidden />}
      {hidden ? t('webTrading.showAmounts') : t('webTrading.hideAmounts')}
    </Button>
  )
}

function Gate({ icon, title, body }: { icon: ReactNode; title: string; body: string }) {
  return <EmptyState icon={icon} title={title} description={body} />
}

function PageLoading() {
  return (
    <LoadingRegion label={t('webTrading.loading')} className="flex flex-col gap-5">
      <div className="trd-figures">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-24 rounded-card" />
        ))}
      </div>
      <Skeleton className="h-64 rounded-card" />
    </LoadingRegion>
  )
}

export function TradingPage() {
  useTradingInvalidation()
  const status = useTradingStatus()
  const enabled = status.data?.enabled === true
  const vault = useVaultStatus(enabled)
  const open = enabled && vault.data?.initialized === true && vault.data.unlocked
  const wallets = useWallets(open)
  const [wallet, setWallet] = useState<string>('')
  const [view, setView] = useState<TradingView>('overview')
  const ready = open && wallets.wallets.length > 0
  const portfolio = usePortfolio(wallet || undefined, ready)
  const pickerId = useId()

  let body: ReactNode
  if (status.isPending || (enabled && vault.isPending)) body = <PageLoading />
  else if (status.isError || vault.isError)
    body = (
      <ErrorState
        title={t('webTrading.errorTitle')}
        message={errorText(status.error ?? vault.error)}
        onRetry={() => void (status.isError ? status.refetch() : vault.refetch())}
      />
    )
  else if (!enabled)
    body = (
      <Gate icon={<Power />} title={t('webTrading.disabledTitle')} body={t('webTrading.disabledBody')} />
    )
  else if (!vault.data?.initialized)
    body = (
      <Gate icon={<Vault />} title={t('webTrading.noVaultTitle')} body={t('webTrading.noVaultBody')} />
    )
  else if (!vault.data.unlocked)
    body = <Gate icon={<Lock />} title={t('webTrading.lockedTitle')} body={t('webTrading.lockedBody')} />
  else if (wallets.isPending) body = <PageLoading />
  else if (wallets.isError)
    body = (
      <ErrorState
        title={t('webTrading.errorTitle')}
        message={errorText(wallets.error)}
        onRetry={() => void wallets.refetch()}
      />
    )
  else if (wallets.wallets.length === 0)
    body = (
      <Gate
        icon={<WalletIcon />}
        title={t('webTrading.noWalletsTitle')}
        body={t('webTrading.noWalletsBody')}
      />
    )
  else
    body = (
      <>
        <div className="trd-toolbar">
          <SegmentedControl
            aria-label={t('webTrading.views')}
            value={view}
            onChange={setView}
            options={VIEWS.map((v) => ({ value: v.value, label: t(v.key) }))}
            className="trd-views"
          />
          <label className="trd-picker" htmlFor={pickerId}>
            <span className="sr-only">{t('webTrading.walletPicker')}</span>
            <select
              id={pickerId}
              className="trd-select"
              value={wallet}
              onChange={(e) => setWallet(e.target.value)}
            >
              <option value="">{t('webTrading.allWallets')}</option>
              {activeFirst(wallets.wallets).map((w) => (
                <option key={w.address} value={w.address}>
                  {clampText(walletLabel(w), 32)}
                  {w.primary ? ` · ${t('webTrading.primary')}` : ''}
                </option>
              ))}
            </select>
          </label>
        </div>
        {view === 'orders' ? (
          <OrdersView wallet={wallet || undefined} />
        ) : view === 'history' ? (
          <HistoryView
            wallet={wallet || undefined}
            chains={(status.data?.chains ?? []).map((c) => c.chainId)}
            portfolio={portfolio.data}
          />
        ) : portfolio.isError && !portfolio.data ? (
          <ErrorState
            title={t('webTrading.errorTitle')}
            message={errorText(portfolio.error)}
            onRetry={() => void portfolio.refetch()}
          />
        ) : !portfolio.data ? (
          <PageLoading />
        ) : view === 'overview' ? (
          <OverviewView portfolio={portfolio.data} />
        ) : view === 'holdings' ? (
          <HoldingsView portfolio={portfolio.data} />
        ) : null}
      </>
    )

  return (
    <div className={PAGE_CLASS} data-testid="trading-page">
      <PageHeader
        title={t('webTrading.title')}
        description={t('webTrading.subtitle')}
        actions={ready ? <PrivacyToggle /> : null}
        footer={
          <p className="trd-readonly">
            <Info aria-hidden className="size-3.5 shrink-0" />
            {t('webTrading.readOnlyNote')}
          </p>
        }
      />
      {body}
    </div>
  )
}
