import { useQueryClient } from '@tanstack/react-query'
import { ArrowUpCircle, Loader2, RefreshCw, X } from 'lucide-react'
import { lazy, Suspense, useEffect, useState } from 'react'
import { Link } from 'react-router'
import { useBootstrap, useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webConnection'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { useConnection } from '@/stores/connection'
import { Button } from '@/web/components/ui/button'
import { useDismissedVersion, useUpdateCheck } from './updates'

const UpdateApplyDialog = lazy(async () => ({
  default: (await import('@/web/features/settings/updates/UpdateApplyDialog')).UpdateApplyDialog,
}))

export const RELOAD_GUARD_KEY = 'agentos.reloadGuard'
export const RELOAD_GUARD_MS = 60_000

/**
 * Reload at most once per bundle version within 60 s, so a protocol mismatch
 * that a reload does not fix can never loop.
 */
export function shouldAutoReload(bundleVersion: string, now: number = Date.now()): boolean {
  try {
    const raw = sessionStorage.getItem(RELOAD_GUARD_KEY)
    const guard = raw ? (JSON.parse(raw) as { bundleVersion?: string; ts?: number }) : null
    if (guard?.bundleVersion === bundleVersion && now - (guard.ts ?? 0) < RELOAD_GUARD_MS) {
      return false
    }
    sessionStorage.setItem(RELOAD_GUARD_KEY, JSON.stringify({ bundleVersion, ts: now }))
    return true
  } catch {
    return false
  }
}

function Banner({ children, tone }: { children: React.ReactNode; tone: 'info' | 'warn' }) {
  return (
    <div
      role="status"
      className={
        tone === 'warn'
          ? 'flex items-center gap-3 border-b border-warn/40 bg-warn/10 px-4 py-2'
          : 'flex items-center gap-3 border-b border-info/40 bg-info/10 px-4 py-2'
      }
    >
      {children}
    </div>
  )
}

/** "Restarting gateway…", protocol-mismatch Reload, and the update notice. */
export function ConnectionNotices({
  reload = () => window.location.reload(),
}: {
  reload?: () => void
}) {
  const rpc = useRpc()
  const bootstrap = useBootstrap()
  const queryClient = useQueryClient()
  const state = useConnection((s) => s.state)
  const shutdown = useRuntimeSignals((s) => s.shutdown)
  const handshake = useRuntimeSignals((s) => s.handshake)
  const update = useUpdateCheck()
  const [dismissed, dismiss] = useDismissedVersion()
  const [applyOpen, setApplyOpen] = useState(false)
  const [restartPending, setRestartPending] = useState(false)

  const restarting = state !== 'connected' && !!shutdown?.restartExpectedMs
  useEffect(
    () =>
      useRuntimeSignals.subscribe((s, prev) => {
        if (s.shutdown !== prev.shutdown && s.shutdown?.restartExpectedMs) setRestartPending(true)
      }),
    [],
  )

  // Back after an expected restart: refresh what the open view shows.
  useEffect(
    () =>
      rpc.on('_hello', () => {
        setRestartPending((was) => {
          if (was) void queryClient.invalidateQueries()
          return false
        })
      }),
    [rpc, queryClient],
  )

  const mismatch = handshake?.kind === 'protocol_mismatch'
  useEffect(() => {
    if (mismatch && handshake?.reloadRequired && shouldAutoReload(bootstrap.version)) reload()
  }, [mismatch, handshake?.reloadRequired, bootstrap.version, reload])

  const latest = update.check?.latest ?? ''
  const showUpdate = update.available && dismissed !== latest

  return (
    <>
      {restarting || (restartPending && state !== 'connected') ? (
        <Banner tone="info">
          <Loader2 className="motion-status size-4 animate-spin" aria-hidden />
          <span className="font-medium">{t('webConnection.restarting')}</span>
          <span className="text-text-muted">{t('webConnection.restartingBody')}</span>
        </Banner>
      ) : null}
      {mismatch ? (
        <Banner tone="warn">
          <span className="min-w-0 flex-1">
            <span className="font-medium">{t('webConnection.protocolTitle')}</span>{' '}
            <span className="text-text-muted">{t('webConnection.protocolBody')}</span>
          </span>
          <Button size="sm" variant="primary" onClick={reload}>
            <RefreshCw aria-hidden />
            {t('webConnection.protocolReload')}
          </Button>
        </Banner>
      ) : null}
      {showUpdate ? (
        <Banner tone="info">
          <ArrowUpCircle className="size-4 text-info" aria-hidden />
          <span className="min-w-0 flex-1">
            {t('webConnection.updateNotice', { version: latest })}{' '}
            <Link to="/settings/updates" className="text-accent underline-offset-2 hover:underline">
              {t('webConnection.updateNoticeLink')}
            </Link>
          </span>
          <Button size="sm" variant="outline" onClick={() => setApplyOpen(true)}>
            {t('webConnection.updateNow')}
          </Button>
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={t('webConnection.updateDismiss')}
            onClick={() => dismiss(latest)}
          >
            <X />
          </Button>
        </Banner>
      ) : null}
      {applyOpen ? (
        <Suspense fallback={null}>
          <UpdateApplyDialog version={latest} open={applyOpen} onOpenChange={setApplyOpen} />
        </Suspense>
      ) : null}
    </>
  )
}
