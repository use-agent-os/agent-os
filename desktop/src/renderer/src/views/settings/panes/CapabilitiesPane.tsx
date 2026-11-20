import '../capabilities.css'
import { useQuery } from '@tanstack/react-query'
import { AudioLines, Globe, ImageIcon, LoaderCircle, AtSign } from 'lucide-react'
import { useEffect, useMemo, type ReactNode } from 'react'
import { useRpc } from '@/app/providers'
import { ENV_QUERY_KEY, type EnvListResponse } from '@/views/env/logic'
import { xSearchSignedIn, type AuthStatus } from '@/views/setup/logic'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { CapabilityCard, STATE_TONE } from '../capabilities/CapabilityCard'
import {
  capabilityStatus,
  imageDef,
  searchDef,
  shortLabel,
  voiceDef,
  xSearchDef,
  type CapabilityDef,
  type CapStatus,
  type KeyContext,
} from '../capabilities/logic'
import { takeFocus } from '../overview/jumps'
import { Head, Notice, Stat, Stats } from '../parts'
import { PendingRestartNotice } from '../PendingRestartNotice'
import { useConfigSnapshot } from '../use-snapshot'

const ANCHOR: Record<string, string> = {
  search: 'cap-search',
  x: 'cap-x',
  image: 'cap-image',
  voice: 'cap-voice',
}

/** Smooth within the page (the stats strip); instant when arriving from elsewhere. */
function scrollToCard(id: string, behavior: ScrollBehavior = 'smooth') {
  const el = document.getElementById(ANCHOR[id] ?? id)
  if (!el) return
  el.scrollIntoView({ block: 'start', behavior })
  el.dataset.flash = 'true'
  window.setTimeout(() => delete el.dataset.flash, 1200)
}

/**
 * Settings › Capabilities: the services the agent reaches for besides its
 * model. A strip says what is on; one card per capability picks the service
 * and its key, saved through the gateway's guided writes so the console and
 * the CLI read the same result.
 */
export function CapabilitiesPane() {
  const { connected, query, snapshot, reload } = useConfigSnapshot()
  return (
    <>
      <Head
        title={t('settings.section.capabilities')}
        blurb={t('settings.section.capabilities.blurb')}
      />
      {!connected ? (
        <Notice tone="info">{t('settings.offline')}</Notice>
      ) : query.isError ? (
        <Notice
          tone="danger"
          action={
            <Button disabled={query.isFetching} onClick={() => void query.refetch()}>
              {t('settings.capabilities.retry')}
            </Button>
          }
        >
          {t('settings.loadFailed')}
        </Notice>
      ) : !snapshot ? (
        <div className="cap-loading">
          <LoaderCircle className="stg-spin size-3.5" strokeWidth={1.75} aria-hidden />
          {t('settings.loading')}
        </div>
      ) : (
        <CapabilitiesBody snapshot={snapshot} reload={reload} />
      )}
    </>
  )
}

