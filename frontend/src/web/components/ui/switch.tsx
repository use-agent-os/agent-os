import * as SwitchPrimitive from '@radix-ui/react-switch'
import * as React from 'react'
import { cn } from '@/lib/utils'

export function Switch({ className, ...props }: React.ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        'peer inline-flex h-6 w-10 shrink-0 items-center rounded-full border border-border-control p-0.5',
        'bg-surface-3 transition-colors data-[state=checked]:border-accent data-[state=checked]:bg-accent',
        'disabled:cursor-not-allowed disabled:opacity-50',
        // Forced colors would flatten track and thumb to Canvas: paint them
        // with system colours so the thumb shows and "on" reads as Highlight.
        'forced-colors:forced-color-adjust-none forced-colors:border-[ButtonText] forced-colors:bg-[Canvas]',
        'forced-colors:data-[state=checked]:border-[Highlight] forced-colors:data-[state=checked]:bg-[Highlight]',
        'forced-colors:disabled:border-[GrayText] forced-colors:disabled:data-[state=checked]:border-[GrayText]',
        'forced-colors:disabled:data-[state=checked]:bg-[GrayText]',
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        className={cn(
          'pointer-events-none block size-4 rounded-full bg-text-muted shadow-1 transition-transform',
          'data-[state=checked]:translate-x-4 data-[state=checked]:bg-accent-fg',
          'forced-colors:bg-[ButtonText] forced-colors:data-[state=checked]:bg-[HighlightText]',
          'forced-colors:data-[disabled]:bg-[GrayText] forced-colors:data-[disabled]:data-[state=checked]:bg-[Canvas]',
        )}
      />
    </SwitchPrimitive.Root>
  )
}
