import { useQuery } from '@tanstack/react-query'
import {
  Bot,
  ChevronDown,
  ExternalLink,
  MessageSquare,
  Pause,
  Play,
  Trash2,
  Zap,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { useRpc } from '@/app/providers'
import type { RawJob, RawRun } from '@/views/cron/logic'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useBrackets, useMandates, useTriggers } from '~/stores/trading'
import { clockLabel } from '~/views/jobs/logic'
import { Empty, Skeleton } from '../parts'
import type { Bracket, Mandate, Trigger } from '../types'
import {
  AUTOMATION_GROUPS,
  automationCounts,
  automationElsewhere,
  automationItems,
  automationSections,
  FINISHED_SHOWN,
  itemHistory,
  type AutomationFilter,
  type AutomationGroup,
  type AutomationItem,
  type HistoryRow,
} from './automations-logic'
import { bracketPlanText, bracketWord } from './bracket-logic'
import { missionStatus } from './desk-logic'
import {
  mandateBuysText,
  mandateMissesText,
  mandateProgressText,
  mandateReason,
} from './mandate-logic'
import {
  BracketControls,
  Elsewhere,
  MandateControls,
  mandateWord,
  missionWord,
  TriggerControls,
  useMandateClock,
} from './MissionControls'
import type { MissionsApi } from './missions'
import { useAllJobs } from './missions'
import { planText, triggerReason, triggerWord } from './trigger-logic'

/** History rows drawn before "+N earlier". */
const HISTORY_ROWS = 5

/**
 * The BOOK's Automations tab: every mission, DCA mandate, price trigger and
 * bracket in one place, grouped by what the user has to do about it —
 * running, needs you, paused, finished — each with the same controls as its
 * row above the composer and, opened, the history the engine kept for it.
 *
 * The lists come from the same cache entries the Missions row fills
 * (`trading.*.list {all:true}`, `cron.list`), so the tab costs no second
 * request. The mission writes go through the desk's one `useMissions`.
 */
export function Automations({
  missions,
  sessionKey,
  pendingApprovals,
  enabled,
  onOpenSession,
}: {
  missions: MissionsApi
  sessionKey: string
  pendingApprovals: number
  enabled: boolean
  onOpenSession: (sessionKey: string) => void
}) {
  const jobs = useAllJobs(enabled)
  const mandates = useMandates(true, enabled)
  const triggers = useTriggers(true, enabled)
  const brackets = useBrackets(true, enabled)
  const [filter, setFilter] = useState<AutomationFilter>('all')
  const items = useMemo(
    () =>
      automationItems({
        jobs: jobs.data ?? [],
        running: missions.running,
        pendingApprovals,
        mandates: mandates.mandates,
        triggers: triggers.triggers,
        brackets: brackets.brackets,
      }),
    [
      jobs.data,
      missions.running,
      pendingApprovals,
      mandates.mandates,
      triggers.triggers,
      brackets.brackets,
    ],
  )
  const counts = automationCounts(items)
  const sections = automationSections(items, filter)
  const now = useMandateClock(mandates.mandates)
  const loading =
    items.length === 0 &&
    (jobs.isPending || mandates.isPending || triggers.isPending || brackets.isPending)

  if (loading) {
    return (
      <div className="trd-auto" data-testid="automations" aria-busy>
        {[0, 1, 2].map((i) => (
          <div key={i} className="trd-auto__item">
            <Skeleton width="66%" />
            <Skeleton width="45%" />
          </div>
        ))}
      </div>
    )
  }
  if (items.length === 0) {
    return (
      <Empty
        icon={<Bot className="size-5" strokeWidth={1.5} aria-hidden />}
        title={t('trading.auto.empty.title')}
        body={t('trading.auto.empty.body')}
      />
    )
  }
  return (
    <div className="trd-auto" data-testid="automations">
      <div className="trd-auto__filters" role="radiogroup" aria-label={t('trading.auto.title')}>
        {(['all', ...AUTOMATION_GROUPS] as const).map((f) => (
          <button
            key={f}
            type="button"
            role="radio"
            aria-checked={filter === f}
            className="trd-chip app-no-drag"
            aria-pressed={filter === f}
            disabled={f !== 'all' && counts[f] === 0}
            onClick={() => setFilter(f)}
            data-testid={`auto-filter-${f}`}
          >
            <span className="trd-chip__label">
              {f === 'all' ? t('trading.auto.filter.all') : t(`trading.auto.group.${f}`)}
            </span>
            <span className="trd-chip__count">{counts[f]}</span>
          </button>
        ))}
      </div>
      {sections.length === 0 ? (
        <p className="trd-auto__none">{t('trading.auto.empty.filtered')}</p>
      ) : null}
      {sections.map((section) => (
        <Section
          key={section.group}
          group={section.group}
          items={section.items}
          folded={section.group === 'finished' && filter === 'all'}
          now={now}
          missions={missions}
          pendingApprovals={pendingApprovals}
          sessionKey={sessionKey}
          onOpenSession={onOpenSession}
        />
      ))}
    </div>
  )
}

