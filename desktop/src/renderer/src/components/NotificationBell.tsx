import './notification-bell.css'
import {
  AlertTriangle,
  Bell,
  BellOff,
  CalendarClock,
  CheckCircle2,
  MessageSquare,
  Settings,
  ShieldAlert,
  Volume2,
  VolumeX,
  XCircle,
} from 'lucide-react'
import { useCallback, useEffect, useId, useRef } from 'react'
import type { NotifyKind } from '@shared/notify'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { activateTarget } from '~/lib/notifications/dispatch'
import { isMuted, MUTE_OPTIONS, muteUntilFor, type MuteOption } from '~/lib/notifications/logic'
import { shortAge } from '~/lib/relative-time'
import { useNow } from '~/lib/use-now'
import { cn } from '~/lib/utils'
import { useNotifyCenter, unseenCount, type NotifyItem } from '~/stores/notify-center'
import { useSettings } from '~/stores/settings'
import { useUi } from '~/stores/ui'
import { formatMuteUntil } from '~/views/settings/panes/NotificationsPane'

const ICON: Record<NotifyKind, typeof Bell> = {
  reply: MessageSquare,
  replyFailed: XCircle,
  approval: ShieldAlert,
  job: CalendarClock,
  jobFailed: AlertTriangle,
  gateway: XCircle,
  test: CheckCircle2,
}
const TONE: Record<NotifyKind, 'ok' | 'warn' | 'danger' | 'dim'> = {
  reply: 'ok',
  replyFailed: 'danger',
  approval: 'warn',
  job: 'ok',
  jobFailed: 'danger',
  gateway: 'danger',
  test: 'dim',
}

/**
 * The toolbar bell: a count of what arrived while you were not looking,
 * a slash when muted or off, and a popover with the recent notifications,
 * Do not disturb, the sound toggle and a door to Settings › Notifications.
 */
export function NotificationBell() {
  const items = useNotifyCenter((s) => s.items)
  const open = useNotifyCenter((s) => s.open)
  const setOpen = useNotifyCenter((s) => s.setOpen)
  const prefs = useSettings((s) => s.settings.notifications)
  const now = useNow(open ? 15_000 : 0)
  const unseen = unseenCount(items)
  const muted = isMuted(prefs, now)
  const quiet = muted || !prefs.enabled
  const label = t('bell.label')
  const popId = useId()

  return (
    <div className="bell">
      <Button
        variant={open ? 'secondary' : 'ghost'}
        size="icon"
        aria-label={unseen > 0 ? `${label} (${unseen})` : label}
        title={label}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? popId : undefined}
        onClick={() => setOpen(!open)}
      >
        <span className="relative flex">
          {quiet ? (
            <BellOff className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
          ) : (
            <Bell className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
          )}
          {unseen > 0 ? (
            <span className="bell__count" aria-hidden>
              {unseen > 9 ? '9+' : unseen}
            </span>
          ) : null}
        </span>
      </Button>
      {open ? <BellPopover id={popId} now={now} onClose={() => setOpen(false)} /> : null}
    </div>
  )
}

function BellPopover({ id, now, onClose }: { id: string; now: number; onClose: () => void }) {
  const items = useNotifyCenter((s) => s.items)
  const clear = useNotifyCenter((s) => s.clear)
  const prefs = useSettings((s) => s.settings.notifications)
  const update = useSettings((s) => s.update)
  const openSettings = useUi((s) => s.openSettings)
  const ref = useRef<HTMLDivElement>(null)
  const muted = isMuted(prefs, now)

  // Escape or a press outside closes; the bell button itself toggles.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
      }
    }
    const onDown = (e: MouseEvent) => {
      const anchor = ref.current?.parentElement
      if (anchor && !anchor.contains(e.target as Node)) onClose()
    }
    document.addEventListener('keydown', onKey, true)
    document.addEventListener('mousedown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.removeEventListener('mousedown', onDown)
    }
  }, [onClose])

  const openItem = useCallback(
    (item: NotifyItem) => {
      onClose()
      activateTarget(item.target)
    },
    [onClose],
  )

  return (
    <div
      ref={ref}
      id={id}
      className="bell__pop app-no-drag"
      role="dialog"
      aria-label={t('bell.title')}
    >
      <div className="bell__head">
        <span className="bell__title">{t('bell.title')}</span>
        <div className="bell__tools">
          <Button
            variant="ghost"
            size="icon"
            aria-label={prefs.sound ? t('bell.sound.on') : t('bell.sound.off')}
            title={prefs.sound ? t('bell.sound.on') : t('bell.sound.off')}
            aria-pressed={prefs.sound}
            onClick={() => void update({ notifications: { sound: !prefs.sound } })}
          >
            {prefs.sound ? (
              <Volume2 className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            ) : (
              <VolumeX className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            )}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label={t('bell.settings')}
            title={t('bell.settings')}
            onClick={() => {
              onClose()
              openSettings('notifications')
            }}
          >
            <Settings className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
          </Button>
        </div>
      </div>

      <div className="bell__mute">
        <span className="bell__mute-label">
          {muted && prefs.muteUntil !== null
            ? `${t('bell.muted')} ${formatMuteUntil(prefs.muteUntil, now)}`
            : t('bell.mute')}
        </span>
        {muted ? (
          <Button onClick={() => void update({ notifications: { muteUntil: null } })}>
            {t('bell.unmute')}
          </Button>
        ) : (
          <select
            className="mac-select"
            data-compact="true"
            aria-label={t('bell.mute')}
            value="off"
            onChange={(e) => {
              const option = e.target.value as MuteOption
              if (option === 'off') return
              void update({ notifications: { muteUntil: muteUntilFor(option, Date.now()) } })
            }}
          >
            {MUTE_OPTIONS.map((o) => (
              <option key={o} value={o}>
                {t(`mute.${o}`)}
              </option>
            ))}
          </select>
        )}
      </div>

      {!prefs.enabled ? (
        <div className="bell__off">
          <span>{t('bell.off')}</span>
          <Button onClick={() => void update({ notifications: { enabled: true } })}>
            {t('bell.turnOn')}
          </Button>
        </div>
      ) : null}

      {items.length === 0 ? (
        <div className="bell__empty">
          <span className="bell__empty-title">{t('bell.empty')}</span>
          <span>{t('bell.empty.help')}</span>
        </div>
      ) : (
        <ul className="bell__list">
          {items.map((item) => {
            const Icon = ICON[item.kind]
            const clickable = item.target.type !== 'none'
            return (
              <li key={item.id}>
                <button
                  type="button"
                  className={cn('bell__item', !clickable && 'bell__item--static')}
                  data-tone={TONE[item.kind]}
                  disabled={!clickable}
                  onClick={() => openItem(item)}
                >
                  <Icon className="size-3.5" strokeWidth={1.75} aria-hidden />
                  <span className="bell__item-text">
                    <span className="bell__item-title">{item.title}</span>
                    {item.subtitle || item.body ? (
                      <span className="bell__item-body">
                        {[item.subtitle, item.body].filter(Boolean).join(' · ')}
                      </span>
                    ) : null}
                  </span>
                  <span className="bell__item-age">{shortAge(item.at, now)}</span>
                </button>
              </li>
            )
          })}
        </ul>
      )}

      {items.length > 0 ? (
        <div className="bell__foot">
          <Button variant="ghost" onClick={clear}>
            {t('bell.clear')}
          </Button>
        </div>
      ) : null}
    </div>
  )
}
