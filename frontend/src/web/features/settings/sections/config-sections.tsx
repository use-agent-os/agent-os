import { useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Loader2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSetConfig'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import {
  audioStatusText,
  imageGenerationStatusText,
  providerRouterSupportText,
  searchStatusText,
  xSearchCredentialText,
  xSearchSignedIn,
  type AuthStatus,
  type XaiLoginPoll,
  type XaiPendingLogin,
} from '@/views/setup/logic'
import { Badge } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { useRegisterFields } from '../fields'
import { Notice, SettingsCard } from '../ui'
import {
  audioAdapter,
  imageAdapter,
  providerAdapter,
  searchAdapter,
  xSearchAdapter,
  xSearchParams,
} from './onboarding-adapters'
import { RouterForm, SnapshotGate, SpecForm } from './onboarding-forms'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export const AUTH_STATUS_KEY = ['auth.status'] as const

function useAuthStatus() {
  const rpc = useRpc()
  return useQuery({
    queryKey: AUTH_STATUS_KEY,
    queryFn: async () => (await rpc.call<AuthStatus>('auth.status')) ?? {},
  })
}

function Logins() {
  const auth = useAuthStatus()
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
    <SettingsCard
      aria-label={t('webSetConfig.logins')}
      title={t('webSetConfig.logins')}
      icon={<KeyRound />}
    >
      {auth.isPending ? (
        <LoadingRegion className="mt-3 flex flex-col gap-2">
          <Skeleton className="h-10 rounded-card" />
        </LoadingRegion>
      ) : entries.length === 0 ? (
        <p className="mt-2 text-small text-text-muted">{t('webSetConfig.loginsNone')}</p>
      ) : (
        <ul className="set-list mt-3 shadow-none">
          {entries.map(([name, value]) => (
            <li key={name} className="flex items-center justify-between gap-3 px-3.5 py-2.5">
              <span className="font-mono text-small">{name}</span>
              <Badge tone={value.signedIn ? 'ok' : 'neutral'} dot>
                {value.signedIn
                  ? t('webSetConfig.loginSignedIn')
                  : t('webSetConfig.loginSignedOut')}
              </Badge>
            </li>
          ))}
        </ul>
      )}
    </SettingsCard>
  )
}

/** The badge next to a provider: can the Pilot Router route it, or direct only. */
function RouterSupport({ supported }: { supported: string }) {
  return (
    <p className="flex flex-wrap items-center gap-2">
      <span className="text-text-muted">{t('webSetConfig.routerSupport')}</span>{' '}
      <Badge tone="accent" className="font-medium">
        {supported}
      </Badge>
    </p>
  )
}

function ProviderForm({ snapshot }: { snapshot: SettingsSnapshot }) {
  const status = snapshot.status ?? {}
  const envKey = snapshot.config?.llm?.api_key_env || String(status.llmEnvKey ?? '')
  return (
    <SpecForm
      title={t('webSetConfig.provider')}
      adapter={providerAdapter}
      snapshot={snapshot}
      recovery="llm"
      aside={(spec) => <RouterSupport supported={providerRouterSupportText(spec)} />}
    >
      {status.llmSource === 'missing_env' ? (
        <Notice role="status" tone="warn" className="mb-1">
          {t('webSetConfig.providerEnvMissing', { key: envKey || t('webSetConfig.envUnnamed') })}
        </Notice>
      ) : null}
    </SpecForm>
  )
}

/** Settings › Models & routing: provider, Pilot Router tiers, subscription logins. */
export function ModelsSection() {
  useRegisterFields('models', [
    { id: 'llm.provider', label: t('webSetConfig.provider') },
    { id: 'llm.model', label: t('webSetConfig.fieldModel') },
    { id: 'agentos_router.strategy', label: t('webSetConfig.routerMode') },
    { id: 'agentos_router.default_tier', label: t('webSetConfig.defaultTier') },
    { id: 'agentos_router.translate_ceiling_tier', label: t('webSetConfig.translateCap') },
    {
      id: 'agentos_router.tiers.image_model.model',
      label: t('webSetConfig.tierModel', { tier: 'image_model' }),
    },
  ])
  return (
    <SnapshotGate>
      {(snapshot) => (
        <div className="flex flex-col gap-4">
          <ProviderForm snapshot={snapshot} />
          <RouterForm snapshot={snapshot} />
          <Logins />
        </div>
      )}
    </SnapshotGate>
  )
}

const DEFAULT_POLL_SECONDS = 5

/** Seconds to wait before the next poll. `0` is a real answer, not a missing one. */
function pollDelay(interval: number | undefined, fallback: number): number {
  return typeof interval === 'number' && interval >= 0 ? interval : fallback
}

