import { cva, type VariantProps } from 'class-variance-authority'
import * as React from 'react'
import { cn } from '@/lib/utils'

// Text fields and the styled native <select>. The inset fill, inner shadow,
// hover step and ring-plus-glow focus come from base.css (it keys on
// `border-border-control`), so hand-rolled fields that use the same class
// string look identical. `aria-invalid` turns the boundary red.

export const fieldVariants = cva(
  [
    'w-full min-w-0 rounded-control border border-border-control bg-surface-inset text-ui text-text',
    'aria-[invalid=true]:border-danger touch:text-base',
  ].join(' '),
  {
    variants: {
      size: {
        sm: 'h-8 px-2.5',
        md: 'h-9 px-3 touch:h-11',
        lg: 'h-10 px-3 touch:h-11',
      },
    },
    defaultVariants: { size: 'md' },
  },
)

type FieldSize = VariantProps<typeof fieldVariants>['size']

export interface InputProps extends Omit<React.ComponentProps<'input'>, 'size'> {
  size?: FieldSize
  /** A leading icon (e.g. lucide `Search`); decorative. */
  icon?: React.ReactNode
  /** Classes for the wrapper rendered when `icon` is set (layout: width, flex). */
  wrapperClassName?: string
}

export function Input({ className, size, icon, wrapperClassName, ...props }: InputProps) {
  const input = (
    <input
      data-slot="input"
      className={cn(fieldVariants({ size }), icon ? 'pl-8' : null, className)}
      {...props}
    />
  )
  if (!icon) return input
  return (
    <span className={cn('relative block min-w-0', wrapperClassName)}>
      <span
        aria-hidden
        className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-text-muted [&_svg]:size-4"
      >
        {icon}
      </span>
      {input}
    </span>
  )
}

export function Textarea({ className, ...props }: React.ComponentProps<'textarea'>) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        fieldVariants({ size: null }),
        'min-h-20 px-3 py-2 leading-normal [resize:vertical]',
        className,
      )}
      {...props}
    />
  )
}

export interface SelectProps extends Omit<React.ComponentProps<'select'>, 'size'> {
  size?: FieldSize
}

/**
 * A styled native <select>: the platform picker (keyboard, screen readers,
 * mobile wheels) with our chevron and field chrome. Pass <option>s as usual.
 */
export function Select({ className, size, ...props }: SelectProps) {
  return (
    <select
      data-slot="select"
      className={cn(
        fieldVariants({ size }),
        'cursor-pointer disabled:cursor-not-allowed',
        className,
      )}
      {...props}
    />
  )
}
