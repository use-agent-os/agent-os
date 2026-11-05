import { useQuery } from '@tanstack/react-query'
import { ExternalLink, OctagonX, Pause, Pencil, Play, Trash2, Zap } from 'lucide-react'
import { useEffect, useState, useSyncExternalStore } from 'react'
import { useRpc } from '@/app/providers'
import type { RawJob } from '@/views/cron/logic'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import type { Bracket, Mandate, Trigger } from '../types'
import {
  bracketFireKey,
  bracketNear,
  bracketPlanText,
  bracketReason,
  bracketRows,
  bracketWord,
  isSteerableBracket,
} from './bracket-logic'
import { jobSessionKey, missionStatus, type MissionState } from './desk-logic'
import {
  mandateBuysText,
  mandateMissesText,
  mandateProgress,
  mandateProgressText,
  mandateReason,
  mandateRows,
  mandateState,
} from './mandate-logic'
import {
  fireNowKey,
  isNear,
  isSteerableTrigger,
  planText,
  triggerReason,
  triggerRows,
  triggerWord,
} from './trigger-logic'

const NO_MANDATES: Mandate[] = []
const NO_TRIGGERS: Trigger[] = []
const NO_BRACKETS: Bracket[] = []
const NO_IDS: ReadonlySet<string> = new Set()
/** A second click on Stop within this long stops the mandate; after it, the button disarms. */
const STOP_ARM_MS = 4000

function clock(ts: number | null): string {
  if (!ts) return ''
  return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(ts)
}

export function missionWord(state: MissionState, until: number | null): string {
  switch (state) {
    case 'running':
      return t('trading.mission.state.running')
    case 'awaiting':
      return t('trading.mission.state.awaiting')
    case 'sleeping':
      return until
        ? `${t('trading.mission.state.sleeping')} · ${t('trading.mission.until')} ${clock(until)}`
        : t('trading.mission.state.sleeping')
    case 'paused':
      return t('trading.mission.state.paused')
    case 'failed':
      return t('trading.mission.state.failed')
    default:
      return t('trading.mission.state.done')
  }
}

/**
 * A mandate's state word: "Awaiting approval", "Active · next 3 h 12 m",
 * "Active · buy due", "Paused", "Done". The engine's figures, in words.
 */
export function mandateWord(m: Mandate, now: number): string {
  const s = mandateState(m, now)
  if (s.next) return `${t(s.key)} · ${t('trading.dca.next')} ${s.next}`
  if (s.due) return `${t(s.key)} · ${t('trading.dca.due')}`
  return t(s.key)
}

/*
 * One second-tick shared by every DCA countdown on screen — the Missions
 * rows, the strip above the composer, the status strip's chip — so two
 * surfaces never read two clocks and disagree about the same buy (one said
 * "next 59 m" while the other, on a staler clock, still said "next 1 h").
 */
const clockListeners = new Set<() => void>()
let clockNow = 0
let clockTimer: ReturnType<typeof setInterval> | null = null

function subscribeClock(listener: () => void): () => void {
  clockListeners.add(listener)
  if (clockTimer === null) {
    clockNow = Date.now()
    clockTimer = setInterval(() => {
      clockNow = Date.now()
      for (const l of clockListeners) l()
    }, 1_000)
  }
  return () => {
    clockListeners.delete(listener)
    if (clockListeners.size === 0 && clockTimer !== null) {
      clearInterval(clockTimer)
      clockTimer = null
    }
  }
}

// While nothing ticks, the second the render happens in: stable across the
// reads of one render, never a stale tick from an earlier mount.
const readClock = () => (clockTimer === null ? Math.floor(Date.now() / 1_000) * 1_000 : clockNow)
const noClock = () => () => {}

/** The shared clock, ticking while any listed mandate has a next buy to count down to. */
export function useMandateClock(mandates: readonly Mandate[]): number {
  const ticking = mandates.some((m) => m.status === 'active' && Boolean(m.schedule.nextRunAt))
  return useSyncExternalStore(ticking ? subscribeClock : noClock, readClock)
}

/** "Paused by you" for the operator's own bare pause or stop; the engine's words otherwise. */
export function mandateTitle(m: Mandate): string | null {
  const reason = mandateReason(m)
  if (reason !== 'user') return reason
  return m.status === 'paused' ? t('trading.dca.reason.pausedByYou') : t('trading.dca.reason.byYou')
}

