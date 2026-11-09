import * as ContextMenuPrimitive from '@radix-ui/react-context-menu'
import * as React from 'react'
import { cn } from '@/lib/utils'
import { menuContentClass, menuItemClass, menuSeparatorClass } from './menu-styles'

export const ContextMenu = ContextMenuPrimitive.Root
export const ContextMenuTrigger = ContextMenuPrimitive.Trigger

export function ContextMenuContent({
  className,
  ...props
}: React.ComponentProps<typeof ContextMenuPrimitive.Content>) {
  return (
    <ContextMenuPrimitive.Portal>
      <ContextMenuPrimitive.Content className={cn(menuContentClass, className)} {...props} />
    </ContextMenuPrimitive.Portal>
  )
}

export function ContextMenuItem({
  className,
  variant,
  ...props
}: React.ComponentProps<typeof ContextMenuPrimitive.Item> & { variant?: 'danger' }) {
  return (
    <ContextMenuPrimitive.Item
      data-variant={variant}
      className={cn(menuItemClass, className)}
      {...props}
    />
  )
}

export function ContextMenuSeparator({
  className,
  ...props
}: React.ComponentProps<typeof ContextMenuPrimitive.Separator>) {
  return <ContextMenuPrimitive.Separator className={cn(menuSeparatorClass, className)} {...props} />
}
