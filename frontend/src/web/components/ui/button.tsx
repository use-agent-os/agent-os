import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import * as React from 'react'
import { cn } from '@/lib/utils'

// One primary per view: the lime (olive in light) gradient with a top sheen
// and a soft glow on hover. Everything else stays quiet. `press` gives every
// variant the pressed shrink.
export const buttonVariants = cva(
  [
    'press target inline-flex shrink-0 items-center justify-center gap-2 rounded-control font-medium whitespace-nowrap',
    'text-ui select-none',
    'disabled:pointer-events-none disabled:opacity-50 aria-disabled:opacity-50',
    '[&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0',
  ].join(' '),
  {
    variants: {
      variant: {
        primary: [
          'bg-accent [background-image:var(--accent-gradient)] text-accent-fg shadow-[var(--elevation-primary)]',
          'hover:shadow-[var(--elevation-primary-hover)] hover:brightness-[1.04]',
        ].join(' '),
        // Secondary and outline read as one family (an outlined control on
        // the surface beneath); secondary only adds the dark top highlight.
        secondary:
          'border border-border-control bg-transparent text-text shadow-highlight hover:border-[color-mix(in_oklab,var(--border-control),var(--text)_28%)] hover:bg-surface-3',
        outline:
          'border border-border-control bg-transparent text-text hover:border-[color-mix(in_oklab,var(--border-control),var(--text)_28%)] hover:bg-surface-3',
        ghost: 'bg-transparent text-text hover:bg-surface-3',
        danger:
          'bg-danger text-danger-fg shadow-[var(--elevation-primary)] hover:bg-[color-mix(in_oklab,var(--danger),black_8%)]',
        link: 'text-accent underline-offset-4 hover:underline',
      },
      size: {
        sm: 'h-7 px-2.5 [&_svg]:size-3.5',
        md: 'h-9 px-3.5',
        lg: 'h-11 px-5',
        icon: 'size-9 touch:size-11',
        'icon-sm': 'size-7 touch:size-11',
      },
    },
    defaultVariants: { variant: 'secondary', size: 'md' },
  },
)

export interface ButtonProps
  extends React.ComponentProps<'button'>, VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

export function Button({ className, variant, size, asChild = false, ...props }: ButtonProps) {
  const Comp = asChild ? Slot : 'button'
  return (
    <Comp
      data-slot="button"
      type={asChild ? undefined : (props.type ?? 'button')}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  )
}