/** "$120 / $300" over a hairline bar: the engine's spent against its cap. */
function MandateProgress({ mandate }: { mandate: Mandate }) {
  const pct = Math.round(mandateProgress(mandate) * 100)
  return (
    <span
      className="trd-mprog"
      role="meter"
      aria-label={t('trading.dca.progress')}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
      data-testid="mandate-progress"
    >
      <span className="trd-mprog__bar" aria-hidden>
        <span className="trd-mprog__fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="trd-mprog__text trd-mono">{mandateProgressText(mandate)}</span>
    </span>
  )
}

/**
 * The one-line band above the composer that says what the missions are
 * doing. Renders nothing when there is nothing running: a band that says
 * IDLE over an idle composer is chrome, not information.
 *
 * Only cron missions are named here: their rows in the controls below have
 * no state word, so this is where it is said. A DCA mandate's or a price
 * trigger's row already carries its state and figures, and naming them twice
 * cost the transcript a line each.
 */
export function MissionStrip({
  missions,
  running,
  pendingApprovals,
}: {
  missions: RawJob[]
  running: ReadonlySet<string>
  pendingApprovals: number
}) {
  if (missions.length === 0) return null
  return (
    <div className="trd-mstrip" role="status" data-testid="mission-strip">
      {missions.map((job) => {
        const s = missionStatus(job, {
          running: Boolean(job.id && running.has(job.id)),
          pendingApprovals,
        })
        return (
          <span key={job.id ?? job.name} className="trd-mstrip__item" data-state={s.state}>
            <span className="trd-mstrip__dot" aria-hidden />
            <b>{job.name}</b>
            <span className="trd-mstrip__word">{missionWord(s.state, s.until)}</span>
          </span>
        )
      })}
    </div>
  )
}

/**
 * Status-gated controls for the missions this chat runs. Every refusal the
 * scheduler returns is shown as copy by the hook; nothing here fails silently.
 */
