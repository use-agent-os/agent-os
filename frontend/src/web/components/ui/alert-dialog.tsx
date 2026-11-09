import * as AlertDialogPrimitive from '@radix-ui/react-alert-dialog'
import * as React from 'react'
import { cn } from '@/lib/utils'
import { buttonVariants } from './button'

export const AlertDialog = AlertDialogPrimitive.Root
export const AlertDialogTrigger = AlertDialogPrimitive.Trigger

export function AlertDialogContent({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Content>) {
  return (
    <AlertDialogPrimitive.Portal>
      <AlertDialogPrimitive.Overlay className="anim-overlay-in fixed inset-0 z-[var(--z-overlay)] bg-scrim" />
      <AlertDialogPrimitive.Content
        data-slot="alert-dialog-content"
        className={cn(
          'anim-dialog-in fixed top-1/2 left-1/2 z-[var(--z-overlay)] grid w-[calc(100vw-2rem)] max-w-md',
          '-translate-x-1/2 -translate-y-1/2 gap-4 rounded-xl border border-border bg-surface-2 p-6 shadow-3',
          className,
        )}
        {...props}
      />
    </AlertDialogPrimitive.Portal>
  )
}

export function AlertDialogHeader({ className, ...props }: React.ComponentProps<'div'>) {
  return <div className={cn('flex flex-col gap-1.5', className)} {...props} />
}

export function AlertDialogFooter({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      className={cn('flex flex-col-reverse gap-2 sm:flex-row sm:justify-end', className)}
      {...props}
    />
  )
}

export function AlertDialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Title>) {
  return (
    <AlertDialogPrimitive.Title className={cn('text-base font-semibold', className)} {...props} />
  )
}

export function AlertDialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Description>) {
  return (
    <AlertDialogPrimitive.Description className={cn('text-text-muted', className)} {...props} />
  )
}

export function AlertDialogAction({
  className,
  destructive = false,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Action> & { destructive?: boolean }) {
  return (
    <AlertDialogPrimitive.Action
      className={cn(buttonVariants({ variant: destructive ? 'danger' : 'primary' }), className)}
      {...props}
    />
  )
}

export function AlertDialogCancel({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Cancel>) {
  return (
    <AlertDialogPrimitive.Cancel
      className={cn(buttonVariants({ variant: 'outline' }), className)}
      {...props}
    />
  )
}
