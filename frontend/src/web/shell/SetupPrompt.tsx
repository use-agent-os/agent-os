import { useQuery } from '@tanstack/react-query'
import { Sparkles, X } from 'lucide-react'
import { useState } from 'react'
import { Link, useLocation } from 'react-router'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSettings'
import { useConnection } from '@/stores/connection'
import { buttonVariants } from '@/web/components/ui/button'
import { ShellBar } from './ShellBar'

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
  const dismiss = () => {
    try {
      sessionStorage.setItem(DISMISS_KEY, '1')
    } catch {
      /* dismissed for this page only */
    }
    setHidden(true)
  }
  return (
    <ShellBar tone="accent" icon={<Sparkles />}>
      {/* Phones get one line: a short title and the action; the body stays for screen readers. */}
      <span className="min-w-0 flex-1">
        <span className="font-medium max-sm:hidden">{t('webSettings.setupPromptTitle')}</span>
        <span className="font-medium sm:hidden">{t('webSettings.setupPromptTitleShort')}</span>{' '}
        <span className="text-text-muted max-sm:sr-only">{t('webSettings.setupPromptBody')}</span>
      </span>
      <Link
        to="/settings/setup"
        viewTransition
        className={buttonVariants({ size: 'sm', variant: 'primary' })}
      >
        {t('webSettings.setupPromptAction')}
      </Link>
      <button
        type="button"
        aria-label={t('webSettings.setupPromptDismiss')}
        onClick={dismiss}
        className="press target inline-flex size-7 items-center justify-center rounded-md text-text-muted hover:bg-surface-3 hover:text-text touch:size-11"
      >
        <X className="size-4" aria-hidden />
      </button>
    </ShellBar>
  )
}