/** The xAI device-code login: Sign in when signed out, Sign out when signed in. */
function XaiLogin({ snapshot }: { snapshot: SettingsSnapshot }) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const auth = useAuthStatus()
  const [pending, setPending] = useState<XaiPendingLogin | null>(null)
  const [error, setError] = useState<string | null>(null)
  // A poll in flight carries the flow it belongs to; Cancel or unmount ends it.
  const flow = useRef(0)
  useEffect(() => {
    const id = flow
    return () => {
      id.current += 1
    }
  }, [])
  const signedIn = xSearchSignedIn(auth.data ?? {})
  const refresh = () => queryClient.invalidateQueries({ queryKey: AUTH_STATUS_KEY })

  const poll = async (login: XaiPendingLogin, wait: number, mine: number): Promise<void> => {
    await new Promise((r) => window.setTimeout(r, wait * 1000))
    if (mine !== flow.current) return
    const result = await rpc.call<XaiLoginPoll>('auth.xai.login.poll', { loginId: login.loginId })
    if (mine !== flow.current) return
    if (result?.status === 'complete') {
      setPending(null)
      await refresh()
      return
    }
    if (result?.status === 'expired') throw new Error(t('webSetConfig.xaiExpired'))
    return poll(login, pollDelay(result?.interval, login.interval), mine)
  }
  const start = () => {
    const mine = ++flow.current
    setError(null)
    void rpc
      .call<XaiPendingLogin>('auth.xai.login.start')
      .then((login) => {
        if (mine !== flow.current || !login?.loginId) return
        setPending(login)
        return poll(login, pollDelay(login.interval, DEFAULT_POLL_SECONDS), mine)
      })
      .catch((e: unknown) => {
        if (mine !== flow.current) return
        setPending(null)
        setError(errorMessage(e))
      })
  }
  const signOut = () => {
    setError(null)
    void rpc
      .call('auth.xai.logout', {})
      .then(() => refresh())
      .catch((e: unknown) => setError(errorMessage(e)))
  }
  return (
    <div className="mb-1 flex flex-col gap-2.5 rounded-control bg-surface-2 px-3 py-2.5">
      <p className="text-small text-text-muted" data-testid="x-search-credential">
        {xSearchCredentialText(auth.data ?? {}, snapshot.config ?? {})}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        {pending ? (
          <>
            <Loader2 className="size-3.5 animate-spin text-text-muted" aria-hidden />
            <span className="text-small">
              {t('webSetConfig.xaiCode', { url: pending.verificationUri, code: pending.userCode })}
            </span>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                flow.current += 1
                setPending(null)
              }}
            >
              {t('webSetConfig.xaiCancel')}
            </Button>
          </>
        ) : signedIn ? (
          <Button size="sm" variant="outline" onClick={signOut}>
            {t('webSetConfig.xaiLogout')}
          </Button>
        ) : (
          <Button size="sm" variant="outline" onClick={start}>
            {t('webSetConfig.xaiLogin')}
          </Button>
        )}
        {error ? (
          <span role="alert" className="anim-fade-in text-small text-danger">
            {error}
          </span>
        ) : null}
      </div>
    </div>
  )
}

/** Settings › Search: web search and X search. */
export function SearchSection() {
  useRegisterFields('search', [
    { id: 'search_provider', label: t('webSetConfig.webSearch') },
    { id: 'search_max_results', label: t('webSetConfig.searchMaxResults') },
    { id: 'x_search.enabled', label: t('webSetConfig.xSearchEnabled') },
    { id: 'x_search.model', label: t('webSetConfig.xSearchModel') },
  ])
  return (
    <SnapshotGate>
      {(snapshot) => (
        <div className="flex flex-col gap-4">
          <SpecForm
            title={t('webSetConfig.webSearch')}
            adapter={searchAdapter}
            snapshot={snapshot}
            status={searchStatusText(snapshot.status ?? {}, snapshot.config ?? {})}
            recovery="search"
          />
          <SpecForm
            title={t('webSetConfig.xSearch')}
            adapter={xSearchAdapter}
            snapshot={snapshot}
            buildParams={(_id, values, fields) => xSearchParams(values, fields)}
          >
            <XaiLogin snapshot={snapshot} />
          </SpecForm>
        </div>
      )}
    </SnapshotGate>
  )
}

/** Settings › Media: image generation and audio. */
export function MediaSection() {
  useRegisterFields('media', [
    { id: 'image_generation.enabled', label: t('webSetConfig.image') },
    { id: 'image_generation.primary', label: t('webSetConfig.imagePrimary') },
    { id: 'audio.enabled', label: t('webSetConfig.audio') },
  ])
  return (
    <SnapshotGate>
      {(snapshot) => (
        <div className="flex flex-col gap-4">
          <SpecForm
            title={t('webSetConfig.image')}
            adapter={imageAdapter}
            snapshot={snapshot}
            status={imageGenerationStatusText(snapshot.status ?? {})}
            recovery="image_generation"
          />
          <SpecForm
            title={t('webSetConfig.audio')}
            adapter={audioAdapter}
            snapshot={snapshot}
            status={audioStatusText(snapshot.status ?? {})}
            recovery="audio"
          />
        </div>
      )}
    </SnapshotGate>
  )
}
