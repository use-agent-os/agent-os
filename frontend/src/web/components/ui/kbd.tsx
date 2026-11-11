import * as React from 'react'
import { cn } from '@/lib/utils'

/** A keyboard key cap: `<Kbd>⌘</Kbd><Kbd>K</Kbd>`. */
export function Kbd({ className, ...props }: React.ComponentProps<'kbd'>) {
  return (
    <kbd
      data-slot="kbd"
      className={cn(
        'inline-flex h-5 min-w-5 items-center justify-center rounded-[5px] border border-border-strong bg-surface-2 px-1',
        'font-sans text-small leading-none font-medium text-text-muted shadow-[inset_0_-1px_0_var(--border-strong)]',
        className,
      )}
      {...props}
    />
  )
}
