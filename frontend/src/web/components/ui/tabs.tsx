import * as TabsPrimitive from '@radix-ui/react-tabs'
import * as React from 'react'
import { cn } from '@/lib/utils'
import { mergeRefs, useGlide } from './use-glide'

export const Tabs = TabsPrimitive.Root

/** A tab strip whose active pill glides between tabs (`.glide-track`). */
export function TabsList({
  className,
  ref,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List>) {
  const glide = useGlide<HTMLDivElement>()
  return (
    <TabsPrimitive.List
      ref={mergeRefs(glide, ref)}
      className={cn(
        'glide-track inline-flex items-center gap-0.5 rounded-control border border-border bg-surface-inset p-0.5 shadow-inset',
        className,
      )}
      {...props}
    />
  )
}

export function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        'target inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1 text-ui font-medium text-text-muted',
        'transition-colors duration-[var(--duration-fast)] hover:text-text data-[state=active]:text-text',
        'disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4',
        className,
      )}
      {...props}
    />
  )
}

export const TabsContent = TabsPrimitive.Content