/**
 * One group: its header with a count, its items. The finished group, among
 * the others, shows its newest few and folds the rest behind "+N earlier";
 * filtered to it, everything is drawn.
 */
function Section({
  group,
  items,
  folded,
  now,
  missions,
  pendingApprovals,
  sessionKey,
  onOpenSession,
}: {
  group: AutomationGroup
  items: AutomationItem[]
  folded: boolean
  now: number
  missions: MissionsApi
  pendingApprovals: number
  sessionKey: string
  onOpenSession: (sessionKey: string) => void
}) {
  const [all, setAll] = useState(false)
  const shown = folded && !all ? items.slice(0, FINISHED_SHOWN) : items
  const more = folded ? items.length - FINISHED_SHOWN : 0
  return (
    <section className="trd-auto__section" data-group={group}>
      <h4 className="trd-auto__group" data-testid={`auto-group-${group}`}>
        {t(`trading.auto.group.${group}`)}
        <span className="trd-auto__groupcount">{items.length}</span>
      </h4>
      <ul className="trd-auto__list">
        {shown.map((item) => (
          <Item
            key={`${item.kind}:${item.id}`}
            item={item}
            now={now}
            missions={missions}
            pendingApprovals={pendingApprovals}
            elsewhere={automationElsewhere(item, sessionKey)}
            onOpenSession={onOpenSession}
          />
        ))}
      </ul>
      {more > 0 ? (
        <button
          type="button"
          className="trd-mctl__more trd-auto__more app-no-drag"
          aria-expanded={all}
          onClick={() => setAll((v) => !v)}
          data-testid={`auto-more-${group}`}
        >
          {all
            ? t('trading.auto.history.fewer')
            : t('trading.auto.history.more').replace('{count}', String(more))}
        </button>
      ) : null}
    </section>
  )
}

/**
 * Why a row is where it is, in words. The engine's reason as it said it; a
 * bare operator action by what it was ("Paused by you", "Rejected by you",
 * "Stopped by you"); nothing for a live row.
 */
function reasonText(status: string, reason: string | null): string | null {
  if (reason !== 'user') return reason
  if (status === 'paused') return t('trading.dca.reason.pausedByYou')
  if (status === 'rejected') return t('trading.auto.reason.rejectedByYou')
  return t('trading.dca.reason.byYou')
}

/** The state word, the figures and the reason an item's row says, by kind. */
function describe(
  item: AutomationItem,
  now: number,
  ctx: { running: boolean; pendingApprovals: number },
): { tag: string; action: string; word: string; facts: string[]; reason: string | null } {
  switch (item.kind) {
    case 'mandate': {
      const m = item.mandate
      const misses = mandateMissesText(m)
      return {
        tag: t('trading.dca.kind'),
        action: 'dca',
        word: mandateWord(m, now),
        facts: [mandateProgressText(m), `${mandateBuysText(m)}${misses ? ` ${misses}` : ''}`],
        reason: reasonText(m.status, mandateReason(m)),
      }
    }
    case 'trigger': {
      const tr = item.trigger
      return {
        tag: t(`trading.trigger.tag.${tr.kind}`),
        action: tr.kind,
        word: triggerWord(tr),
        facts: [planText(tr)],
        reason: reasonText(tr.status, triggerReason(tr)),
      }
    }
    case 'bracket': {
      const b = item.bracket
      return {
        tag: t(`trading.bracket.tag.${b.kind}`),
        action: 'bracket',
        word: bracketWord(b),
        facts: [bracketPlanText(b)],
        reason: reasonText(b.status, triggerReason(b)),
      }
    }
    default: {
      const job = item.job
      const s = missionStatus(job, ctx)
      const facts: string[] = []
      const next = job.next_run ? Date.parse(String(job.next_run)) : NaN
      if (job.enabled !== false && Number.isFinite(next))
        facts.push(`${t('trading.auto.mission.next')} ${clockLabel(new Date(next), now)}`)
      const runs = Number(job.run_count ?? 0)
      if (runs > 0) facts.push(t('trading.auto.mission.runs').replace('{count}', String(runs)))
      return {
        tag: t('trading.auto.kind.mission'),
        action: 'mission',
        word: missionWord(s.state, s.until),
        facts,
        reason: null,
      }
    }
  }
}

