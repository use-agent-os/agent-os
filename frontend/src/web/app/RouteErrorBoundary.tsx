import { AlertTriangle } from 'lucide-react'
import { isRouteErrorResponse, useRouteError } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { Button } from '@/web/components/ui/button'
import { EmptyState } from '@/web/components/ui/states'

/** A route that threw: name the failure and offer a reload, inside the shell. */
export function RouteErrorBoundary() {
  const error = useRouteError()
  const detail = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : String(error)
  return (
    <div
      role="alert"
      className="bg-atmosphere flex h-full min-h-dvh items-center justify-center p-4"
    >
      <EmptyState
        icon={<AlertTriangle className="text-danger" data-status-icon />}
        title={t('webShell.routeErrorTitle')}
        description={detail}
        action={
          <Button variant="primary" onClick={() => window.location.reload()}>
            {t('webShell.routeErrorReload')}
          </Button>
        }
      />
    </div>
  )
}
