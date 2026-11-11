import { RotateCcw } from 'lucide-react'
import { t } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webShell'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/web/components/ui/alert-dialog'

/** Confirm a session reset: it clears the conversation the agent sees. */
export function ResetSessionDialog(props: {
  open: boolean
  onOpenChange(open: boolean): void
  title: string
  onConfirm(): void
  onCloseAutoFocus?(event: Event): void
}) {
  return (
    <AlertDialog open={props.open} onOpenChange={props.onOpenChange}>
      <AlertDialogContent onCloseAutoFocus={props.onCloseAutoFocus}>
        <AlertDialogHeader className="flex-row items-start gap-3.5">
          <span
            aria-hidden
            className="grid size-10 shrink-0 place-items-center rounded-card border border-danger/25 bg-danger/10 text-danger [&_svg]:size-[18px]"
          >
            <RotateCcw />
          </span>
          <div className="flex min-w-0 flex-col gap-1.5">
            <AlertDialogTitle className="break-words">
              {t('webShell.resetTitle', { title: props.title })}
            </AlertDialogTitle>
            <AlertDialogDescription className="text-pretty">
              {t('webShell.resetBody')}
            </AlertDialogDescription>
          </div>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{t('webCommon.cancel')}</AlertDialogCancel>
          <AlertDialogAction destructive onClick={props.onConfirm}>
            {t('webShell.resetConfirm')}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