function Item({
  item,
  now,
  missions,
  pendingApprovals,
  elsewhere,
  onOpenSession,
}: {
  item: AutomationItem
  now: number
  missions: MissionsApi
  pendingApprovals: number
  elsewhere: string | null
  onOpenSession: (sessionKey: string) => void
}) {
  const [open, setOpen] = useState(false)
  const running = item.kind === 'mission' && missions.running.has(item.id)
  const d = describe(item, now, { running, pendingApprovals })
  const state =
    item.kind === 'mission'
      ? item.job.enabled === false
        ? 'paused'
        : missionStatus(item.job, { running, pendingApprovals }).state
      : item.kind === 'mandate'
        ? item.mandate.status
        : item.kind === 'trigger'
          ? item.trigger.status
          : item.bracket.status
  return (
    <li
      className="trd-auto__item"
      data-kind={item.kind}
      data-action={d.action}
      data-state={state}
      data-group={item.group}
      data-open={open || undefined}
      data-testid="auto-item"
      data-id={item.id}
    >
      <button
        type="button"
        className="trd-auto__head app-no-drag"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        data-testid="auto-toggle"
      >
        <span className="trd-auto__light" aria-hidden />
        <span className="trd-mctl__kind" data-action={d.action} aria-hidden>
          {d.tag}
        </span>
        <span className="trd-auto__name" title={d.reason ? `${d.word} · ${d.reason}` : d.word}>
          {itemName(item)}
        </span>
        <ChevronDown className="trd-auto__chev size-3.5" strokeWidth={1.75} aria-hidden />
      </button>
      <div className="trd-auto__word" data-testid="auto-word">
        {d.word}
      </div>
      {d.facts.length ? (
        <div className="trd-auto__facts trd-mono" data-testid="auto-facts">
          {d.facts.filter(Boolean).join(' · ')}
        </div>
      ) : null}
      {d.reason ? (
        <div className="trd-auto__reason" data-testid="auto-reason">
          {d.reason}
        </div>
      ) : null}
      <div className="trd-auto__foot">
        <Elsewhere sessionKey={elsewhere} onOpen={onOpenSession} />
        <Controls item={item} missions={missions} running={running} />
      </div>
      {open ? <HistoryList item={item} /> : null}
    </li>
  )
}

function itemName(item: AutomationItem): string {
  switch (item.kind) {
    case 'mandate':
      return item.mandate.name
    case 'trigger':
      return item.trigger.name
    case 'bracket':
      return item.bracket.name
    default:
      return item.job.name || item.id
  }
}

/** The same controls as the row above the composer; a mission's are the scheduler's. */
function Controls({
  item,
  missions,
  running,
}: {
  item: AutomationItem
  missions: MissionsApi
  running: boolean
}) {
  switch (item.kind) {
    case 'mandate':
      return (
        <MandateControls
          mandate={item.mandate}
          busy={missions.mandate.pending === item.id}
          onPause={(m: Mandate) => void missions.mandate.pause(m)}
          onResume={(m: Mandate) => void missions.mandate.resume(m)}
          onRun={(m: Mandate) => void missions.mandate.run(m)}
          onStop={(m: Mandate) => void missions.mandate.stop(m)}
        />
      )
    case 'trigger':
      return (
        <TriggerControls
          trigger={item.trigger}
          busy={missions.trigger.pending === item.id}
          onPause={(tr: Trigger) => void missions.trigger.pause(tr)}
          onResume={(tr: Trigger) => void missions.trigger.resume(tr)}
          onFire={(tr: Trigger) => void missions.trigger.fire(tr)}
          onStop={(tr: Trigger) => void missions.trigger.stop(tr)}
        />
      )
    case 'bracket':
      return (
        <BracketControls
          bracket={item.bracket}
          busy={missions.bracket.pending === item.id}
          onPause={(b: Bracket) => void missions.bracket.pause(b)}
          onResume={(b: Bracket) => void missions.bracket.resume(b)}
          onFire={(b: Bracket) => void missions.bracket.fire(b)}
          onStop={(b: Bracket) => void missions.bracket.stop(b)}
        />
      )
    default:
      return <MissionButtons job={item.job} missions={missions} running={running} />
  }
}

