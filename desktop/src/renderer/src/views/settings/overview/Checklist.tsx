import { AlertTriangle, Check, LoaderCircle } from 'lucide-react'
import type { ReactNode } from 'react'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { Card } from '../parts'
import type { SetupItem, SetupState } from './setup-items'

/** A check when done, an empty ring to do, a triangle when broken, a spinner while checking. */
export function StateMark({ state }: { state: SetupState }) {
  return (
    <span
      className="ovw-mark"
      data-state={state}
      role="img"
      aria-label={t(`settings.overview.state.${state}`)}
    >
      {state === 'done' ? (
        <Check className="size-3" strokeWidth={3} aria-hidden />
      ) : state === 'attention' ? (
        <AlertTriangle className="size-3.5" strokeWidth={2.25} aria-hidden />
      ) : state === 'checking' ? (
        <LoaderCircle className="stg-spin size-3.5" strokeWidth={2} aria-hidden />
      ) : null}
    </span>
  )
}

export function SetupRow({
  item,
  onAction,
  busy,
}: {
  item: SetupItem
  onAction: (item: SetupItem) => void
  busy?: boolean
}) {
  // The hero holds the page's one primary button; rows stay quiet, and a
  // done row's button recedes further.
  return (
    <li className="ovw-item" data-state={item.state} data-testid={`setup-item-${item.id}`}>
      <StateMark state={item.state} />
      <div className="ovw-item__text">
        <span className="ovw-item__title">{item.title}</span>
        <span className="ovw-item__detail">{item.detail}</span>
      </div>
      <Button
        variant={item.state === 'done' ? 'ghost' : 'secondary'}
        disabled={busy || item.state === 'checking'}
        aria-label={`${item.action}: ${item.title}`}
        onClick={() => onAction(item)}
      >
        {item.action}
      </Button>
    </li>
  )
}

export function Checklist({
  title,
  blurb,
  items,
  onAction,
  busy,
  children,
}: {
  title: string
  blurb: string
  items: readonly SetupItem[]
  onAction: (item: SetupItem) => void
  busy?: boolean
  /** Under the rows: a note when the list is partial. */
  children?: ReactNode
}) {
  const done = items.filter((i) => i.state === 'done').length
  const counted = items.filter((i) => i.state !== 'checking').length
  return (
    <Card
      title={title}
      blurb={blurb}
      action={
        counted > 0 ? (
          <span className="ovw-count" aria-hidden>
            {done}/{counted}
          </span>
        ) : null
      }
    >
      <ul className="ovw-list">
        {items.map((item) => (
          <SetupRow key={item.id} item={item} onAction={onAction} busy={busy} />
        ))}
      </ul>
      {children}
    </Card>
  )
}
