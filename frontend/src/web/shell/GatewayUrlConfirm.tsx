import { Globe } from 'lucide-react'
import { useState } from 'react'
import { useBootstrap, useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webConnection'
import { resolveWsUrl } from '@/lib/bootstrap'
import {
  clearPendingGatewayUrl,
  pendingGatewayUrlRequest,
  type GatewayUrlRequest,
} from '@/lib/credentials'
import { connectGateway } from './connection-gate'
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
 *
 * The token this tab holds belongs to the gateway it is using now, so it is
 * never sent to the link's host: the switch carries only a token that came in
 * the same link, or none, and the Connect screen asks for one if needed. The
 * new address is saved only once that gateway completes a handshake.
 */
export function GatewayUrlConfirm() {
  const rpc = useRpc()
  const bootstrap = useBootstrap()
  const [request] = useState<GatewayUrlRequest | null>(() => {
    const requested = pendingGatewayUrlRequest()
    if (!requested) return null
    if (!isTopLevel() || !hostOf(requested.url)) {
      clearPendingGatewayUrl()
      return null
    }
    return requested
  })
  const [open, setOpen] = useState(request !== null)
  if (!request) return null
  const { url } = request
  const host = hostOf(url)!

  const confirm = () => {
    clearPendingGatewayUrl()
    connectGateway(rpc, {
      url,
      token: request.token,
      defaultUrl: resolveWsUrl(bootstrap.ws_url),
      holdScreen: true,
    })
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
          <span
            aria-hidden
            className="mb-1 grid size-10 place-items-center rounded-card border border-warn/25 bg-warn/10 text-warn [&_svg]:size-[18px]"
          >
            <Globe />
          </span>
          <AlertDialogTitle>{t('webConnection.gatewayUrlTitle')}</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="flex flex-col gap-2">
              <p>{t('webConnection.gatewayUrlBody', { host })}</p>
              <code
                data-testid="gateway-url-target"
                className="rounded-md border border-border bg-surface-inset px-2.5 py-2 font-mono text-small break-all text-text shadow-inset"
              >
                {url}
              </code>
              <p>
                {request.token
                  ? t('webConnection.gatewayUrlLinkToken', { host })
                  : t('webConnection.gatewayUrlNoToken')}
              </p>
              <p>{t('webConnection.gatewayUrlSaved', { host })}</p>
            </div>
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
