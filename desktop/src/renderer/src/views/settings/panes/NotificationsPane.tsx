import { useEffect, useId, useState } from 'react'
import { toast } from 'sonner'
import { isSystemSound, SYSTEM_SOUNDS, type NotifySound } from '@shared/notify'
import { REPLY_MIN_SECONDS, type ReplyMinSeconds } from '@shared/settings'
import { Button } from '~/components/ui/button'
import { Switch } from '~/components/ui/switch'
import { t } from '~/i18n'
import { desktopApi, isDesktop } from '~/lib/desktop-api'
import { playSound } from '~/lib/notify'
import { notify } from '~/lib/notifications/dispatch'
import { isMuted, MUTE_OPTIONS, muteUntilFor, type MuteOption } from '~/lib/notifications/logic'
import { useNow } from '~/lib/use-now'
import { useSettings } from '~/stores/settings'
import { Card, Head, Notice, Row, Segmented, Value } from '../parts'

const TIME = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' })
const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  weekday: 'short',
  hour: 'numeric',
  minute: '2-digit',
})

/** "15:30" today, "Fri 9:00" otherwise. */
export function formatMuteUntil(until: number, now: number): string {
  const sameDay = new Date(until).toDateString() === new Date(now).toDateString()
  return (sameDay ? TIME : DATE_TIME).format(until)
}

