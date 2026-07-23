import { useEffect, useRef } from 'react'
import { isRouteErrorResponse, Link, useNavigate, useRouteError } from 'react-router'
import { LayoutDashboardIcon, RefreshCwIcon, TriangleAlertIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import './route-error.css'

const MAX_DEVELOPER_MESSAGE_LENGTH = 240

export interface RouteErrorCopy {
  code: string
  title: string
  message: string
  developerMessage?: string
}

/**
 * Converts an arbitrary router error into user-safe copy.
 *
 * Route response bodies are deliberately ignored because they can contain
 * backend diagnostics. In production, Error.message is also suppressed; the
 * boundary never renders an Error.stack in any environment.
 */
export function routeErrorCopy(
  error: unknown,
  showDeveloperMessage = import.meta.env.DEV,
): RouteErrorCopy {
  if (isRouteErrorResponse(error)) {
    const unavailable = error.status === 401 || error.status === 403 || error.status === 404
    return {
      code: `HTTP ${error.status}`,
      title: unavailable ? 'This view is unavailable' : 'This view hit a snag',
      message:
        'AgentOS kept the rest of your workspace intact. Reload this view or return to Overview.',
    }
  }

  const developerMessage =
    showDeveloperMessage && error instanceof Error && error.message.trim()
      ? error.message.trim().slice(0, MAX_DEVELOPER_MESSAGE_LENGTH)
      : undefined

  return {
    code: 'VIEW ERROR',
    title: 'This view hit a snag',
    message:
      'AgentOS kept the rest of your workspace intact. Reload this view or return to Overview.',
    developerMessage,
  }
}

export function RouteErrorBoundary() {
  const error = useRouteError()
  const navigate = useNavigate()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const copy = routeErrorCopy(error)

  useEffect(() => {
    document.title = 'Recovery - AgentOS Control'
    headingRef.current?.focus()
  }, [])

  return (
    <section className="route-error" aria-labelledby="route-error-title">
      <div className="route-error__card">
        <div className="route-error__header">
          <span className="route-error__icon" aria-hidden="true">
            <TriangleAlertIcon />
          </span>
          <div className="route-error__identity">
            <span className="route-error__eyebrow">Workspace recovery</span>
            <span className="route-error__status">
              <span aria-hidden="true" />
              View paused safely
            </span>
          </div>
          <code className="route-error__code">{copy.code}</code>
        </div>

        <div className="route-error__body">
          <h1 id="route-error-title" ref={headingRef} tabIndex={-1}>
            {copy.title}
          </h1>
          <p role="alert">{copy.message}</p>

          {copy.developerMessage ? (
            <div className="route-error__developer">
              <span>Developer detail</span>
              <code>{copy.developerMessage}</code>
            </div>
          ) : null}

          <div className="route-error__actions" aria-label="Recovery actions">
            <Button type="button" onClick={() => navigate(0)}>
              <RefreshCwIcon />
              Reload view
            </Button>
            <Button asChild variant="outline">
              <Link to="/overview">
                <LayoutDashboardIcon />
                Go to Overview
              </Link>
            </Button>
          </div>
        </div>
      </div>
    </section>
  )
}
