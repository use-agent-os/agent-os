import '../capabilities.css'
import { AudioLines, Globe, ImageIcon, LoaderCircle, AtSign } from 'lucide-react'
import { useEffect, type ReactNode } from 'react'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { CapabilityCard, STATE_TONE } from '../capabilities/CapabilityCard'
import { useKeyFacts } from '../capabilities/key-context'
import {
  capabilityStatus,
  imageDef,
  searchDef,
  shortLabel,
  voiceDef,
  xSearchDef,
  type CapabilityDef,
  type CapStatus,
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

/**
 * Smooth, unless the Mac asks for reduced motion. Also at once while the
 * window is hidden (covered, on another Space): a smooth scroll runs on
 * animation frames, which a hidden window does not get, so it would never
 * arrive.
 */
export function scrollBehavior(): ScrollBehavior {
  if (document.visibilityState === 'hidden') return 'instant'
  const reduce =
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  return reduce ? 'instant' : 'smooth'
}

/** Bring a card into view and say "here" (the stats strip, a jump from elsewhere). */
function scrollToCard(id: string) {
  const el = document.getElementById(ANCHOR[id] ?? id)
  if (!el) return
  el.scrollIntoView({ block: 'start', behavior: scrollBehavior() })
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
  const { ctx, envUrl } = useKeyFacts(true)

  // Arrived from the Overview's "Set up image generation": land on that card.
  useEffect(() => {
    const id = takeFocus('capabilities')
    if (!id) return
    // After the sheet's own scroll-to-top for the new section. A timer, not
    // a frame: frames pause while the window is covered.
    const timer = window.setTimeout(() => scrollToCard(id), 30)
    return () => window.clearTimeout(timer)
  }, [])

  const status = {
    search: capabilityStatus(searchDef, snapshot, ctx),
    x: capabilityStatus(xSearchDef, snapshot, ctx),
    image: capabilityStatus(imageDef, snapshot, ctx),
    voice: capabilityStatus(voiceDef, snapshot, ctx),
  }
  const hasX = xSearchDef.specs(snapshot).length > 0
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
        {hasX ? (
          <CapStat
            label={t('settings.capabilities.stat.x')}
            status={status.x}
            name={t('settings.capabilities.stat.x.value')}
            icon={<AtSign aria-hidden />}
            onClick={() => scrollToCard('x')}
          />
        ) : null}
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
      {hasX
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
  name,
  icon,
  onClick,
}: {
  label: string
  status: CapStatus
  /** The figure when on, instead of the service's short name. */
  name?: string
  icon: ReactNode
  onClick: () => void
}) {
  const value =
    status.state === 'off'
      ? t('settings.capabilities.stat.off')
      : (name ?? shortLabel(status.providerLabel))
  const hint =
    status.state === 'off'
      ? t('settings.capabilities.stat.offHint')
      : status.state === 'key'
        ? t('settings.capabilities.stat.keyMissing')
        : status.source === 'none'
          ? t('settings.capabilities.stat.noKey')
          : status.source === 'login'
            ? t('settings.capabilities.stat.login')
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
