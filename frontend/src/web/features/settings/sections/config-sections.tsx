import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSetConfig'
import type { AuthStatus, XaiLoginPoll, XaiPendingLogin } from '@/views/setup/logic'
import { Button } from '@/web/components/ui/button'
import { useRegisterFields } from '../fields'
import { RouterForm, SnapshotGate, SpecForm } from './onboarding-forms'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function Logins() {
  const rpc = useRpc()
  const auth = useQuery({
    queryKey: ['auth.status'],
    queryFn: () => rpc.call<AuthStatus & Record<string, unknown>>('auth.status'),
  })
  const entries = Object.entries(auth.data ?? {})
    .filter(
      ([, v]) =>
        v && typeof v === 'object' && ('loggedIn' in (v as object) || 'logged_in' in (v as object)),
    )
    .map(([name, v]) => {
      const value = v as { loggedIn?: boolean; logged_in?: boolean }
      return [name, { signedIn: Boolean(value.loggedIn ?? value.logged_in) }] as const
    })
  return (
    <section aria-label={t('webSetConfig.logins')} className="rounded-lg border border-border p-4">
      <h2 className="mb-2 font-semibold">{t('webSetConfig.logins')}</h2>
      {entries.length === 0 ? (
        <p className="text-text-muted">{t('webSetConfig.loginsNone')}</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {entries.map(([name, value]) => (
            <li key={name} className="flex justify-between">
              <span>{name}</span>
              <span className="text-text-muted">
                {value.signedIn
                  ? t('webSetConfig.loginSignedIn')
                  : t('webSetConfig.loginSignedOut')}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/** Settings › Models & routing: provider, Pilot Router tiers, subscription logins. */
export function ModelsSection() {
  useRegisterFields('models', [
    { id: 'llm.provider', label: t('webSetConfig.provider') },
    { id: 'agentos_router.strategy', label: t('webSetConfig.routerMode') },
    { id: 'agentos_router.default_tier', label: t('webSetConfig.defaultTier') },
  ])
  return (
    <SnapshotGate>
      {(snapshot) => (
        <div className="flex flex-col gap-4">
          <SpecForm
            title={t('webSetConfig.provider')}
            specs={snapshot.catalog?.providers ?? []}
            current={snapshot.config?.llm?.provider ?? ''}
            method="onboarding.provider.configure"
            revision={snapshot.revision}
          />
          <RouterForm snapshot={snapshot} />
          <Logins />
        </div>
      )}
    </SnapshotGate>
  )
}

function XaiLogin() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const [pending, setPending] = useState<{ url?: string; code?: string } | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Device-code login: poll until the user finished in the browser tab.
  const poll = async (login: XaiPendingLogin) => {
    for (;;) {
      await new Promise((r) => window.setTimeout(r, Math.max(1, login.interval || 5) * 1000))
      const result = await rpc.call<XaiLoginPoll>('auth.xai.login.poll', { loginId: login.loginId })
      if (result?.status === 'complete') {
        setPending(null)
        await queryClient.invalidateQueries({ queryKey: ['auth.status'] })
        return
      }
      if (result?.status === 'expired') throw new Error('expired')
    }
  }
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      <Button
        size="sm"
        variant="outline"
        onClick={() =>
          void rpc
            .call<XaiPendingLogin>('auth.xai.login.start')
            .then((login) => {
              setPending({ url: login?.verificationUri, code: login?.userCode })
              return login?.loginId ? poll(login) : undefined
            })
            .catch((e: unknown) => {
              setPending(null)
              setError(errorMessage(e))
            })
        }
      >
        {t('webSetConfig.xaiLogin')}
      </Button>
      <Button
        size="sm"
        variant="ghost"
        onClick={() =>
          void rpc.call('auth.xai.logout', {}).catch((e: unknown) => setError(errorMessage(e)))
        }
      >
        {t('webSetConfig.xaiLogout')}
      </Button>
      {pending?.url ? (
        <span className="text-small">
          {t('webSetConfig.xaiCode', { url: pending.url, code: pending.code ?? '' })}
        </span>
      ) : null}
      {error ? (
        <span role="alert" className="text-small text-danger">
          {error}
        </span>
      ) : null}
    </div>
  )
}

/** Settings › Search: web search and X search. */
export function SearchSection() {
  useRegisterFields('search', [
    { id: 'search_provider', label: t('webSetConfig.webSearch') },
    { id: 'x_search.enabled', label: t('webSetConfig.xSearchEnabled') },
  ])
  return (
    <SnapshotGate>
      {(snapshot) => (
        <div className="flex flex-col gap-4">
          <SpecForm
            title={t('webSetConfig.webSearch')}
            specs={snapshot.catalog?.searchProviders ?? []}
            current={snapshot.config?.search_provider ?? ''}
            method="onboarding.search.configure"
            revision={snapshot.revision}
          />
          <div>
            <SpecForm
              title={t('webSetConfig.xSearch')}
              specs={snapshot.catalog?.xSearch ?? []}
              current=""
              method="onboarding.x_search.configure"
              revision={snapshot.revision}
            />
            <XaiLogin />
          </div>
        </div>
      )}
    </SnapshotGate>
  )
}

/** Settings › Media: image generation and audio. */
export function MediaSection() {
  useRegisterFields('media', [
    { id: 'image_generation', label: t('webSetConfig.image') },
    { id: 'audio', label: t('webSetConfig.audio') },
  ])
  return (
    <SnapshotGate>
      {(snapshot) => (
        <div className="flex flex-col gap-4">
          <SpecForm
            title={t('webSetConfig.image')}
            specs={snapshot.catalog?.imageGenerationProviders ?? []}
            current=""
            method="onboarding.imageGeneration.configure"
            revision={snapshot.revision}
          />
          <SpecForm
            title={t('webSetConfig.audio')}
            specs={snapshot.catalog?.audioProviders ?? []}
            current=""
            method="onboarding.audio.configure"
            revision={snapshot.revision}
          />
        </div>
      )}
    </SnapshotGate>
  )
}
