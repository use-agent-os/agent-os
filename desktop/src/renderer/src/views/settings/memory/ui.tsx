import { LoaderCircle, X } from 'lucide-react'
import { useId, type ReactNode } from 'react'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '~/components/ui/button'
import { t, type MessageKey } from '~/i18n'

/** `t()` with `{name}` placeholders filled in. */
export function fill(key: MessageKey, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce(
    (text, [name, value]) => text.split(`{${name}}`).join(String(value)),
    t(key),
  )
}

export function count(n: number | undefined): string {
  return n === undefined ? t('settings.memory.dash') : n.toLocaleString('en-US')
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="mem-loading">
      <LoaderCircle className="stg-spin size-3.5" strokeWidth={1.75} aria-hidden />
      {label ?? t('settings.loading')}
    </span>
  )
}

/** Destructive confirm in the house style (`.stg-confirm`). */
export function Confirm({
  title,
  body,
  confirm,
  busy,
  onCancel,
  onConfirm,
}: {
  title: string
  body?: ReactNode
  confirm: string
  busy?: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={body ? bodyId : undefined}
      onClose={onCancel}
      dismissible={!busy}
      overlayClassName="stg-confirm__overlay"
      className="stg-confirm mem-confirm"
    >
      <h2 id={titleId}>{title}</h2>
      {body ? (
        <div id={bodyId} className="mem-confirm__body">
          {body}
        </div>
      ) : null}
      <div className="stg-confirm__actions">
        <Button disabled={busy} onClick={onCancel}>
          {t('settings.memory.cancel')}
        </Button>
        <Button variant="danger" disabled={busy} onClick={onConfirm}>
          {confirm}
        </Button>
      </div>
    </ModalShell>
  )
}

/** A sheet over the settings: a head, a scrolling body, an optional foot. */
export function Sheet({
  title,
  blurb,
  foot,
  dismissible = true,
  wide,
  onClose,
  children,
}: {
  title: ReactNode
  blurb?: ReactNode
  foot?: ReactNode
  dismissible?: boolean
  wide?: boolean
  onClose: () => void
  children: ReactNode
}) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <ModalShell
      role="dialog"
      labelledBy={titleId}
      describedBy={blurb ? bodyId : undefined}
      onClose={onClose}
      dismissible={dismissible}
      overlayClassName="mem-sheet__overlay"
      className="mem-sheet"
    >
      <div className="mem-sheet__frame" data-wide={wide ? 'true' : undefined}>
        <header className="mem-sheet__head">
          <div>
            <h2 id={titleId}>{title}</h2>
            {blurb ? <p id={bodyId}>{blurb}</p> : null}
          </div>
          <Button
            variant="ghost"
            size="icon"
            aria-label={t('settings.memory.close')}
            disabled={!dismissible}
            onClick={onClose}
          >
            <X className="size-3.5" strokeWidth={1.75} aria-hidden />
          </Button>
        </header>
        <div className="mem-sheet__body">{children}</div>
        {foot ? <footer className="mem-sheet__foot">{foot}</footer> : null}
      </div>
    </ModalShell>
  )
}
