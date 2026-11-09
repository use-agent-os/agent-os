import { useState } from 'react'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webConnection'
import {
  clearPendingGatewayUrl,
  pendingGatewayUrlRequest,
  readStoredToken,
  storeWsUrl,
} from '@/lib/credentials'
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

function hostOf(url: string): string | null {
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'ws:' || parsed.protocol === 'wss:' ? parsed.host : null
  } catch {
    return null
  }
}

function isTopLevel(): boolean {
  try {
    return window.top === window
  } catch {
    return false
  }
}

/**
 * Confirm a gateway URL that arrived in a link (connection spec "Changing the
 * gateway URL needs confirmation"). Never applied inside a frame.
 */
export function GatewayUrlConfirm() {
  const rpc = useRpc()
  const [url] = useState(() => {
    const requested = pendingGatewayUrlRequest()
    if (!requested) return null
    if (!isTopLevel() || !hostOf(requested)) {
      clearPendingGatewayUrl()
      return null
    }
    return requested
  })
  const [open, setOpen] = useState(url !== null)
  if (!url) return null
  const host = hostOf(url)!

  const confirm = () => {
    storeWsUrl(url)
    clearPendingGatewayUrl()
    rpc.connect(url, readStoredToken() || undefined)
  }
  const cancel = () => clearPendingGatewayUrl()

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) cancel()
        setOpen(next)
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('webConnection.gatewayUrlTitle')}</AlertDialogTitle>
          <AlertDialogDescription>
            {t('webConnection.gatewayUrlBody', { host })}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel onClick={cancel}>
            {t('webConnection.gatewayUrlCancel')}
          </AlertDialogCancel>
          <AlertDialogAction onClick={confirm}>
            {t('webConnection.gatewayUrlConfirm', { host })}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
