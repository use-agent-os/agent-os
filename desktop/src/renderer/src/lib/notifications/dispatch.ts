import { toast } from 'sonner'
import type { NotifyTarget } from '@shared/notify'
import { t } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'
import { playSound, windowInBackground } from '~/lib/notify'
import { useNotifyCenter } from '~/stores/notify-center'
import { useSettings } from '~/stores/settings'
import { useUi } from '~/stores/ui'
import { decideDelivery, sessionKeyFromHash, type Delivery, type NotifyEvent } from './logic'

/**
 * One door for every notification. Reads the settings at fire time, decides
 * through `decideDelivery`, then does each part: record in the bell, play
 * the sound, show the banner or ask main for a native notification. Returns
 * the decision so a caller (the test button) can tell the user what happened.
 */
export async function notify(ev: NotifyEvent): Promise<Delivery> {
  const settings = useSettings.getState().settings.notifications
  const decision = decideDelivery(settings, ev, {
    now: Date.now(),
    focused: !windowInBackground(),
    currentSessionKey: sessionKeyFromHash(window.location.hash),
  })
  if (decision.record) {
    useNotifyCenter.getState().push({
      kind: ev.kind,
      title: ev.title,
      subtitle: ev.subtitle,
      body: ev.body,
      target: ev.target,
      at: Date.now(),
      seen: decision.seen,
    })
  }
  if (decision.sound) playSound(settings.soundName)
  if (decision.banner) showBanner(ev)
  if (decision.bounce) void desktopApi().notify.bounce()
  if (decision.system) {
    const result = await desktopApi().notify.show({
      tag: `${ev.kind}:${targetTag(ev.target)}`,
      kind: ev.kind,
      title: ev.title,
      subtitle: ev.subtitle,
      body: ev.body,
      target: ev.target,
    })
    // Nowhere to post it (platform said no): keep it visible in the app.
    if (!result.shown) showBanner(ev)
  }
  return decision
}

function targetTag(target: NotifyTarget): string {
  switch (target.type) {
    case 'session':
      return target.key
    case 'jobs':
      return target.jobId ?? 'all'
    default:
      return target.type
  }
}

const TONE: Record<NotifyEvent['kind'], 'success' | 'error' | 'warning' | 'info'> = {
  reply: 'success',
  replyFailed: 'error',
  approval: 'warning',
  job: 'success',
  jobFailed: 'error',
  gateway: 'error',
  test: 'info',
}

function showBanner(ev: NotifyEvent): void {
  const description = [ev.subtitle, ev.body].filter(Boolean).join(' · ')
  const canOpen = ev.target.type !== 'none'
  toast[TONE[ev.kind]](ev.title, {
    id: `notify:${ev.kind}:${targetTag(ev.target)}`,
    description: description || undefined,
    duration: 6000,
    action: canOpen
      ? { label: t('notify.open'), onClick: () => activateTarget(ev.target) }
      : undefined,
  })
}

/** Where a click on a notification lands. Navigation is the shell's; this sets the stage. */
export let activateTarget: (target: NotifyTarget) => void = () => {}

export function bindActivation(fn: (target: NotifyTarget) => void): () => void {
  activateTarget = (target) => {
    // Sheets sit over the window: a click that wants the conversation must
    // not land under Settings.
    if (target.type === 'session') {
      useUi.getState().closeSettings()
      useUi.getState().closeJobs()
      useUi.getState().closeSkills()
    }
    fn(target)
  }
  return () => {
    activateTarget = () => {}
  }
}
