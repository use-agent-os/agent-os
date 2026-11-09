import { useQuery } from '@tanstack/react-query'
import { Sparkles, X } from 'lucide-react'
import { useState } from 'react'
import { Link, useLocation } from 'react-router'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSettings'
import { useConnection } from '@/stores/connection'
import { buttonVariants } from '@/web/components/ui/button'

const DISMISS_KEY = 'agentos.ui.setupPromptDismissed'

function dismissed(): boolean {
  try {
    return sessionStorage.getItem(DISMISS_KEY) === '1'
  } catch {
    return false
  }
}

/**
 * The first-run prompt (settings spec "Guided setup flow"): persistent while
 * onboarding is incomplete, dismissible for the tab, linking to the stepper.
 */
export function SetupPrompt() {
  const rpc = useRpc()
  const connected = useConnection((s) => s.state === 'connected')
  const { pathname } = useLocation()
  const [hidden, setHidden] = useState(dismissed)
  const status = useQuery({
    queryKey: ['onboarding.status'],
    queryFn: () =>
      rpc.call<{ needsOnboarding?: boolean; llmConfigured?: boolean }>('onboarding.status'),
    enabled: connected && !hidden,
    staleTime: 60_000,
    retry: false,
  })
  const incomplete = status.data?.needsOnboarding === true || status.data?.llmConfigured === false
  if (hidden || !incomplete || pathname.startsWith('/settings/setup')) return null
  return (
    <div
      role="status"
      className="flex items-center gap-3 border-b border-accent/40 bg-accent-soft px-4 py-2 max-sm:gap-2 max-sm:py-1.5"
    >
      <Sparkles className="size-4 shrink-0 text-accent" aria-hidden />
      {/* Phones get one line: a short title and the action; the body stays for screen readers. */}
      <span className="min-w-0 flex-1">
        <span className="font-medium max-sm:hidden">{t('webSettings.setupPromptTitle')}</span>
        <span className="font-medium sm:hidden">{t('webSettings.setupPromptTitleShort')}</span>{' '}
        <span className="text-text-muted max-sm:sr-only">{t('webSettings.setupPromptBody')}</span>
      </span>
      <Link to="/settings/setup" className={buttonVariants({ size: 'sm', variant: 'primary' })}>
        {t('webSettings.setupPromptAction')}
      </Link>
      <button
        type="button"
        aria-label={t('webSettings.setupPromptDismiss')}
        onClick={() => {
          try {
            sessionStorage.setItem(DISMISS_KEY, '1')
          } catch {
            /* dismissed for this page only */
          }
          setHidden(true)
        }}
        className="target inline-flex items-center justify-center rounded text-text-muted hover:text-text"
      >
        <X className="size-4" aria-hidden />
      </button>
    </div>
  )
}
