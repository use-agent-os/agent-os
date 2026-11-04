import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { useConnection } from '@/stores/connection'
import type { RawJob, RawRun } from '@/views/cron/logic'
import { t } from '~/i18n'
import { useNow } from '~/lib/use-now'
import {
  useBracketActions,
  useBrackets,
  useMandateActions,
  useMandates,
  useTriggerActions,
  useTriggers,
  type BracketActions,
  type MandateActions,
  type TriggerActions,
} from '~/stores/trading'
import { errorText } from '../logic'
import type { Bracket, Mandate, Trigger } from '../types'
import { deskBrackets } from './bracket-logic'
import {
  isDeskMission,
  isSessionMission,
  jobText,
  missionCronPayload,
  missionStopDue,
  needsFullOutput,
  runSaysComplete,
  withoutDryRun,
  type MissionForm,
} from './desk-logic'
import { deskMandates } from './mandate-logic'
import { deskTriggers } from './trigger-logic'

/**
 * Missions are cron jobs that post into the desk's chat. This hook reads
 * them back, follows their runs over the cron events, and offers the
 * controls above the composer. The engine's scheduler is the source of
 * truth for every state word shown.
 *
 * DCA mandates (docs/dca.md) sit beside them: the engine runs those itself,
 * so the desk only lists them and forwards the user's controls. Price
 * triggers (docs/triggers.md) are listed the same way, and so are brackets
 * (docs/brackets.md): one row per bracket, never one per leg.
 */

export const MISSIONS_KEY = ['trading', 'missions'] as const

interface CronListResult {
  jobs?: RawJob[]
}

interface CronRunsResult {
  runs?: RawRun[]
}

interface RunOutputResult {
  output?: string
}

type Rpc = ReturnType<typeof useRpc>

/**
 * Whether the job's last recorded run declared the mission complete. Both
 * the live `cron.run.finished` payload and `cron.runs` carry only the first
 * 500 characters of the agent's reply, while the marker is instructed to be
 * the last thing it says — so a talkative run is settled against the full
 * text from `cron.runOutput`.
 */
async function lastRunComplete(rpc: Rpc, jobId: string): Promise<boolean> {
  const data = await rpc.call<RawRun[] | CronRunsResult>('cron.runs', { id: jobId, limit: 1 })
  const run = (Array.isArray(data) ? data : (data.runs ?? []))[0]
  if (!run) return false
  if (runSaysComplete(run.summary)) return true
  if (!needsFullOutput(run)) return false
  // The handler names the job `id` (rpc_cron.py: `params.get("id") or
  // params.get("job_id")`); `jobId` is not read and the call was refused.
  const full = await rpc.call<RunOutputResult>('cron.runOutput', { id: jobId, runId: run.id })
  return runSaysComplete(full?.output)
}

interface RunStartPayload {
  jobId?: string
}

interface RunFinishedPayload {
  jobId?: string
  success?: boolean
  summary?: string | null
  sessionKey?: string
}

export interface MissionsApi {
  missions: RawJob[]
  loading: boolean
  /** Job ids with a run in flight right now. */
  running: ReadonlySet<string>
  /** The job, or null when the gateway refused (already toasted). */
  create: (form: MissionForm, prompt: string) => Promise<RawJob | null>
  /** True when saved; false when the gateway refused (already toasted). */
  update: (id: string, patch: Record<string, unknown>) => Promise<boolean>
  setEnabled: (job: RawJob, enabled: boolean) => void
  runNow: (job: RawJob) => void
  remove: (job: RawJob) => void
  busy: boolean
  /** The DCA mandates listed here: every live one, and this desk's finished within the hour. */
  mandates: Mandate[]
  /** Every mandate awaiting the operator's approval, whichever chat proposed it. */
  awaitingMandates: Mandate[]
  /** The mandate controls (approve, pause, buy now, update …), toasting their own outcome. */
  mandate: MandateActions
  /** The price triggers listed here: every live one, and this desk's finished within the hour. */
  triggers: Trigger[]
  /** Every trigger awaiting the operator's approval, whichever chat proposed it. */
  awaitingTriggers: Trigger[]
  /** The trigger controls (approve, pause, fire now, stop …), toasting their own outcome. */
  trigger: TriggerActions
  /** The brackets listed here: every live one, and this desk's finished within the hour. */
  brackets: Bracket[]
  /** Every bracket awaiting the operator's approval, whichever chat proposed it. */
  awaitingBrackets: Bracket[]
  /** The bracket controls (approve, pause, sell now, stop …): one write for both legs. */
  bracket: BracketActions
}

/**
 * Every cron job, from the same cache entry `useMissions` fills: the BOOK's
 * Automations tab filters it to the trading desk's own. Reading the key
 * again costs no second request and binds no second event listener.
 */
export function useAllJobs(enabled = true) {
  const rpc = useRpc()
  const connected = useConnection((s) => s.state === 'connected')
  return useQuery<RawJob[]>({
    queryKey: MISSIONS_KEY,
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<RawJob[] | CronListResult>('cron.list', {})
      return Array.isArray(data) ? data : (data.jobs ?? [])
    },
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  })
}

