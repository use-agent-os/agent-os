import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import * as React from 'react'
import { cn } from '@/lib/utils'

export function TooltipProvider({
  delayDuration = 400,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return <TooltipPrimitive.Provider delayDuration={delayDuration} {...props} />
}

export const Tooltip = TooltipPrimitive.Root
export const TooltipTrigger = TooltipPrimitive.Trigger

export function TooltipContent({
  className,
  sideOffset = 6,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        sideOffset={sideOffset}
        className={cn(
          'anim-pop z-[var(--z-tooltip)] max-w-xs rounded-md px-2 py-1 text-small font-medium shadow-2',
          // Light: an ink chip. Dark: a lifted overlay chip with a hairline.
          'bg-text text-surface-0 dark:border dark:border-border-strong dark:bg-surface-overlay dark:text-text',
          className,
        )}
        {...props}
      />
    </TooltipPrimitive.Portal>
  )
}

/** A tooltip that also shows on keyboard focus (Radix does both by default). */
export function SimpleTooltip({
  label,
  side,
  children,
}: {
  label: React.ReactNode
  side?: React.ComponentProps<typeof TooltipPrimitive.Content>['side']
  children: React.ReactElement
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side={side}>{label}</TooltipContent>
    </Tooltip>
  )
}
