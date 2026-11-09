import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import * as React from 'react'
import { cn } from '@/lib/utils'

export const buttonVariants = cva(
  [
    'target inline-flex shrink-0 items-center justify-center gap-2 rounded-md font-medium whitespace-nowrap',
    'text-ui transition-colors duration-[var(--duration-fast)] select-none',
    'disabled:pointer-events-none disabled:opacity-50 aria-disabled:opacity-50',
    '[&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0',
  ].join(' '),
  {
    variants: {
      variant: {
        primary: 'bg-accent text-accent-fg hover:bg-accent/90',
        secondary: 'bg-surface-3 text-text hover:bg-surface-3/80',
        outline: 'border border-border-control bg-transparent text-text hover:bg-surface-3',
        ghost: 'bg-transparent text-text hover:bg-surface-3',
        danger: 'bg-danger text-danger-fg hover:bg-danger/90',
        link: 'text-accent underline-offset-4 hover:underline',
      },
      size: {
        sm: 'h-7 px-2.5',
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
