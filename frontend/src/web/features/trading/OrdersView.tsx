import { ArrowRight, ListOrdered } from 'lucide-react'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webTrading'
import {
  errorText,
  formatAmount,
  formatUsd,
  isAwaitingApproval,
  isOrderLive,
  shortHash,
} from '@/views/trading/logic'
import {
  isLiveMandate,
  isLiveTrigger,
  type Bracket,
  type Mandate,
  type Order,
  type Trigger,
} from '@/views/trading/types'
import { Card, CardHeader } from '@/web/components/ui/card'
import { EmptyState, ErrorState, Skeleton } from '@/web/components/ui/states'
import { useAutomations, useOrders } from './data'
import {
  Amount,
  ChainChip,
  EngineNote,
  ExplorerLink,
  formatWhen,
  initiatorLabel,
  kindLabel,
  Money,
  StatusBadge,
} from './parts'
import { clampSym, clampText, namesFigure, txUrl, useMask } from './safety'

// Orders: what the engine is running or waiting on, then what finished, and
// the automations (DCA plans, price triggers, brackets). Approving,
// rejecting, cancelling and pausing all live in the desktop app; an order
// waiting on approval says so in words.

function OrderRow({ order }: { order: Order }) {
  const href = txUrl(order.chainId, order.txHash)
  const into = order.kind === 'send' || order.kind === 'revoke' ? null : order.tokenOut
  const note = order.note ?? order.reason
  return (
    <li className="trd-row trd-order" data-testid="trd-order">
      <div className="trd-order__main">
        <div className="trd-order__title">
          <span className="font-medium">{kindLabel(order.kind)}</span>
          <ChainChip chainId={order.chainId} />
          <StatusBadge status={order.status} />
        </div>
        <div className="trd-order__legs">
          <Amount text={formatAmount(order.amountIn)} />
          <span className="truncate">{clampSym(order.tokenIn.symbol)}</span>
          {into ? (
            <>
              <ArrowRight aria-hidden className="size-3.5 shrink-0 text-text-muted" />
              <Amount text={formatAmount(order.receivedOut ?? order.expectedOut)} />
              <span className="truncate">{clampSym(into.symbol)}</span>
            </>
          ) : null}
        </div>
        <EngineNote text={note} />
      </div>
      <div className="trd-order__side">
        <Money value={order.valueUsd} />
        <span className="trd-note">
          {initiatorLabel(order.initiator)} · {formatWhen(order.createdAt)}
        </span>
        {href ? (
          <ExplorerLink href={href} label={t('webTrading.openTx')}>
            <span className="trd-mono">{shortHash(order.txHash)}</span>
          </ExplorerLink>
        ) : null}
      </div>
    </li>
  )
}

function OrderList({ title, orders }: { title: string; orders: Order[] }) {
  return (
    <Card asChild padding="none">
      <section aria-label={title}>
        <CardHeader title={title} className="px-4 pt-4 pb-2" />
        {orders.length === 0 ? (
          <p className="trd-note px-4 pb-4">{t('webTrading.noOpenOrders')}</p>
        ) : (
          <ul className="trd-rows">
            {orders.map((o) => (
              <OrderRow key={o.orderId} order={o} />
            ))}
          </ul>
        )}
      </section>
    </Card>
  )
}

type Automation =
  | { type: 'dca'; item: Mandate }
  | { type: 'trigger'; item: Trigger }
  | { type: 'bracket'; item: Bracket }

function AutomationRow({ a }: { a: Automation }) {
  const mask = useMask()
  const { item } = a
  let detail: string
  if (a.type === 'dca')
    detail = `${t('webTrading.dcaRuns', { schedule: clampText(a.item.schedule.label, 24) })} · ${t(
      'webTrading.dcaSpent',
      {
        spent: mask(formatUsd(a.item.budget.spentUsd)),
        cap: mask(formatUsd(a.item.budget.capUsd)),
      },
    )}`
  else if (a.type === 'trigger')
    // An action that names what it moves ("buy $50 of ETH") is a figure like
    // any other; one that moves nothing ("notify") stays readable.
    detail = `${clampText(a.item.condition.label, 40)} · ${
      namesFigure(a.item.action.label)
        ? mask(clampText(a.item.action.label, 48))
        : clampText(a.item.action.label, 48)
    }`
  else
    detail = t('webTrading.bracketLines', {
      tp: clampText(a.item.lines.takeProfitLabel, 24),
      sl: clampText(a.item.lines.stopLossLabel, 24),
    })
  const typeLabel =
    a.type === 'dca'
      ? t('webTrading.dca')
      : a.type === 'trigger'
        ? t('webTrading.trigger')
        : t('webTrading.bracket')
  return (
    <li className="trd-row trd-order" data-testid={`trd-automation-${a.type}`}>
      <div className="trd-order__main">
        <div className="trd-order__title">
          <span className="trd-note">{typeLabel}</span>
          <span className="truncate font-medium">{clampText(item.name, 40)}</span>
          <ChainChip chainId={item.chain.id} />
          <StatusBadge status={item.status} />
        </div>
        <p className="trd-order__note">
          <span className="font-medium text-text">{clampSym(item.token.symbol)}</span> · {detail}
        </p>
      </div>
    </li>
  )
}

