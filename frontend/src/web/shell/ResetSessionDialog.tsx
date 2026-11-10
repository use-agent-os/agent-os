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
        <AlertDialogHeader>
          <AlertDialogTitle>{t('webShell.resetTitle', { title: props.title })}</AlertDialogTitle>
          <AlertDialogDescription>{t('webShell.resetBody')}</AlertDialogDescription>
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