export function useMissions(sessionKey: string, enabled = true): MissionsApi {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const connected = useConnection((s) => s.state === 'connected')
  const [running, setRunning] = useState<ReadonlySet<string>>(() => new Set())

  const query = useQuery<RawJob[]>({
    queryKey: MISSIONS_KEY,
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<RawJob[] | CronListResult>('cron.list', {})
      return Array.isArray(data) ? data : (data.jobs ?? [])
    },
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  })

  // Listed: this session's missions, and the enabled ones of every other
  // trading chat — the scheduler keeps running a mission after its chat is
  // left, and a desk that hid it would leave it trading unseen. Only this
  // session's are settled below: another desk's mission is its own to stop.
  const missions = useMemo(
    () => (query.data ?? []).filter((job) => isDeskMission(job, sessionKey)),
    [query.data, sessionKey],
  )
  const ownMissions = useMemo(
    () => missions.filter((job) => isSessionMission(job, sessionKey)),
    [missions, sessionKey],
  )

  // Every mandate, so a DCA that just finished (completed, stopped, rejected
  // or expired) still shows how it ended for an hour; the minute clock only
  // ages that window, the rows tick on their own.
  const allMandates = useMandates(true, enabled)
  const mandateActions = useMandateActions()
  const minute = useNow(enabled ? 60_000 : 0)
  const mandates = useMemo(
    () => deskMandates(allMandates.mandates, sessionKey, minute),
    [allMandates.mandates, sessionKey, minute],
  )
  const awaitingMandates = useMemo(
    () => allMandates.mandates.filter((m) => m.status === 'awaiting_approval'),
    [allMandates.mandates],
  )
  // Triggers the same way: every one, so a fired or stopped trigger still
  // says how it ended for an hour.
  const allTriggers = useTriggers(true, enabled)
  const triggerActions = useTriggerActions()
  const triggers = useMemo(
    () => deskTriggers(allTriggers.triggers, sessionKey, minute),
    [allTriggers.triggers, sessionKey, minute],
  )
  const awaitingTriggers = useMemo(
    () => allTriggers.triggers.filter((tr) => tr.status === 'awaiting_approval'),
    [allTriggers.triggers],
  )

  // Brackets the same way: every one, so a bracket that took profit or was
  // stopped still says how it ended for an hour.
  const allBrackets = useBrackets(true, enabled)
  const bracketActions = useBracketActions()
  const brackets = useMemo(
    () => deskBrackets(allBrackets.brackets, sessionKey, minute),
    [allBrackets.brackets, sessionKey, minute],
  )
  const awaitingBrackets = useMemo(
    () => allBrackets.brackets.filter((b) => b.status === 'awaiting_approval'),
    [allBrackets.brackets],
  )

  const stopCompleted = useCallback(
    (id: string, why: 'complete' | 'stopRule' = 'complete') =>
      rpc
        .call('cron.update', { id, enabled: false })
        .then(() => {
          toast.success(
            why === 'stopRule'
              ? t('trading.mission.stopRuleReached')
              : t('trading.mission.completed'),
            { id: `mission-${id}` },
          )
          void queryClient.invalidateQueries({ queryKey: MISSIONS_KEY })
        })
        .catch(() => {}),
    [rpc, queryClient],
  )

  // Runs: start marks the job live, finished clears it and refreshes the
  // list (next_run moved). A mission that reports completion turns itself
  // off; a first run in dry-run mode drops the dry-run line for the next.
  // Bound once per desk (the frame owns this hook): a second listener
  // would send every `cron.update` twice.
  useEffect(() => {
    if (!enabled) return
    const invalidate = () => void queryClient.invalidateQueries({ queryKey: MISSIONS_KEY })
    const offStart = rpc.on('cron.run.start', (payload) => {
      const id = (payload as RunStartPayload | undefined)?.jobId
      if (!id) return
      setRunning((prev) => {
        if (prev.has(id)) return prev
        const next = new Set(prev)
        next.add(id)
        return next
      })
    })
    const offFinished = rpc.on('cron.run.finished', (payload) => {
      const p = (payload ?? {}) as RunFinishedPayload
      const id = p.jobId
      if (id) {
        setRunning((prev) => {
          if (!prev.has(id)) return prev
          const next = new Set(prev)
          next.delete(id)
          return next
        })
      }
      const job = (queryClient.getQueryData<RawJob[]>(MISSIONS_KEY) ?? []).find((j) => j.id === id)
      if (job && id && isSessionMission(job, sessionKey)) {
        if (runSaysComplete(p.summary)) {
          void stopCompleted(id)
        } else {
          // The preview may have cut the marker off; reconciliation below
          // settles that against the full output once the run is recorded.
          const text = jobText(job)
          const cleaned = withoutDryRun(text)
          if (cleaned !== text) void rpc.call('cron.update', { id, text: cleaned }).catch(() => {})
        }
      }
      invalidate()
    })
    const offHello = rpc.on('_hello', () => {
      setRunning(new Set())
      invalidate()
    })
    return () => {
      offStart()
      offFinished()
      offHello()
    }
  }, [rpc, queryClient, sessionKey, enabled, stopCompleted])

  // Reconciliation. Turning a finished mission off used to happen only in
  // the live `cron.run.finished` handler above, so a mission that reported
  // completion while this window was closed stayed enabled — and kept
  // spending. Settle every enabled mission against its last recorded run,
  // once per run: `run_count` moves after the run lands, so a job is checked
  // again only when there is something new to check.
  const settled = useRef<Set<string>>(new Set())
  // Stop-rule pauses already sent, one per (job, run count): the effect
  // re-runs before the list refetches, and a mission the user turns back on
  // is theirs until its next run moves the count again.
  const ruled = useRef<Set<string>>(new Set())
  useEffect(() => {
    if (!enabled || !connected) return
    let cancelled = false
    for (const job of ownMissions) {
      const id = job.id
      if (!id || job.enabled === false || running.has(id)) continue
      // "After N runs" and "after <date>" are prose to the agent; the desk
      // measures them itself, or a miscount keeps a mission spending.
      if (missionStopDue(job, Date.now())) {
        const stamp = `${id}:${String(job.run_count ?? '')}`
        if (ruled.current.has(stamp)) continue
        ruled.current.add(stamp)
        void stopCompleted(id, 'stopRule')
        continue
      }
      if (!job.last_run) continue
      const stamp = `${id}:${String(job.run_count ?? '')}:${String(job.last_run)}`
      if (settled.current.has(stamp)) continue
      settled.current.add(stamp)
      void lastRunComplete(rpc, id)
        .then((done) => {
          if (done && !cancelled) return stopCompleted(id)
        })
        // A failed lookup must not mark the run settled: try again next poll.
        .catch(() => settled.current.delete(stamp))
    }
    return () => {
      cancelled = true
    }
  }, [ownMissions, running, rpc, enabled, connected, stopCompleted])

  const create = useMutation({
    mutationFn: async ({ form, prompt }: { form: MissionForm; prompt: string }) => {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone
      return rpc.call<RawJob | { job?: RawJob }>(
        'cron.add',
        missionCronPayload(form, prompt, sessionKey, tz),
      )
    },
    onSettled: () => void queryClient.invalidateQueries({ queryKey: MISSIONS_KEY }),
  })
  const update = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Record<string, unknown> }) =>
      rpc.call('cron.update', { id, ...patch }),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: MISSIONS_KEY }),
  })
  const run = useMutation({
    mutationFn: (id: string) =>
      rpc.call<{ success?: boolean; status?: string; reason?: string; error?: string | null }>(
        'cron.run',
        { id },
      ),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: MISSIONS_KEY }),
  })
  const removeJob = useMutation({
    mutationFn: (id: string) => rpc.call('cron.remove', { id }),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: MISSIONS_KEY }),
  })

  const fail = (what: string, err: unknown) =>
    toast.error(`${what}: ${errorText(err)}`, { id: 'mission-err' })

  return {
    missions,
    mandates,
    awaitingMandates,
    mandate: mandateActions,
    triggers,
    awaitingTriggers,
    trigger: triggerActions,
    brackets,
    awaitingBrackets,
    bracket: bracketActions,
    loading: connected && query.isPending,
    running,
    busy: create.isPending || update.isPending || run.isPending || removeJob.isPending,
    create: async (form, prompt) => {
      try {
        const res = await create.mutateAsync({ form, prompt })
        toast.success(t('trading.mission.started'), { id: 'mission-create' })
        const job = (res && 'job' in res ? res.job : res) as RawJob | undefined
        return job ?? null
      } catch (err) {
        fail(t('trading.mission.failed'), err)
        return null
      }
    },
    update: async (id, patch) => {
      try {
        await update.mutateAsync({ id, patch })
        toast.success(t('trading.mission.updated'), { id: 'mission-update' })
        return true
      } catch (err) {
        fail(t('trading.mission.failed'), err)
        return false
      }
    },
    setEnabled: (job, enabled) => {
      if (!job.id) return
      update.mutate(
        { id: job.id, patch: { enabled } },
        {
          onSuccess: () =>
            toast.success(enabled ? t('trading.mission.resumed') : t('trading.mission.stopped'), {
              id: `mission-${job.id}`,
            }),
          onError: (err) => fail(t('trading.mission.failed'), err),
        },
      )
    },
    runNow: (job) => {
      if (!job.id) return
      run.mutate(job.id, {
        onSuccess: (res) => {
          // A refused run says why; a silent no-op is the one thing not allowed.
          if (res && (res.success === false || res.status === 'blocked' || res.error)) {
            toast.warning(
              `${t('trading.mission.runRefused')}: ${res.reason || res.error || res.status || ''}`,
              { id: `mission-${job.id}` },
            )
          } else toast.success(t('trading.mission.ran'), { id: `mission-${job.id}` })
        },
        onError: (err) => fail(t('trading.mission.failed'), err),
      })
    },
    remove: (job) => {
      if (!job.id) return
      removeJob.mutate(job.id, {
        onSuccess: () => toast.success(t('trading.mission.removed'), { id: `mission-${job.id}` }),
        onError: (err) => fail(t('trading.mission.failed'), err),
      })
    },
  }
}