function CapabilitiesBody({
  snapshot,
  reload,
}: {
  snapshot: SettingsSnapshot
  reload: () => Promise<void>
}) {
  const rpc = useRpc()
  const env = useQuery({
    queryKey: ENV_QUERY_KEY,
    refetchOnWindowFocus: false,
    queryFn: () => rpc.call<EnvListResponse>('env.list', {}),
  })
  const auth = useQuery({
    queryKey: ['settings', 'auth', 'status'],
    refetchOnWindowFocus: false,
    retry: false,
    queryFn: () => rpc.call<AuthStatus>('auth.status', {}),
  })

  const rows = env.data?.vars
  const ctx: KeyContext = useMemo(() => {
    const byName = new Map((rows ?? []).map((r) => [r.name, r]))
    return {
      // Unknown until the listing lands (or when it failed): never block on a guess.
      envSet: (name: string) => (rows ? Boolean(byName.get(name)?.isSet) : undefined),
      xaiLogin: auth.data ? xSearchSignedIn(auth.data) : false,
    }
  }, [rows, auth.data])
  const envUrl = (name: string) => rows?.find((r) => r.name === name)?.url || undefined

  // Arrived from the Overview's "Set up image generation": land on that card.
  useEffect(() => {
    const id = takeFocus('capabilities')
    if (!id) return
    // After the sheet's own scroll-to-top for the new section. A timer, not
    // a frame: frames pause while the window is covered.
    const timer = window.setTimeout(() => scrollToCard(id, 'instant'), 30)
    return () => window.clearTimeout(timer)
  }, [])

  const status = {
    search: capabilityStatus(searchDef, snapshot, ctx),
    x: capabilityStatus(xSearchDef, snapshot, ctx),
    image: capabilityStatus(imageDef, snapshot, ctx),
    voice: capabilityStatus(voiceDef, snapshot, ctx),
  }
  const blocked = Boolean(snapshot.writeBlocked)
  const revision = snapshot.revision ?? ''

  const card = (
    def: CapabilityDef,
    icon: ReactNode,
    title: string,
    blurb: string,
    offText?: string,
    aside?: ReactNode,
  ) => (
    <div id={ANCHOR[def.id]} className="cap-anchor" key={def.id}>
      <CapabilityCard
        key={`${def.id}:${revision}`}
        def={def}
        snapshot={snapshot}
        ctx={ctx}
        envUrl={envUrl}
        icon={icon}
        title={title}
        blurb={blurb}
        offText={offText}
        status={status[def.id]}
        disabled={blocked}
        onSaved={reload}
        aside={aside}
      />
    </div>
  )

  return (
    <>
      {blocked ? (
        <Notice tone="danger">{t('settings.writeBlocked')}</Notice>
      ) : snapshot.pendingRestart ? (
        <PendingRestartNotice />
      ) : null}

      <Stats label={t('settings.capabilities.stats')}>
        <CapStat
          label={t('settings.capabilities.stat.search')}
          status={status.search}
          icon={<Globe aria-hidden />}
          onClick={() => scrollToCard('search')}
        />
        <CapStat
          label={t('settings.capabilities.stat.image')}
          status={status.image}
          icon={<ImageIcon aria-hidden />}
          onClick={() => scrollToCard('image')}
        />
        <CapStat
          label={t('settings.capabilities.stat.voice')}
          status={status.voice}
          icon={<AudioLines aria-hidden />}
          onClick={() => scrollToCard('voice')}
        />
      </Stats>

      {card(
        searchDef,
        <Globe strokeWidth={1.75} aria-hidden />,
        t('settings.capabilities.search.title'),
        t('settings.capabilities.search.blurb'),
      )}
      {card(
        imageDef,
        <ImageIcon strokeWidth={1.75} aria-hidden />,
        t('settings.capabilities.image.title'),
        t('settings.capabilities.image.blurb'),
        t('settings.capabilities.image.off'),
      )}
      {card(
        voiceDef,
        <AudioLines strokeWidth={1.75} aria-hidden />,
        t('settings.capabilities.voice.title'),
        t('settings.capabilities.voice.blurb'),
        t('settings.capabilities.voice.off'),
      )}
      {xSearchDef.specs(snapshot).length > 0
        ? card(
            xSearchDef,
            <AtSign strokeWidth={1.75} aria-hidden />,
            t('settings.capabilities.x.title'),
            t('settings.capabilities.x.blurb'),
            t('settings.capabilities.x.off'),
            ctx.xaiLogin ? null : (
              <span className="cap-hint">
                {t('settings.capabilities.x.loginHint')} <code>agentos auth login xai</code>
              </span>
            ),
          )
        : null}
    </>
  )
}

function CapStat({
  label,
  status,
  icon,
  onClick,
}: {
  label: string
  status: CapStatus
  icon: ReactNode
  onClick: () => void
}) {
  const value =
    status.state === 'off' ? t('settings.capabilities.stat.off') : shortLabel(status.providerLabel)
  const hint =
    status.state === 'off'
      ? t('settings.capabilities.stat.offHint')
      : status.state === 'key'
        ? t('settings.capabilities.stat.keyMissing')
        : status.source === 'none'
          ? t('settings.capabilities.stat.noKey')
          : t('settings.capabilities.stat.keyReady')
  return (
    <Stat
      label={label}
      value={value}
      hint={hint}
      tone={STATE_TONE[status.state]}
      icon={icon}
      onClick={onClick}
    />
  )
}
