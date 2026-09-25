import { LayoutPanelLeft, MessageSquare } from 'lucide-react'
import type { RawJob } from '@/views/cron/logic'
import { t } from '~/i18n'
import { badgeText } from '../logic'
import { missionStatus, statusWord, type StatusWord } from './desk-logic'
import { missionWord } from './MissionControls'
import type { DeskMode } from './mode-logic'

/**
 * The chat's top strip. In every mode the mode pill sits in the middle. In
 * Trading mode the row becomes the desk's status strip around it: missions
 * on the left, the one status word beside the pill, the approvals pin and
 * the Desk toggle on the right. The pin is null while the count is unknown
 * — never a broken count. In Chat mode the strip is the pill and, on the
 * right, a slot the open chat fills with its session actions (ChatView), so
 * the conversation needs no header row of its own.
 */
export function StatusStrip({
  mode,
  onSwitchMode,
  sessionSlot,
  missions = [],
  running = new Set(),
  sessionPending = 0,
  globalPending = null,
  streaming = false,
  deskMode = false,
  onToggleDesk,
  onOpenApprovals,
}: {
  mode: DeskMode
  onSwitchMode: (next: DeskMode) => void
  /** Chat mode: handed the element the chat's session actions go into. */
  sessionSlot?: (el: HTMLDivElement | null) => void
  missions?: RawJob[]
  running?: ReadonlySet<string>
  sessionPending?: number
  /** null while loading or errored. */
  globalPending?: number | null
  streaming?: boolean
  deskMode?: boolean
  onToggleDesk?: () => void
  onOpenApprovals?: () => void
}) {
  const trading = mode === 'trading'
  const word: StatusWord = statusWord({
    pendingApprovals: sessionPending,
    streaming,
    missionRunning: missions.some((m) => m.id && running.has(m.id)),
  })
  const shown = missions.slice(0, 2)
  return (
    <div className="trd-strip" data-mode={mode} data-testid="status-strip">
      <div className="trd-strip__left">
        {trading
          ? shown.map((job) => {
              const s = missionStatus(job, {
                running: Boolean(job.id && running.has(job.id)),
                pendingApprovals: sessionPending,
              })
              return (
                <span key={job.id ?? job.name} className="trd-strip__mission" data-state={s.state}>
                  <b>{job.name}</b>
                  <span>{missionWord(s.state, s.until)}</span>
                </span>
              )
            })
          : null}
        {trading && missions.length > shown.length ? (
          <span className="trd-strip__more">+{missions.length - shown.length}</span>
        ) : null}
      </div>
      <div className="trd-strip__centre">
        <ModePill mode={mode} onSwitch={onSwitchMode} />
        {/* No word for "idle": a label that says nothing is happening is
            chrome, not information. The row stays empty until there is
            something to say. */}
        {trading && word !== 'idle' ? (
          <div className="trd-strip__word" data-word={word} data-testid="status-word">
            {t(`trading.strip.${word}`)}
          </div>
        ) : null}
      </div>
      <div className="trd-strip__right">
        {trading && globalPending !== null && globalPending > 0 ? (
          <button
            type="button"
            className="trd-strip__pin app-no-drag"
            onClick={onOpenApprovals}
            data-testid="strip-pin"
          >
            {t('trading.strip.awaiting')} {badgeText(globalPending)}
          </button>
        ) : null}
        {trading ? (
          <button
            type="button"
            className="trd-strip__toggle app-no-drag"
            onClick={onToggleDesk}
            aria-pressed={deskMode}
            title={deskMode ? t('trading.strip.chat') : t('trading.strip.desk')}
            data-testid="desk-toggle"
          >
            {deskMode ? (
              <MessageSquare className="size-3.5" strokeWidth={1.75} aria-hidden />
            ) : (
              <LayoutPanelLeft className="size-3.5" strokeWidth={1.75} aria-hidden />
            )}
            {deskMode ? t('trading.strip.chat') : t('trading.strip.desk')}
          </button>
        ) : null}
        {!trading && sessionSlot ? (
          <div className="trd-strip__session" ref={sessionSlot} data-testid="strip-session" />
        ) : null}
      </div>
    </div>
  )
}

/**
 * Chat | Trading. Two wordmarks in one capsule; the active one is filled,
 * and the fill slides across when the mode changes. The tabs carry nothing
 * but their names: whether the desk is busy is the status word beside the
 * pill, not a mark on the Trading tab. Arrow keys move between the two, as a
 * tab list should.
 */
export function ModePill({
  mode,
  onSwitch,
}: {
  mode: DeskMode
  onSwitch: (next: DeskMode) => void
}) {
  const segs: DeskMode[] = ['chat', 'trading']
  return (
    <div
      className="trd-pill app-no-drag"
      role="tablist"
      aria-label={t('trading.mode.label')}
      data-mode={mode}
      data-testid="mode-pill"
      onKeyDown={(e) => {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
        e.preventDefault()
        onSwitch(mode === 'chat' ? 'trading' : 'chat')
      }}
    >
      <span className="trd-pill__thumb" aria-hidden />
      {segs.map((seg) => (
        <button
          key={seg}
          type="button"
          role="tab"
          className="trd-pill__seg"
          aria-selected={seg === mode}
          tabIndex={seg === mode ? 0 : -1}
          data-seg={seg}
          data-testid={`mode-${seg}`}
          onClick={() => {
            if (seg !== mode) onSwitch(seg)
          }}
        >
          {t(`trading.mode.${seg}`)}
        </button>
      ))}
    </div>
  )
}