export function NotificationsPane() {
  const prefs = useSettings((s) => s.settings.notifications)
  const update = useSettings((s) => s.update)
  const now = useNow(30_000)
  const desktop = isDesktop()
  const soundId = useId()
  const muteId = useId()
  const replyMinId = useId()
  const [supported, setSupported] = useState<boolean | null>(null)

  useEffect(() => {
    let cancelled = false
    void desktopApi()
      .notify.supported()
      .then((v) => {
        if (!cancelled) setSupported(v)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const muted = isMuted(prefs, now)
  const off = !prefs.enabled

  async function sendTest() {
    const d = await notify({
      kind: 'test',
      title: t('notify.test.title'),
      body: t('notify.test.body'),
      target: { type: 'none' },
    })
    toast.info(
      d.system ? t('settings.notifications.test.sent') : t('settings.notifications.test.banner'),
      {
        id: 'stg-notify-test',
        description: d.system && desktop ? t('settings.notifications.test.blocked') : undefined,
        duration: 6000,
      },
    )
  }

  return (
    <>
      <Head
        title={t('settings.section.notifications')}
        blurb={t('settings.section.notifications.blurb')}
      />

      {!desktop ? <Notice tone="info">{t('settings.notifications.browserOnly')}</Notice> : null}
      {supported === false ? (
        <Notice tone="danger">{t('settings.notifications.permission.unsupported')}</Notice>
      ) : null}

      <Card
        title={t('settings.notifications.delivery')}
        action={
          <Button onClick={() => void sendTest()} disabled={supported === false}>
            {t('settings.notifications.test')}
          </Button>
        }
      >
        <Row
          label={t('settings.notifications.enabled')}
          help={t('settings.notifications.enabled.help')}
        >
          <Switch
            checked={prefs.enabled}
            aria-label={t('settings.notifications.enabled')}
            onCheckedChange={(enabled) => void update({ notifications: { enabled } })}
          />
        </Row>
        <Row
          label={t('settings.notifications.whenActive')}
          help={t('settings.notifications.whenActive.help')}
        >
          <Segmented
            label={t('settings.notifications.whenActive')}
            value={prefs.whenActive}
            disabled={off}
            options={[
              { value: 'skip', label: t('settings.notifications.whenActive.skip') },
              { value: 'banner', label: t('settings.notifications.whenActive.banner') },
              { value: 'system', label: t('settings.notifications.whenActive.system') },
            ]}
            onChange={(whenActive) => void update({ notifications: { whenActive } })}
          />
        </Row>
        <Row
          label={t('settings.notifications.mute')}
          htmlFor={muteId}
          help={
            muted && prefs.muteUntil !== null
              ? `${t('settings.notifications.mute.until')} ${formatMuteUntil(prefs.muteUntil, now)}`
              : t('settings.notifications.mute.help')
          }
        >
          <select
            id={muteId}
            className="mac-select"
            data-compact="true"
            disabled={off}
            value={muted ? 'on' : 'off'}
            onChange={(e) => {
              const option = e.target.value as MuteOption | 'on'
              if (option === 'on') return
              void update({ notifications: { muteUntil: muteUntilFor(option, Date.now()) } })
            }}
          >
            {muted ? (
              <option value="on" disabled>
                {t('settings.notifications.mute.until')}{' '}
                {prefs.muteUntil !== null ? formatMuteUntil(prefs.muteUntil, now) : ''}
              </option>
            ) : null}
            {MUTE_OPTIONS.map((o) => (
              <option key={o} value={o}>
                {t(`mute.${o}`)}
              </option>
            ))}
          </select>
        </Row>
        <Row
          label={t('settings.notifications.preview')}
          help={t('settings.notifications.preview.help')}
        >
          <Switch
            checked={prefs.preview}
            disabled={off}
            aria-label={t('settings.notifications.preview')}
            onCheckedChange={(preview) => void update({ notifications: { preview } })}
          />
        </Row>
      </Card>

      <Card title={t('settings.notifications.events')}>
        <Row
          label={t('settings.notifications.replyDone')}
          help={t('settings.notifications.replyDone.help')}
        >
          <Switch
            checked={prefs.replyDone}
            disabled={off}
            aria-label={t('settings.notifications.replyDone')}
            onCheckedChange={(replyDone) => void update({ notifications: { replyDone } })}
          />
        </Row>
        <Row label={t('settings.notifications.replyMin')} htmlFor={replyMinId}>
          <select
            id={replyMinId}
            className="mac-select"
            data-compact="true"
            disabled={off || !prefs.replyDone}
            value={String(prefs.replyMinSeconds)}
            onChange={(e) => {
              const replyMinSeconds = Number(e.target.value) as ReplyMinSeconds
              void update({ notifications: { replyMinSeconds } })
            }}
          >
            {REPLY_MIN_SECONDS.map((s) => (
              <option key={s} value={String(s)}>
                {t(
                  s === 0
                    ? 'settings.notifications.replyMin.any'
                    : `settings.notifications.replyMin.${s}`,
                )}
              </option>
            ))}
          </select>
        </Row>
        <Row
          label={t('settings.notifications.replyFailed')}
          help={t('settings.notifications.replyFailed.help')}
        >
          <Switch
            checked={prefs.replyFailed}
            disabled={off}
            aria-label={t('settings.notifications.replyFailed')}
            onCheckedChange={(replyFailed) => void update({ notifications: { replyFailed } })}
          />
        </Row>
        <Row
          label={t('settings.notifications.approvals')}
          help={t('settings.notifications.approvals.help')}
        >
          <Switch
            checked={prefs.approvals}
            disabled={off}
            aria-label={t('settings.notifications.approvals')}
            onCheckedChange={(approvals) => void update({ notifications: { approvals } })}
          />
        </Row>
        <Row label={t('settings.notifications.jobs')} help={t('settings.notifications.jobs.help')}>
          <Segmented
            label={t('settings.notifications.jobs')}
            value={prefs.jobs}
            disabled={off}
            options={[
              { value: 'off', label: t('settings.notifications.jobs.off') },
              { value: 'failures', label: t('settings.notifications.jobs.failures') },
              { value: 'all', label: t('settings.notifications.jobs.all') },
            ]}
            onChange={(jobs) => void update({ notifications: { jobs } })}
          />
        </Row>
        <Row
          label={t('settings.notifications.gateway')}
          help={t('settings.notifications.gateway.help')}
        >
          <Switch
            checked={prefs.gateway}
            disabled={off}
            aria-label={t('settings.notifications.gateway')}
            onCheckedChange={(gateway) => void update({ notifications: { gateway } })}
          />
        </Row>
      </Card>

      <Card title={t('settings.notifications.sound')}>
        <Row
          label={t('settings.notifications.sound.play')}
          help={t('settings.notifications.sound.play.help')}
        >
          <Switch
            checked={prefs.sound}
            disabled={off}
            aria-label={t('settings.notifications.sound.play')}
            onCheckedChange={(sound) => void update({ notifications: { sound } })}
          />
        </Row>
        <Row label={t('settings.notifications.sound.pick')} htmlFor={soundId}>
          <Button
            disabled={off || !prefs.sound}
            onClick={() => playSound(prefs.soundName)}
            aria-label={`${t('settings.notifications.sound.preview')} ${t('settings.notifications.sound.pick')}`}
          >
            {t('settings.notifications.sound.preview')}
          </Button>
          <select
            id={soundId}
            className="mac-select"
            data-compact="true"
            disabled={off || !prefs.sound}
            value={prefs.soundName}
            onChange={(e) => {
              const soundName = e.target.value as NotifySound
              if (soundName !== 'chime' && !isSystemSound(soundName)) return
              void update({ notifications: { soundName } }).then(() => playSound(soundName))
            }}
          >
            <option value="chime">{t('settings.notifications.sound.chime')}</option>
            {desktop ? (
              <optgroup label={t('settings.notifications.sound.system')}>
                {SYSTEM_SOUNDS.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </optgroup>
            ) : null}
          </select>
        </Row>
      </Card>

      {desktop ? (
        <Card title={t('settings.notifications.dock')}>
          <Row
            label={t('settings.notifications.badge')}
            help={t('settings.notifications.badge.help')}
          >
            <Switch
              checked={prefs.badge}
              disabled={off}
              aria-label={t('settings.notifications.badge')}
              onCheckedChange={(badge) => void update({ notifications: { badge } })}
            />
          </Row>
          <Row
            label={t('settings.notifications.bounce')}
            help={t('settings.notifications.bounce.help')}
          >
            <Switch
              checked={prefs.bounce}
              disabled={off}
              aria-label={t('settings.notifications.bounce')}
              onCheckedChange={(bounce) => void update({ notifications: { bounce } })}
            />
          </Row>
        </Card>
      ) : null}

      {desktop ? (
        <Card title={t('settings.notifications.system')}>
          <Row
            label={t('settings.notifications.permission')}
            help={t('settings.notifications.permission.help')}
          >
            {supported === false ? (
              <Value tone="danger">{t('settings.notifications.permission.unsupported')}</Value>
            ) : null}
            <Button onClick={() => void desktopApi().notify.openSystemSettings()}>
              {t('settings.notifications.permission.open')}
            </Button>
          </Row>
        </Card>
      ) : null}
    </>
  )
}
