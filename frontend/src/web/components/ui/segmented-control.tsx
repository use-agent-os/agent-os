import * as React from 'react'
import { cn } from '@/lib/utils'
import { mergeRefs, useGlide } from './use-glide'

// Forced colors drop the pill: the chosen item takes the system highlight.
const FORCED_ACTIVE =
  'forced-colors:bg-[Highlight] forced-colors:text-[HighlightText] forced-colors:forced-color-adjust-none'

export interface SegmentedOption<V extends string> {
  value: V
  label: React.ReactNode
  icon?: React.ReactNode
  disabled?: boolean
}

/**
 * A segmented control whose active pill glides between options.
 *
 * `kind="radio"` (default) renders native radios in labels: one exclusive
 * choice, arrow keys move it, the group is a radiogroup. `kind="toggle"`
 * renders `aria-pressed` buttons for a view switch that should stay buttons.
 * `tone="accent"` tints the active pill with the accent (one per view).
 */
export function SegmentedControl<V extends string>({
  value,
  onChange,
  options,
  kind = 'radio',
  size = 'md',
  tone = 'neutral',
  name,
  className,
  ref,
  ...aria
}: {
  value: V
  onChange: (value: V) => void
  options: readonly SegmentedOption<V>[]
  kind?: 'radio' | 'toggle'
  size?: 'sm' | 'md'
  tone?: 'neutral' | 'accent'
  /** Radio group name; generated when omitted. */
  name?: string
  className?: string
  ref?: React.Ref<HTMLDivElement>
  'aria-label'?: string
  'aria-labelledby'?: string
  'aria-describedby'?: string
}) {
  const glide = useGlide<HTMLDivElement>()
  const generated = React.useId()
  const group = name ?? generated
  const item = cn(
    'target relative inline-flex cursor-pointer items-center justify-center gap-1.5 rounded-md font-medium whitespace-nowrap',
    'text-text-muted transition-colors duration-[var(--duration-fast)] hover:text-text',
    'data-[active=true]:text-text [&_svg]:size-4 [&_svg]:shrink-0',
    tone === 'accent' && 'data-[active=true]:text-accent',
    size === 'sm' ? 'h-7 px-2.5 text-small' : 'h-8 px-3 text-ui',
    'has-[:disabled]:cursor-not-allowed has-[:disabled]:opacity-50 disabled:cursor-not-allowed disabled:opacity-50',
  )
  return (
    <div
      ref={mergeRefs(glide, ref)}
      role={kind === 'radio' ? 'radiogroup' : 'group'}
      data-segmented
      className={cn(
        'glide-track inline-flex w-fit max-w-full flex-wrap items-center gap-0.5 rounded-control border border-border bg-surface-inset p-0.5 shadow-inset',
        tone === 'accent' && '[--glide-bg:var(--accent-tint)]',
        className,
      )}
      {...aria}
    >
      {options.map((option) => {
        const active = option.value === value
        const content = (
          <>
            {option.icon ? <span aria-hidden>{option.icon}</span> : null}
            {option.label}
          </>
        )
        return kind === 'radio' ? (
          <label
            key={option.value}
            data-active={active}
            className={cn(
              item,
              'has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-ring',
              active && FORCED_ACTIVE,
            )}
          >
            <input
              type="radio"
              className="sr-only"
              name={group}
              value={option.value}
              checked={active}
              disabled={option.disabled}
              onChange={() => onChange(option.value)}
            />
            {content}
          </label>
        ) : (
          <button
            key={option.value}
            type="button"
            aria-pressed={active}
            data-active={active}
            disabled={option.disabled}
            onClick={() => onChange(option.value)}
            className={cn(item, active && FORCED_ACTIVE)}
          >
            {content}
          </button>
        )
      })}
    </div>
  )
}