function Automations() {
  const { dca, triggers, brackets } = useAutomations(true)
  const pending = dca.isPending || triggers.isPending || brackets.isPending
  const failed = dca.isError || triggers.isError || brackets.isError
  const items: Automation[] = [
    ...(dca.data?.mandates ?? []).map((item) => ({ type: 'dca' as const, item })),
    ...(triggers.data?.triggers ?? [])
      .filter((item) => !item.bracket)
      .map((item) => ({ type: 'trigger' as const, item })),
    ...(brackets.data?.brackets ?? []).map((item) => ({ type: 'bracket' as const, item })),
  ]
  // Live ones first; within each group the list keeps the engine's order.
  const live = (a: Automation) =>
    a.type === 'dca' ? isLiveMandate(a.item.status) : isLiveTrigger(a.item.status)
  items.sort((x, y) => Number(live(y)) - Number(live(x)))
  return (
    <Card asChild padding="none">
      <section aria-label={t('webTrading.automations')}>
        <CardHeader title={t('webTrading.automations')} className="px-4 pt-4 pb-2" />
        {failed && items.length === 0 ? (
          <div className="px-4 pb-4">
            <ErrorState
              title={t('webTrading.automationsFailed')}
              message={errorText(dca.error ?? triggers.error ?? brackets.error)}
              onRetry={() => {
                void dca.refetch()
                void triggers.refetch()
                void brackets.refetch()
              }}
            />
          </div>
        ) : pending && items.length === 0 ? (
          <div className="px-4 pb-4">
            <Skeleton className="h-12 rounded-[14px]" />
          </div>
        ) : items.length === 0 ? (
          <p className="trd-note px-4 pb-4">{t('webTrading.automationsEmpty')}</p>
        ) : (
          <ul className="trd-rows">
            {items.map((a) => (
              <AutomationRow key={`${a.type}:${a.item.id}`} a={a} />
            ))}
          </ul>
        )}
      </section>
    </Card>
  )
}

export function OrdersView({ wallet }: { wallet: string | undefined }) {
  const query = useOrders(wallet, true)
  // An order waiting for approval has not finished: it can still execute.
  const isOpen = (o: Order) => isOrderLive(o) || isAwaitingApproval(o)
  const open = query.orders.filter(isOpen)
  const finished = query.orders.filter((o) => !isOpen(o))
  const awaiting = open.filter(isAwaitingApproval).length

  let orders
  if (query.isError && !query.data)
    orders = (
      <ErrorState
        title={t('webTrading.errorTitle')}
        message={errorText(query.error)}
        onRetry={() => void query.refetch()}
      />
    )
  else if (query.isPending) orders = <Skeleton className="h-40 rounded-card" />
  else if (query.orders.length === 0)
    orders = (
      <EmptyState
        size="sm"
        icon={<ListOrdered />}
        title={t('webTrading.ordersEmpty')}
        description={t('webTrading.ordersEmptyBody')}
      />
    )
  else
    orders = (
      <>
        {awaiting > 0 ? (
          <p className="trd-callout" role="note">
            {tPlural('webTrading.awaitingNote', awaiting, { count: awaiting })}
          </p>
        ) : null}
        <OrderList title={t('webTrading.openOrders')} orders={open} />
        {finished.length > 0 ? (
          <OrderList title={t('webTrading.finishedOrders')} orders={finished} />
        ) : null}
      </>
    )

  return (
    <div className="flex flex-col gap-4" data-testid="trd-orders">
      {orders}
      <Automations />
    </div>
  )
}