function MissionButtons({
  job,
  missions,
  running,
}: {
  job: RawJob
  missions: MissionsApi
  running: boolean
}) {
  const enabled = job.enabled !== false
  const busy = missions.busy
  return (
    <span className="trd-mctl__ctl">
      {enabled ? (
        <Button
          variant="ghost"
          size="icon"
          disabled={busy || running}
          aria-label={t('trading.mission.stop')}
          title={t('trading.mission.stop')}
          onClick={() => missions.setEnabled(job, false)}
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
          onClick={() => missions.setEnabled(job, true)}
          data-testid="mission-continue"
        >
          <Play className="size-3.5" strokeWidth={1.75} aria-hidden />
        </Button>
      )}
      <Button
        variant="ghost"
        size="icon"
        disabled={busy || running}
        aria-label={t('trading.mission.runNow')}
        title={t('trading.mission.runNow')}
        onClick={() => missions.runNow(job)}
        data-testid="mission-run"
      >
        <Zap className="size-3.5" strokeWidth={1.75} aria-hidden />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        disabled={busy || running}
        aria-label={t('trading.auto.mission.remove')}
        title={t('trading.auto.mission.remove')}
        onClick={() => missions.remove(job)}
        data-testid="mission-remove"
      >
        <Trash2 className="size-3.5" strokeWidth={1.75} aria-hidden />
      </Button>
    </span>
  )
}

/**
 * The history an item kept: a DCA's buys, a trigger's or bracket's fires
 * (carried in the list payload), a mission's runs (`cron.runs`, fetched
 * when the item is opened). Newest first; five, then "+N earlier".
 */
function HistoryList({ item }: { item: AutomationItem }) {
  const rpc = useRpc()
  const [all, setAll] = useState(false)
  const runs = useQuery<RawRun[]>({
    queryKey: ['trading', 'auto', 'runs', item.id],
    enabled: item.kind === 'mission',
    queryFn: async () => {
      const data = await rpc.call<RawRun[] | { runs?: RawRun[] }>('cron.runs', {
        id: item.id,
        limit: 20,
      })
      return Array.isArray(data) ? data : (data.runs ?? [])
    },
    staleTime: 15_000,
  })
  const rows = itemHistory(item, runs.data)
  const shown = all ? rows : rows.slice(0, HISTORY_ROWS)
  const more = rows.length - HISTORY_ROWS
  return (
    <div className="trd-auto__hist" data-testid="auto-history">
      <div className="trd-auto__histhead">{t('trading.auto.history')}</div>
      {item.kind === 'mission' && runs.isPending ? (
        <p className="trd-auto__none">{t('trading.auto.history.loading')}</p>
      ) : rows.length === 0 ? (
        <p className="trd-auto__none">{t('trading.auto.history.none')}</p>
      ) : (
        <ol className="trd-auto__runs">
          {shown.map((row) => (
            <Run key={row.key} row={row} chat={item.kind === 'mission' ? runs.data : undefined} />
          ))}
        </ol>
      )}
      {more > 0 ? (
        <button
          type="button"
          className="trd-mctl__more app-no-drag"
          aria-expanded={all}
          onClick={() => setAll((v) => !v)}
          data-testid="auto-history-more"
        >
          {all
            ? t('trading.auto.history.fewer')
            : t('trading.auto.history.more').replace('{count}', String(more))}
        </button>
      ) : null}
    </div>
  )
}

function Run({ row, chat }: { row: HistoryRow; chat?: RawRun[] }) {
  const at = Date.parse(row.at)
  // A mission run's chat: the run that made this row, by its id in the key.
  const run = chat?.find((r) => row.key.endsWith(`:${String(r.id)}`))
  const chatKey = run?.chatAvailable !== false && run?.sessionKey ? run.sessionKey : null
  return (
    <li className="trd-auto__run" data-tone={row.tone} data-testid="auto-run">
      <span className="trd-auto__runlight" aria-hidden />
      <span className="trd-auto__runat trd-mono">
        {Number.isFinite(at) ? clockLabel(new Date(at)) : ''}
      </span>
      <span className="trd-auto__runword">
        {row.leg ? `${row.leg} · ` : ''}
        {t(row.wordKey)}
        {row.manual ? (
          <span className="trd-auto__byhand"> · {t('trading.auto.byHand')}</span>
        ) : null}
      </span>
      {row.figures ? <span className="trd-auto__runfig trd-mono">{row.figures}</span> : null}
      {row.reason ? (
        <span className="trd-auto__runreason" title={row.reason}>
          {row.reason}
        </span>
      ) : null}
      {row.explorerUrl ? (
        <a
          className="trd-auto__runlink"
          href={row.explorerUrl}
          target="_blank"
          rel="noreferrer"
          aria-label={t('trading.auto.openTx')}
          title={t('trading.auto.openTx')}
        >
          <ExternalLink className="size-3" strokeWidth={1.75} aria-hidden />
        </a>
      ) : null}
      {chatKey ? (
        <a
          className="trd-auto__runlink"
          href={`#/sessions/${encodeURIComponent(chatKey)}`}
          aria-label={t('trading.auto.openChat')}
          title={t('trading.auto.openChat')}
        >
          <MessageSquare className="size-3" strokeWidth={1.75} aria-hidden />
        </a>
      ) : null}
    </li>
  )
}
