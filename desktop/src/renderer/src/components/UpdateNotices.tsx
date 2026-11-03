import { useEffect, useRef } from 'react'
import { toast } from 'sonner'
import { releaseUpdate, type ReleaseUpdate } from '@shared/updates'
import { t } from '~/i18n'
import { useGatewayStatus } from '~/lib/use-gateway-status'
import { useGateway } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import { useUi } from '~/stores/ui'
import { useUpdates } from '~/stores/updates'
import { useSessions } from '~/stores/sessions'

const RELEASE_TOAST = 'agentos-release'

export interface NoticeActions {
  /** Install the engine and download the app, whichever the release needs. */
  update(): void
  /** Relaunch on the downloaded app. */
  restart(): void
  /** Restart the managed gateway onto the engine already on disk. */
  restartGateway(): void
  /** After a failed download or restart: check and download again. */
  retry(): void
  /** An external gateway is behind: show About, there is nothing to run. */
  details(): void
}

/**
 * The ambient checks (launch, focus, the 5-minute tick) have no pane open to
 * report into, so a release surfaces as one toast with the next step on it:
 * Update while `available`, Restart once the app has landed, Restart gateway
 * when a terminal upgraded the engine under a running gateway. One toast id,
 * so a later step replaces the earlier card rather than stacking; the same
 * step for the same release is not announced twice; a finished engine-only
 * update says so once and goes away.
 */
export function announceRelease(
  prev: ReleaseUpdate,
  next: ReleaseUpdate,
  act: NoticeActions,
): void {
  if (sameStep(prev, next)) return
  switch (next.kind) {
    case 'available':
      // sonner merges options into the card that already carries this id, so
      // every step states its description outright (null clears the last one).
      toast(`AgentOS ${next.version} ${t('updates.toast.available')}`, {
        id: RELEASE_TOAST,
        duration: Infinity,
        description: next.engine && next.app ? t('updates.toast.both') : null,
        action: { label: t('updates.toast.update'), onClick: act.update },
      })
      return
    case 'restart': {
      const blocked = next.blocked
        ? ` ${t('updates.toast.blocked')} ${t(`settings.about.app.blocked.${next.blocked}`)}`
        : ''
      toast.success(`AgentOS ${next.version} ${t('updates.toast.downloaded')}${blocked}`, {
        id: RELEASE_TOAST,
        duration: Infinity,
        description: null,
        action: { label: t('updates.toast.restart'), onClick: act.restart },
      })
      return
    }
    case 'gateway-restart':
      toast(`${t('updates.toast.engine')} ${next.version} ${t('updates.toast.engineInstalled')}`, {
        id: RELEASE_TOAST,
        duration: Infinity,
        description: `${t('updates.toast.gatewayRuns')} ${next.running}.`,
        action: { label: t('updates.toast.restartGateway'), onClick: act.restartGateway },
      })
      return
    case 'gateway-outdated':
      toast(`AgentOS ${next.version} ${t('updates.toast.available')}`, {
        id: RELEASE_TOAST,
        duration: Infinity,
        description: `${t('updates.toast.gatewayBehind')} ${next.running} ${t('updates.toast.gatewayBehind.rest')}`,
        action: { label: t('updates.toast.gatewayBehind.open'), onClick: act.details },
      })
      return
    case 'failed': {
      // Squirrel refuses a second relaunch in the same process once one was
      // attempted; a fresh launch takes the cached download without fuss.
      const relaunch = /command is disabled/i.test(next.error)
        ? ` ${t('updates.toast.relaunchHint')}`
        : ''
      toast.error(`${t('updates.toast.failed')} AgentOS ${next.version}.${relaunch}`, {
        id: RELEASE_TOAST,
        duration: Infinity,
        description: next.error || null,
        action: { label: t('updates.toast.retry'), onClick: act.retry },
      })
      return
    }
    case 'working':
      // The pill carries the progress; the card would only repeat it.
      toast.dismiss(RELEASE_TOAST)
      return
    case 'none':
      if (prev.kind === 'working' && prev.step === 'engine') {
        toast.success(`${t('updates.toast.engineDone')} ${prev.version}.`, {
          id: RELEASE_TOAST,
          duration: 6000,
          description: null,
          action: undefined,
        })
      } else {
        toast.dismiss(RELEASE_TOAST)
      }
  }
}

function sameStep(a: ReleaseUpdate, b: ReleaseUpdate): boolean {
  if (a.kind !== b.kind) return false
  if (a.kind === 'none' || b.kind === 'none') return true
  if (a.version !== b.version) return false
  if (a.kind === 'restart' && b.kind === 'restart') return a.blocked === b.blocked
  if (a.kind === 'failed' && b.kind === 'failed') return a.error === b.error
  return true
}

/**
 * Mounted once in the shell. Watches both updaters and fires the notices;
 * an "Update" that touches the engine, or a "Restart gateway" that would cut
 * a live session, opens Settings › About instead, where the preflight and
 * "Update anyway" live.
 */
export function UpdateNotices() {
  const engine = useUpdates((s) => s.engine)
  const app = useUpdates((s) => s.app)
  const loaded = useUpdates((s) => s.loaded)
  const external = useSettings((s) => s.settings.gateway.mode === 'external')
  const status = useGatewayStatus()
  const { rows } = useSessions()
  const openSettings = useUi((s) => s.openSettings)
  const live = rows.some((r) => r.live)
  // Read by the toast actions, which outlive the render that made them.
  const liveRef = useRef(live)
  useEffect(() => {
    liveRef.current = live
  }, [live])

  const release = releaseUpdate(engine, app, status.data?.version, { external })
  const prev = useRef<ReleaseUpdate>({ kind: 'none' })

  useEffect(() => {
    if (!loaded) return
    const act: NoticeActions = {
      update: () => {
        // An engine update restarts the gateway: it goes through the
        // preflight in Settings › About (running sessions, jobs due soon,
        // pending approvals) and is applied only once confirmed there.
        const needsGateway = useUpdates.getState().engine.availability === 'outdated'
        if (needsGateway) openSettings('about')
        else void useUpdates.getState().updateAll()
      },
      restart: () => void useUpdates.getState().installApp(),
      restartGateway: () => {
        if (liveRef.current) openSettings('about')
        else void useGateway.getState().restart()
      },
      retry: () => void useUpdates.getState().retryApp(),
      details: () => openSettings('about'),
    }
    announceRelease(prev.current, release, act)
    prev.current = release
    // `release` is derived; compare by its identity-bearing fields only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, release.kind, releaseKey(release), openSettings])

  return null
}

function releaseKey(r: ReleaseUpdate): string {
  switch (r.kind) {
    case 'none':
      return ''
    case 'restart':
      return `${r.version}:${r.blocked ?? ''}`
    case 'failed':
      return `${r.version}:${r.error}`
    default:
      return r.version
  }
}
