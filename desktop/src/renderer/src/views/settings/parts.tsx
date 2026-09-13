import { AlertTriangle, CheckCircle2, Info, XCircle } from 'lucide-react'
import type { ReactNode } from 'react'

/**
 * The vocabulary every section is written in: a page head, a card with its
 * title inside, a row with a label column and a control column, a segmented
 * control, a pill, a notice. Visuals live in settings.css (.stg-*) and
 * tokens.css (.mac-*).
 */

export function Head({ title, blurb }: { title: string; blurb?: string }) {
  return (
    <header className="stg-head">
      <h1>{title}</h1>
      {blurb ? <p>{blurb}</p> : null}
    </header>
  )
}

export function Card({
  title,
  blurb,
  icon,
  action,
  children,
  foot,
  footNote,
}: {
  title?: string
  blurb?: ReactNode
  /** Left of the title: a brand mark. */
  icon?: ReactNode
  /** Top-right of the head: a pill, a button. */
  action?: ReactNode
  children: ReactNode
  /** Right-aligned actions under the rows (Save/Revert). */
  foot?: ReactNode
  /** Left-aligned text in the foot. */
  footNote?: ReactNode
}) {
  return (
    <section className="stg-card" aria-label={title}>
      {title ? (
        <div className="stg-card__head">
          <div className={icon ? 'prov-head' : undefined}>
            {icon}
            <div>
              <h2>{title}</h2>
              {blurb ? <p>{blurb}</p> : null}
            </div>
          </div>
          {action}
        </div>
      ) : null}
      {children}
      {foot || footNote ? (
        <div className="stg-card__foot">
          {footNote ? <span className="stg-foot-note">{footNote}</span> : null}
          {foot}
        </div>
      ) : null}
    </section>
  )
}

export function Row({
  label,
  help,
  children,
  align,
  wide,
  stack,
  htmlFor,
}: {
  label: string
  help?: ReactNode
  children?: ReactNode
  align?: 'start'
  /** Control column takes the remaining width. */
  wide?: boolean
  /** Control sits under the label at full width (long fields). */
  stack?: boolean
  htmlFor?: string
}) {
  const Label = htmlFor ? 'label' : 'span'
  return (
    <div className="stg-row" data-align={align} data-stack={stack ? 'true' : undefined}>
      <div className="stg-row__label">
        <Label htmlFor={htmlFor}>{label}</Label>
        {help ? <span className="stg-row__help">{help}</span> : null}
      </div>
      {children !== undefined ? (
        <div className="stg-row__control" data-wide={wide ? 'true' : undefined}>
          {children}
        </div>
      ) : null}
    </div>
  )
}

export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  label: string
  value: T
  options: readonly { value: T; label: string; icon?: ReactNode }[]
  onChange: (value: T) => void
  disabled?: boolean
}) {
  return (
    <div role="radiogroup" aria-label={label} className="mac-segmented">
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          role="radio"
          aria-checked={opt.value === value}
          className="mac-segment app-no-drag"
          disabled={disabled}
          onClick={() => onChange(opt.value)}
        >
          {opt.icon}
          {opt.label}
        </button>
      ))}
    </div>
  )
}

const NOTICE_ICON = {
  warn: AlertTriangle,
  ok: CheckCircle2,
  danger: XCircle,
  info: Info,
} as const

export function Notice({
  tone = 'warn',
  children,
  action,
}: {
  tone?: keyof typeof NOTICE_ICON
  children: ReactNode
  action?: ReactNode
}) {
  const Icon = NOTICE_ICON[tone]
  return (
    <div className="stg-notice" data-tone={tone} role="status">
      <Icon className="size-3.5" strokeWidth={2} aria-hidden />
      <span>{children}</span>
      {action}
    </div>
  )
}

export type Tone = 'ok' | 'warn' | 'danger' | 'primary'

/** Machine value in a row: a path, a URL, a version. */
export function Value({
  children,
  tone,
  title,
}: {
  children: ReactNode
  tone?: Tone
  title?: string
}) {
  return (
    <span className="stg-value" data-tone={tone} title={title}>
      {children}
    </span>
  )
}

/** A dot and a word: running, key set, up to date. */
export function Pill({
  children,
  tone,
  pulse,
}: {
  children: ReactNode
  tone?: Tone
  pulse?: boolean
}) {
  return (
    <span className="stg-pill" data-tone={tone} data-pulse={pulse ? 'true' : undefined}>
      {children}
    </span>
  )
}
