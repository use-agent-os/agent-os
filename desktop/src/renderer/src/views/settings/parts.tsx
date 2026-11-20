import { AlertTriangle, Check, CheckCircle2, ChevronRight, Info, XCircle } from 'lucide-react'
import { useId, useState, type ReactNode } from 'react'

/**
 * The vocabulary every section is written in: a page head, a card with its
 * title inside, a row with a label column and a control column, a segmented
 * control, a pill, a notice; and for the composed pages a strip of summary
 * stats, in-page tabs, choice cards, a meter, a disclosure and an empty
 * state. Visuals live in settings.css (.stg-*) and
 * tokens.css (.mac-*).
 */

export function Head({
  title,
  blurb,
  action,
}: {
  title: string
  blurb?: string
  /** Right of the title: the page's one primary action, or a status pill. */
  action?: ReactNode
}) {
  return (
    <header className="stg-head">
      <div>
        <h1>{title}</h1>
        {blurb ? <p>{blurb}</p> : null}
      </div>
      {action ? <div className="stg-head__action">{action}</div> : null}
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

/**
 * The page's numbers at a glance: one strip of figures instead of a card of
 * rows that each hold a single number. A stat with `onClick` is a button
 * (jump to the list it counts).
 */
export function Stats({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="stg-stats" role="group" aria-label={label}>
      {children}
    </div>
  )
}

export function Stat({
  label,
  value,
  hint,
  tone,
  icon,
  onClick,
}: {
  label: string
  value: ReactNode
  /** Small text under the label: what the figure means, or its unit. */
  hint?: ReactNode
  tone?: Tone
  icon?: ReactNode
  onClick?: () => void
}) {
  const body = (
    <>
      <span className="stg-stat__value" data-tone={tone}>
        {icon}
        {value}
      </span>
      <span className="stg-stat__label">{label}</span>
      {hint ? <span className="stg-stat__hint">{hint}</span> : null}
    </>
  )
  return onClick ? (
    <button type="button" className="stg-stat app-no-drag" onClick={onClick}>
      {body}
    </button>
  ) : (
    <div className="stg-stat">{body}</div>
  )
}

/** In-page tabs for a section with more than one job (Memory). */
export function Tabs<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: T
  options: readonly { value: T; label: string; icon?: ReactNode; count?: number }[]
  onChange: (value: T) => void
}) {
  function onKey(e: React.KeyboardEvent) {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return
    const i = options.findIndex((o) => o.value === value)
    const next = options[(i + (e.key === 'ArrowRight' ? 1 : options.length - 1)) % options.length]
    if (!next) return
    e.preventDefault()
    onChange(next.value)
    const tabs = e.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]')
    tabs[options.indexOf(next)]?.focus()
  }
  return (
    <div role="tablist" aria-label={label} className="stg-tabs" onKeyDown={onKey}>
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          role="tab"
          aria-selected={opt.value === value}
          tabIndex={opt.value === value ? 0 : -1}
          className="stg-tab app-no-drag"
          onClick={() => onChange(opt.value)}
        >
          {opt.icon}
          {opt.label}
          {opt.count !== undefined ? <span className="stg-tab__count">{opt.count}</span> : null}
        </button>
      ))}
    </div>
  )
}

export interface ChoiceOption<T extends string> {
  value: T
  title: string
  blurb?: string
  icon?: ReactNode
  /** A short tag next to the title: Recommended, Local, Needs a key. */
  tag?: string
  tagTone?: Tone
  disabled?: boolean
}

/**
 * Pick one of a few options that each need a sentence to explain: cards in a
 * radio group rather than a select whose options hide their meaning.
 */
export function Choices<T extends string>({
  label,
  value,
  options,
  onChange,
  columns = 2,
  disabled,
}: {
  label: string
  value: T | null
  options: readonly ChoiceOption<T>[]
  onChange: (value: T) => void
  columns?: 1 | 2 | 3
  disabled?: boolean
}) {
  return (
    <div role="radiogroup" aria-label={label} className="stg-choices" data-columns={columns}>
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          role="radio"
          aria-checked={opt.value === value}
          disabled={disabled || opt.disabled}
          className="stg-choice app-no-drag"
          onClick={() => onChange(opt.value)}
        >
          {opt.icon ? <span className="stg-choice__icon">{opt.icon}</span> : null}
          <span className="stg-choice__text">
            <span className="stg-choice__title">
              {opt.title}
              {opt.tag ? (
                <span className="stg-choice__tag" data-tone={opt.tagTone}>
                  {opt.tag}
                </span>
              ) : null}
            </span>
            {opt.blurb ? <span className="stg-choice__blurb">{opt.blurb}</span> : null}
          </span>
          <span className="stg-choice__check" aria-hidden>
            <Check className="size-3" strokeWidth={3} />
          </span>
        </button>
      ))}
    </div>
  )
}

/** A budget: how much of a limit is used. Turns amber near the top, red at it. */
export function Meter({
  label,
  value,
  max,
  caption,
}: {
  label: string
  value: number
  max: number
  /** Right of the label: "1,240 of 4,000 characters". */
  caption?: ReactNode
}) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0
  const tone = pct >= 100 ? 'danger' : pct >= 85 ? 'warn' : undefined
  return (
    <div className="stg-meter" data-tone={tone}>
      <div className="stg-meter__text">
        <span>{label}</span>
        {caption ? <span className="stg-meter__caption">{caption}</span> : null}
      </div>
      <div
        className="stg-meter__track"
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
      >
        <div className="stg-meter__fill" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

/** Rows most people never need, folded under one line inside a card. */
export function Disclosure({
  title,
  hint,
  children,
  defaultOpen = false,
}: {
  title: string
  /** Right of the title while closed: what is inside, or the current values. */
  hint?: ReactNode
  children: ReactNode
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const id = useId()
  return (
    <div className="stg-disclosure" data-open={open ? 'true' : undefined}>
      <button
        type="button"
        className="stg-disclosure__summary app-no-drag"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
      >
        <ChevronRight className="size-3.5" strokeWidth={2} aria-hidden />
        <span>{title}</span>
        {hint && !open ? <span className="stg-disclosure__hint">{hint}</span> : null}
      </button>
      {open ? <div id={id}>{children}</div> : null}
    </div>
  )
}

/** Nothing here yet: say what would be here and offer the way to add it. */
export function Empty({
  icon,
  title,
  body,
  action,
}: {
  icon?: ReactNode
  title: string
  body?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="stg-empty">
      {icon ? <span className="stg-empty__icon">{icon}</span> : null}
      <p className="stg-empty__title">{title}</p>
      {body ? <p className="stg-empty__body">{body}</p> : null}
      {action ? <div className="stg-empty__action">{action}</div> : null}
    </div>
  )
}
