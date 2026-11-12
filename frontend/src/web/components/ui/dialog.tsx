import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import * as React from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webCommon'
import { cn } from '@/lib/utils'
import { OverlayLayer } from './overlay-layer'
import { useRestoreFocus } from './restore-focus'

export const Dialog = DialogPrimitive.Root
export const DialogTrigger = DialogPrimitive.Trigger
export const DialogClose = DialogPrimitive.Close
export const DialogPortal = DialogPrimitive.Portal

export function DialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn(
        'anim-overlay-in fixed inset-0 z-[var(--z-overlay)] bg-scrim backdrop-blur-[2px]',
        className,
      )}
      {...props}
    />
  )
}

export function DialogContent({
  className,
  children,
  hideClose = false,
  onOpenAutoFocus,
  onCloseAutoFocus,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & { hideClose?: boolean }) {
  const focus = useRestoreFocus(onOpenAutoFocus, onCloseAutoFocus)
  return (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content
        data-slot="dialog-content"
        className={cn(
          'anim-dialog-in fixed top-1/2 left-1/2 z-[var(--z-overlay)] grid w-[calc(100vw-2rem)] max-w-lg',
          '-translate-x-1/2 -translate-y-1/2 gap-5 rounded-overlay border border-border bg-surface-overlay p-6 shadow-overlay',
          'max-h-[calc(100dvh-2rem)] overflow-y-auto',
          className,
        )}
        {...props}
        {...focus}
      >
        <OverlayLayer />
        {children}
        {hideClose ? null : (
          <DialogPrimitive.Close
            className="press target absolute top-3 right-3 inline-flex size-7 items-center justify-center rounded-md text-text-muted hover:bg-surface-3 hover:text-text touch:size-11"
            aria-label={t('webCommon.close')}
          >
            <X className="size-4" aria-hidden />
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPortal>
  )
}

export function DialogHeader({ className, ...props }: React.ComponentProps<'div'>) {
  return <div className={cn('flex flex-col gap-1.5 pr-8', className)} {...props} />
}

export function DialogFooter({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      className={cn('flex flex-col-reverse gap-2 sm:flex-row sm:justify-end', className)}
      {...props}
    />
  )
}

export function DialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      className={cn('text-[17px] leading-snug font-semibold tracking-[-0.016em]', className)}
      {...props}
    />
  )
}

export function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      className={cn('text-text-muted text-pretty', className)}
      {...props}
    />
  )
}