export function MissionControls({
  missions,
  running,
  pendingApprovals,
  busy,
  onStart,
  onEdit,
  onRun,
  onSetEnabled,
  onRemove,
  showStart = true,
  mandates = NO_MANDATES,
  mandateBusy = null,
  onMandatePause,
  onMandateResume,
  onMandateRun,
  onMandateEdit,
  onMandateStop,
  triggers = NO_TRIGGERS,
  askedTriggers = NO_IDS,
  triggerBusy = null,
  onTriggerPause,
  onTriggerResume,
  onTriggerFire,
  onTriggerStop,
  brackets = NO_BRACKETS,
  askedBrackets = NO_IDS,
  bracketBusy = null,
  onBracketPause,
  onBracketResume,
  onBracketFire,
  onBracketStop,
  sessionKey = '',
  onOpenSession,
}: {
  missions: RawJob[]
  running: ReadonlySet<string>
  pendingApprovals: number
  busy: boolean
  onStart: () => void
  onEdit: (job: RawJob) => void
  onRun: (job: RawJob) => void
  onSetEnabled: (job: RawJob, enabled: boolean) => void
  onRemove: (job: RawJob) => void
  /** The seats row carries its own Start chip when the controls sit above it. */
  showStart?: boolean
  /** DCA mandates, listed after the cron missions with their own controls. */
  mandates?: Mandate[]
  /** The mandate with a write in flight: its row is locked until it lands. */
  mandateBusy?: string | null
  onMandatePause?: (m: Mandate) => void
  onMandateResume?: (m: Mandate) => void
  onMandateRun?: (m: Mandate) => void
  onMandateEdit?: (m: Mandate) => void
  onMandateStop?: (m: Mandate) => void
  /** Price triggers, listed after the mandates with their own controls. */
  triggers?: Trigger[]
  /**
   * Pending triggers whose proposal card is already on screen in the
   * approvals region: decided there, so not listed again as a row.
   */
  askedTriggers?: ReadonlySet<string>
  /** The trigger with a write in flight: its row is locked until it lands. */
  triggerBusy?: string | null
  onTriggerPause?: (tr: Trigger) => void
  onTriggerResume?: (tr: Trigger) => void
  onTriggerFire?: (tr: Trigger) => void
  onTriggerStop?: (tr: Trigger) => void
  /** Brackets (docs/brackets.md), one row each — never a row per leg — before the triggers. */
  brackets?: Bracket[]
  /** Pending brackets whose proposal card is already in the approvals region. */
  askedBrackets?: ReadonlySet<string>
  /** The bracket with a write in flight: its row is locked until it lands. */
  bracketBusy?: string | null
  onBracketPause?: (b: Bracket) => void
  onBracketResume?: (b: Bracket) => void
  onBracketFire?: (b: Bracket) => void
  onBracketStop?: (b: Bracket) => void
  /** This desk's chat: a row filed by another chat says so, and opens it. */
  sessionKey?: string
  onOpenSession?: (sessionKey: string) => void
}) {
  // The chat a row was filed by, when it is not this one; null when it is,
  // when the row is unfiled (the operator's and so every desk's), or when
  // the list has no chat of its own to compare against.
  const elsewhere = (filed: string | null | undefined): string | null =>
    sessionKey && filed && filed !== sessionKey ? filed : null
  const now = useMandateClock(mandates)
  const [showAll, setShowAll] = useState(false)
  const { rows, more } = mandateRows(mandates, showAll)
  const [showAllTriggers, setShowAllTriggers] = useState(false)
  const triggerList = triggerRows(
    askedTriggers.size ? triggers.filter((tr) => !askedTriggers.has(tr.id)) : triggers,
    showAllTriggers,
  )
  const [showAllBrackets, setShowAllBrackets] = useState(false)
  const bracketList = bracketRows(
    askedBrackets.size ? brackets.filter((b) => !askedBrackets.has(b.id)) : brackets,
    showAllBrackets,
  )
  return (
    <div className="trd-mctl" data-testid="mission-controls">
      {missions.map((job) => {
        const live = Boolean(job.id && running.has(job.id))
        const s = missionStatus(job, { running: live, pendingApprovals })
        const enabled = job.enabled !== false
        return (
          <div key={job.id ?? job.name} className="trd-mctl__row" data-state={s.state}>
            <span className="trd-mctl__name" title={job.name}>
              {job.name}
            </span>
            <Elsewhere sessionKey={elsewhere(jobSessionKey(job))} onOpen={onOpenSession} />
            {enabled ? (
              <Button
                variant="ghost"
                size="icon"
                disabled={busy || live}
                aria-label={t('trading.mission.stop')}
                title={t('trading.mission.stop')}
                onClick={() => onSetEnabled(job, false)}
                data-testid="mission-stop"
              >
                <Pause className="size-3.5" strokeWidth={1.75} aria-hidden />
              </Button>
            ) : (
              <Button
                variant="ghost"
                size="icon"
                disabled={busy}
                aria-label={t('trading.mission.continue')}
                title={t('trading.mission.continue')}
                onClick={() => onSetEnabled(job, true)}
                data-testid="mission-continue"
              >
                <Play className="size-3.5" strokeWidth={1.75} aria-hidden />
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon"
              disabled={busy || live || s.state === 'awaiting'}
              aria-label={t('trading.mission.runNow')}
              title={t('trading.mission.runNow')}
              onClick={() => onRun(job)}
              data-testid="mission-run"
            >
              <Zap className="size-3.5" strokeWidth={1.75} aria-hidden />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              disabled={busy || s.state === 'awaiting'}
              aria-label={t('trading.mission.edit')}
              title={t('trading.mission.edit')}
              onClick={() => onEdit(job)}
              data-testid="mission-edit"
            >
              <Pencil className="size-3.5" strokeWidth={1.75} aria-hidden />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              disabled={busy || live}
              aria-label={t('trading.mission.remove')}
              title={t('trading.mission.remove')}
              onClick={() => onRemove(job)}
              data-testid="mission-remove"
            >
              <Trash2 className="size-3.5" strokeWidth={1.75} aria-hidden />
            </Button>
          </div>
        )
      })}
      {rows.map((m) => (
        <MandateRow
          key={m.id}
          mandate={m}
          now={now}
          busy={mandateBusy === m.id}
          onPause={onMandatePause}
          onResume={onMandateResume}
          onRun={onMandateRun}
          onEdit={onMandateEdit}
          onStop={onMandateStop}
          elsewhere={elsewhere(m.sessionKey)}
          onOpenSession={onOpenSession}
        />
      ))}
      {more > 0 ? (
        <MoreToggle more={more} showAll={showAll} onToggle={() => setShowAll((v) => !v)} />
      ) : null}
      {bracketList.rows.map((b) => (
        <BracketRow
          key={b.id}
          bracket={b}
          busy={bracketBusy === b.id}
          onPause={onBracketPause}
          onResume={onBracketResume}
          onFire={onBracketFire}
          onStop={onBracketStop}
          elsewhere={elsewhere(b.sessionKey)}
          onOpenSession={onOpenSession}
        />
      ))}
      {bracketList.more > 0 ? (
        <MoreToggle
          more={bracketList.more}
          showAll={showAllBrackets}
          onToggle={() => setShowAllBrackets((v) => !v)}
          testId="bracket-more"
        />
      ) : null}
      {triggerList.rows.map((tr) => (
        <TriggerRow
          key={tr.id}
          trigger={tr}
          busy={triggerBusy === tr.id}
          onPause={onTriggerPause}
          onResume={onTriggerResume}
          onFire={onTriggerFire}
          onStop={onTriggerStop}
          elsewhere={elsewhere(tr.sessionKey)}
          onOpenSession={onOpenSession}
        />
      ))}
      {triggerList.more > 0 ? (
        <MoreToggle
          more={triggerList.more}
          showAll={showAllTriggers}
          onToggle={() => setShowAllTriggers((v) => !v)}
          testId="trigger-more"
        />
      ) : null}
      {showStart ? (
        <button
          type="button"
          className="trd-mctl__start app-no-drag"
          onClick={onStart}
          data-testid="mission-start"
        >
          <Zap className="size-3" strokeWidth={2.25} aria-hidden />
          {t('trading.mission.start')}
        </button>
      ) : null}
    </div>
  )
}

/**
 * Whether the gateway says this chat no longer exists. It asks by key because
 * `sessions.list` is capped: an old chat missing from it may well be alive.
 * True only on that answer, so a slow or failed check keeps the way back. The
 * query key sits under `sessions`, whose invalidation follows every delete.
 */
function useChatDeleted(sessionKey: string | null): boolean {
  const rpc = useRpc()
  const { data } = useQuery({
    queryKey: ['sessions', 'deleted', sessionKey],
    enabled: Boolean(sessionKey),
    queryFn: async () => {
      await rpc.waitForConnection()
      const reply = await rpc.call<{ previews?: { key?: string }[] }>('sessions.preview', {
        keys: [sessionKey],
      })
      return Array.isArray(reply?.previews) && !reply.previews.some((p) => p.key === sessionKey)
    },
  })
  return data === true
}

/**
 * The icon on a row another trading chat filed: the engine runs it whatever
 * chat is open, so the desk lists it rather than let it trade unseen, and
 * the icon opens the chat that can tell its story. Nothing for a row of
 * this chat's, an unfiled one, or one whose chat has been deleted: the row
 * keeps running, but there is no story left to open.
 */
export function Elsewhere({
  sessionKey,
  onOpen,
}: {
  sessionKey: string | null
  onOpen?: (sessionKey: string) => void
}) {
  const deleted = useChatDeleted(sessionKey)
  if (!sessionKey || deleted) return null
  return (
    <button
      type="button"
      className="trd-mctl__elsewhere app-no-drag"
      aria-label={t('trading.mission.elsewhereTitle')}
      title={t('trading.mission.elsewhereTitle')}
      onClick={() => onOpen?.(sessionKey)}
      data-testid="mission-elsewhere"
      data-session={sessionKey}
    >
      <ExternalLink className="size-3.5" strokeWidth={1.75} aria-hidden />
    </button>
  )
}

/**
 * "+N more" opens every finished mandate this desk still lists; "show fewer"
 * folds them back to the newest two. Absent when nothing is folded away.
 */
function MoreToggle({
  more,
  showAll,
  onToggle,
  testId = 'mandate-more',
}: {
  more: number
  showAll: boolean
  onToggle: () => void
  testId?: string
}) {
  return (
    <button
      type="button"
      className="trd-mctl__more app-no-drag"
      aria-expanded={showAll}
      onClick={onToggle}
      data-testid={testId}
    >
      {showAll ? t('trading.dca.fewer') : t('trading.dca.more').replace('{count}', String(more))}
    </button>
  )
}

/**
 * One DCA mandate among the missions: its name, what it is doing, spent of
 * cap, and the controls its state allows. A pending one is decided on its
 * card (the approvals region), so its row only points there; a finished one
 * has nothing left to steer. Stop is final, so it asks for a second click —
 * inline, never a dialog.
 */
function MandateRow({
  mandate: m,
  now,
  busy,
  onPause,
  onResume,
  onRun,
  onEdit,
  onStop,
  elsewhere = null,
  onOpenSession,
}: {
  mandate: Mandate
  now: number
  busy: boolean
  onPause?: (m: Mandate) => void
  onResume?: (m: Mandate) => void
  onRun?: (m: Mandate) => void
  onEdit?: (m: Mandate) => void
  onStop?: (m: Mandate) => void
  elsewhere?: string | null
  onOpenSession?: (sessionKey: string) => void
}) {
  // Why it stopped or paused ("cap reached", "paused after 3 runs: …"), on hover.
  const reason = mandateTitle(m)
  const misses = mandateMissesText(m)
  return (
    <div
      className="trd-mctl__row"
      data-kind="mandate"
      data-state={m.status}
      data-testid="mandate-row"
      data-mandate={m.id}
      title={reason ?? undefined}
    >
      <span className="trd-mctl__kind" aria-hidden>
        {t('trading.dca.kind')}
      </span>
      <span className="trd-mctl__name" title={reason ? `${m.name} · ${reason}` : m.name}>
        {m.name}
      </span>
      <Elsewhere sessionKey={elsewhere} onOpen={onOpenSession} />
      <span className="trd-mctl__word" data-testid="mandate-word">
        {mandateWord(m, now)}
      </span>
      <span className="trd-mctl__runs trd-mono" data-testid="mandate-runs">
        {mandateBuysText(m)}
        {misses ? <span className="trd-mctl__misses"> {misses}</span> : null}
      </span>
      <MandateProgress mandate={m} />
      {m.status === 'awaiting_approval' ? (
        <span className="trd-mctl__hint">{t('trading.dca.review')}</span>
      ) : null}
      <MandateControls
        mandate={m}
        busy={busy}
        onPause={onPause}
        onResume={onResume}
        onRun={onRun}
        onEdit={onEdit}
        onStop={onStop}
      />
    </div>
  )
}

/**
 * A two-click control: the first click arms it, the second (within
 * STOP_ARM_MS) acts. `settle` is the row's state and its write in flight:
 * when either moves (the write resolved, the engine's answer landed), the
 * arm is dropped, so a row never offers "Stop — click again" for an act it
 * has just done.
 */
function useArm(settle: string): [boolean, (v: boolean) => void] {
  const [armed, setArmed] = useState(false)
  const [seen, setSeen] = useState(settle)
  if (seen !== settle) {
    setSeen(settle)
    setArmed(false)
  }
  useEffect(() => {
    if (!armed) return
    const id = window.setTimeout(() => setArmed(false), STOP_ARM_MS)
    return () => window.clearTimeout(id)
  }, [armed])
  return [armed, setArmed]
}

/** "Paused by you" for the operator's own bare pause or stop; the engine's words otherwise. */
export function triggerTitle(tr: Trigger): string | null {
  const reason = triggerReason(tr)
  if (reason !== 'user') return reason
  return tr.status === 'paused'
    ? t('trading.trigger.reason.pausedByYou')
    : t('trading.trigger.reason.byYou')
}

/**
 * One price trigger among the missions: what it fires, its name, what it is
 * doing ("Armed · ETH $3,790 · −0.3 %"), its plan, and the controls its state
 * allows. A pending one is decided on its card; a triggered one can only be
 * stopped; a finished one has nothing left to steer. Fire now and Stop act
 * on a second click — inline, never a dialog: one trades at once, the other
 * is final.
 */
function TriggerRow({
  trigger: tr,
  busy,
  onPause,
  onResume,
  onFire,
  onStop,
  elsewhere = null,
  onOpenSession,
}: {
  trigger: Trigger
  busy: boolean
  onPause?: (tr: Trigger) => void
  onResume?: (tr: Trigger) => void
  onFire?: (tr: Trigger) => void
  onStop?: (tr: Trigger) => void
  elsewhere?: string | null
  onOpenSession?: (sessionKey: string) => void
}) {
  // The engine's statusReason on hover: "paused: nothing to sell", "alerted at $3,790".
  const reason = triggerTitle(tr)
  const word = triggerWord(tr)
  return (
    <div
      className="trd-mctl__row"
      data-kind="trigger"
      data-action={tr.kind}
      data-state={tr.status}
      data-near={isNear(tr) || undefined}
      data-testid="trigger-row"
      data-trigger={tr.id}
      title={reason ?? undefined}
    >
      <span className="trd-mctl__kind" data-action={tr.kind} aria-hidden>
        {t(`trading.trigger.tag.${tr.kind}`)}
      </span>
      <span className="trd-mctl__name" title={reason ? `${tr.name} · ${reason}` : tr.name}>
        {tr.name}
      </span>
      <Elsewhere sessionKey={elsewhere} onOpen={onOpenSession} />
      {/* Ellipsized in a narrow column: the whole word on hover. */}
      <span className="trd-mctl__word" data-testid="trigger-word" title={word}>
        {word}
      </span>
      <span className="trd-mctl__runs trd-mono" data-testid="trigger-plan">
        {planText(tr)}
      </span>
      {tr.status === 'awaiting_approval' ? (
        <span className="trd-mctl__hint">{t('trading.trigger.review')}</span>
      ) : null}
      <TriggerControls
        trigger={tr}
        busy={busy}
        onPause={onPause}
        onResume={onResume}
        onFire={onFire}
        onStop={onStop}
      />
    </div>
  )
}

/** "Paused by you" for the operator's own bare pause or stop; the engine's words otherwise. */
export function bracketTitle(b: Bracket): string | null {
  const reason = bracketReason(b)
  if (reason !== 'user') return reason
  return b.status === 'paused'
    ? t('trading.trigger.reason.pausedByYou')
    : t('trading.trigger.reason.byYou')
}

/**
 * One bracket among the missions (docs/brackets.md): a take-profit and a
 * stop-loss on one position, steered as one thing. Its name, what it is doing
 * ("Armed · ETH $3,790 · +20 % / −10 %"), its range, and the controls its
 * state allows. Sell now (Notify now for a range alert) and Stop act on a
 * second click, inline: one trades at once, the other is final.
 */
function BracketRow({
  bracket: b,
  busy,
  onPause,
  onResume,
  onFire,
  onStop,
  elsewhere = null,
  onOpenSession,
}: {
  bracket: Bracket
  busy: boolean
  onPause?: (b: Bracket) => void
  onResume?: (b: Bracket) => void
  onFire?: (b: Bracket) => void
  onStop?: (b: Bracket) => void
  elsewhere?: string | null
  onOpenSession?: (sessionKey: string) => void
}) {
  const reason = bracketTitle(b)
  const word = bracketWord(b)
  return (
    <div
      className="trd-mctl__row"
      data-kind="bracket"
      data-action={b.kind}
      data-state={b.status}
      data-near={bracketNear(b) || undefined}
      data-testid="bracket-row"
      data-bracket={b.id}
      title={reason ?? undefined}
    >
      <span className="trd-mctl__kind" data-action="bracket" aria-hidden>
        {t(`trading.bracket.tag.${b.kind}`)}
      </span>
      <span className="trd-mctl__name" title={reason ? `${b.name} · ${reason}` : b.name}>
        {b.name}
      </span>
      <Elsewhere sessionKey={elsewhere} onOpen={onOpenSession} />
      <span className="trd-mctl__word" data-testid="bracket-word" title={word}>
        {word}
      </span>
      <span className="trd-mctl__runs trd-mono" data-testid="bracket-plan">
        {bracketPlanText(b)}
      </span>
      {b.status === 'awaiting_approval' ? (
        <span className="trd-mctl__hint">{t('trading.trigger.review')}</span>
      ) : null}
      <BracketControls
        bracket={b}
        busy={busy}
        onPause={onPause}
        onResume={onResume}
        onFire={onFire}
        onStop={onStop}
      />
    </div>
  )
}

/**
 * The controls a DCA mandate's state allows — Pause or Resume, Buy now,
 * Edit (when the caller has a form to open), Stop — shared by the Missions
 * row above the composer and the BOOK's Automations tab, so the same
 * mandate is steered the same way everywhere. Nothing for a pending or a
 * finished one. Stop is final, so it asks for a second click — inline,
 * never a dialog.
 */
export function MandateControls({
  mandate: m,
  busy,
  onPause,
  onResume,
  onRun,
  onEdit,
  onStop,
}: {
  mandate: Mandate
  busy: boolean
  onPause?: (m: Mandate) => void
  onResume?: (m: Mandate) => void
  onRun?: (m: Mandate) => void
  onEdit?: (m: Mandate) => void
  onStop?: (m: Mandate) => void
}) {
  const [armed, setArmed] = useArm(`${m.status}:${busy}`)
  if (m.status !== 'active' && m.status !== 'paused') return null
  return (
    <span className="trd-mctl__ctl">
      {m.status === 'active' ? (
        <Button
          variant="ghost"
          size="icon"
          disabled={busy}
          aria-label={t('trading.dca.pause')}
          title={t('trading.dca.pause')}
          onClick={() => onPause?.(m)}
          data-testid="mandate-pause"
        >
          <Pause className="size-3.5" strokeWidth={1.75} aria-hidden />
        </Button>
      ) : (
        <Button
          variant="ghost"
          size="icon"
          disabled={busy}
          aria-label={t('trading.dca.resume')}
          title={t('trading.dca.resume')}
          onClick={() => onResume?.(m)}
          data-testid="mandate-resume"
        >
          <Play className="size-3.5" strokeWidth={1.75} aria-hidden />
        </Button>
      )}
      <Button
        variant="ghost"
        size="icon"
        disabled={busy}
        aria-label={t('trading.dca.runNow')}
        title={t('trading.dca.runNow')}
        onClick={() => onRun?.(m)}
        data-testid="mandate-run"
      >
        <Zap className="size-3.5" strokeWidth={1.75} aria-hidden />
      </Button>
      {onEdit ? (
        <Button
          variant="ghost"
          size="icon"
          disabled={busy}
          aria-label={t('trading.dca.edit')}
          title={t('trading.dca.edit')}
          onClick={() => onEdit(m)}
          data-testid="mandate-edit"
        >
          <Pencil className="size-3.5" strokeWidth={1.75} aria-hidden />
        </Button>
      ) : null}
      {armed ? (
        <button
          type="button"
          className="trd-mctl__confirm app-no-drag"
          disabled={busy}
          onClick={() => {
            setArmed(false)
            onStop?.(m)
          }}
          data-testid="mandate-stop"
          data-armed
        >
          {t('trading.dca.stopAgain')}
        </button>
      ) : (
        <Button
          variant="ghost"
          size="icon"
          disabled={busy}
          aria-label={t('trading.dca.stop')}
          title={t('trading.dca.stop')}
          onClick={() => setArmed(true)}
          data-testid="mandate-stop"
        >
          <OctagonX className="size-3.5" strokeWidth={1.75} aria-hidden />
        </Button>
      )}
    </span>
  )
}

/**
 * The controls a trigger's state allows: Pause or Resume and Fire now while
 * armed or paused, Stop also while triggered (an order is open). Fire now
 * and Stop act on a second click — inline: one trades at once, the other is
 * final. Shared by the Missions row and the Automations tab.
 */
export function TriggerControls({
  trigger: tr,
  busy,
  onPause,
  onResume,
  onFire,
  onStop,
}: {
  trigger: Trigger
  busy: boolean
  onPause?: (tr: Trigger) => void
  onResume?: (tr: Trigger) => void
  onFire?: (tr: Trigger) => void
  onStop?: (tr: Trigger) => void
}) {
  const settle = `${tr.status}:${busy}`
  const [stopArmed, setStopArmed] = useArm(settle)
  const [fireArmed, setFireArmed] = useArm(settle)
  const steerable = isSteerableTrigger(tr)
  const stoppable = steerable || tr.status === 'triggered'
  const fireLabel = t(fireNowKey(tr))
  if (!stoppable) return null
  return (
    <span className="trd-mctl__ctl">
      {steerable ? (
        <>
          {tr.status === 'armed' ? (
            <Button
              variant="ghost"
              size="icon"
              disabled={busy}
              aria-label={t('trading.trigger.pause')}
              title={t('trading.trigger.pause')}
              onClick={() => onPause?.(tr)}
              data-testid="trigger-pause"
            >
              <Pause className="size-3.5" strokeWidth={1.75} aria-hidden />
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="icon"
              disabled={busy}
              aria-label={t('trading.trigger.resume')}
              title={t('trading.trigger.resume')}
              onClick={() => onResume?.(tr)}
              data-testid="trigger-resume"
            >
              <Play className="size-3.5" strokeWidth={1.75} aria-hidden />
            </Button>
          )}
          {fireArmed ? (
            <button
              type="button"
              className="trd-mctl__confirm app-no-drag"
              data-tone="fire"
              disabled={busy}
              onClick={() => {
                setFireArmed(false)
                onFire?.(tr)
              }}
              data-testid="trigger-fire"
              data-armed
            >
              {`${fireLabel} ${t('trading.trigger.fireAgain')}`}
            </button>
          ) : (
            <Button
              variant="ghost"
              size="icon"
              disabled={busy}
              aria-label={fireLabel}
              title={fireLabel}
              onClick={() => {
                setStopArmed(false)
                setFireArmed(true)
              }}
              data-testid="trigger-fire"
            >
              <Zap className="size-3.5" strokeWidth={1.75} aria-hidden />
            </Button>
          )}
        </>
      ) : null}
      {stopArmed ? (
        <button
          type="button"
          className="trd-mctl__confirm app-no-drag"
          disabled={busy}
          onClick={() => {
            setStopArmed(false)
            onStop?.(tr)
          }}
          data-testid="trigger-stop"
          data-armed
        >
          {t('trading.trigger.stopAgain')}
        </button>
      ) : (
        <Button
          variant="ghost"
          size="icon"
          disabled={busy}
          aria-label={t('trading.trigger.stop')}
          title={t('trading.trigger.stop')}
          onClick={() => {
            setFireArmed(false)
            setStopArmed(true)
          }}
          data-testid="trigger-stop"
        >
          <OctagonX className="size-3.5" strokeWidth={1.75} aria-hidden />
        </Button>
      )}
    </span>
  )
}

/** The bracket's controls: both legs steered as one thing, as TriggerControls. */
export function BracketControls({
  bracket: b,
  busy,
  onPause,
  onResume,
  onFire,
  onStop,
}: {
  bracket: Bracket
  busy: boolean
  onPause?: (b: Bracket) => void
  onResume?: (b: Bracket) => void
  onFire?: (b: Bracket) => void
  onStop?: (b: Bracket) => void
}) {
  const settle = `${b.status}:${busy}`
  const [stopArmed, setStopArmed] = useArm(settle)
  const [fireArmed, setFireArmed] = useArm(settle)
  const steerable = isSteerableBracket(b)
  const stoppable = steerable || b.status === 'triggered'
  const fireLabel = t(bracketFireKey(b))
  if (!stoppable) return null
  return (
    <span className="trd-mctl__ctl">
      {steerable ? (
        <>
          {b.status === 'armed' ? (
            <Button
              variant="ghost"
              size="icon"
              disabled={busy}
              aria-label={t('trading.trigger.pause')}
              title={t('trading.trigger.pause')}
              onClick={() => onPause?.(b)}
              data-testid="bracket-pause"
            >
              <Pause className="size-3.5" strokeWidth={1.75} aria-hidden />
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="icon"
              disabled={busy}
              aria-label={t('trading.trigger.resume')}
              title={t('trading.trigger.resume')}
              onClick={() => onResume?.(b)}
              data-testid="bracket-resume"
            >
              <Play className="size-3.5" strokeWidth={1.75} aria-hidden />
            </Button>
          )}
          {fireArmed ? (
            <button
              type="button"
              className="trd-mctl__confirm app-no-drag"
              data-tone="fire"
              disabled={busy}
              onClick={() => {
                setFireArmed(false)
                onFire?.(b)
              }}
              data-testid="bracket-fire"
              data-armed
            >
              {`${fireLabel} ${t('trading.trigger.fireAgain')}`}
            </button>
          ) : (
            <Button
              variant="ghost"
              size="icon"
              disabled={busy}
              aria-label={fireLabel}
              title={fireLabel}
              onClick={() => {
                setStopArmed(false)
                setFireArmed(true)
              }}
              data-testid="bracket-fire"
            >
              <Zap className="size-3.5" strokeWidth={1.75} aria-hidden />
            </Button>
          )}
        </>
      ) : null}
      {stopArmed ? (
        <button
          type="button"
          className="trd-mctl__confirm app-no-drag"
          disabled={busy}
          onClick={() => {
            setStopArmed(false)
            onStop?.(b)
          }}
          data-testid="bracket-stop"
          data-armed
        >
          {t('trading.trigger.stopAgain')}
        </button>
      ) : (
        <Button
          variant="ghost"
          size="icon"
          disabled={busy}
          aria-label={t('trading.trigger.stop')}
          title={t('trading.trigger.stop')}
          onClick={() => {
            setFireArmed(false)
            setStopArmed(true)
          }}
          data-testid="bracket-stop"
        >
          <OctagonX className="size-3.5" strokeWidth={1.75} aria-hidden />
        </Button>
      )}
    </span>
  )
}
