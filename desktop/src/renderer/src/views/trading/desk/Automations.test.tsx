import { readFileSync } from 'node:fs'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderDesk } from '../test-utils'
import type {
  Bracket,
  BracketPayload,
  Mandate,
  MandatePayload,
  Trigger,
  TriggerPayload,
} from '../types'
import { Automations } from './Automations'
import {
  automationCounts,
  automationElsewhere,
  automationItems,
  automationSections,
  bracketFireRows,
  isTradingJob,
  itemHistory,
  mandateRunRows,
  missionGroup,
  missionRunRows,
  triggerFireRows,
} from './automations-logic'
import type { MissionsApi } from './missions'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

const read = (name: string) =>
  JSON.parse(readFileSync(`src/renderer/src/views/trading/desk/__fixtures__/${name}`, 'utf8'))
const MANDATE = (read('dca/mandate.json') as MandatePayload).mandate
const TRIGGER = (read('trigger/trigger.json') as TriggerPayload).trigger
const BRACKET = (read('bracket/bracket.json') as BracketPayload).bracket
const SESSION = MANDATE.sessionKey as string
const OTHER = 'agent:trading:webchat:trading-other'

const mandate = (extra: Partial<Mandate> = {}): Mandate => ({ ...MANDATE, ...extra })
const trigger = (extra: Partial<Trigger> = {}): Trigger => ({ ...TRIGGER, ...extra })
const bracket = (extra: Partial<Bracket> = {}): Bracket => ({ ...BRACKET, ...extra })
const job = (extra: Record<string, unknown> = {}) => ({
  id: 'j1',
  name: 'Portfolio report',
  enabled: true,
  agentId: 'trading',
  targetSessionKey: SESSION,
  next_run: '2026-09-28T09:00:00Z',
  last_run: '2026-09-28T05:00:00Z',
  run_count: 3,
  ...extra,
})

describe('automations logic', () => {
  it('knows a trading job by its agent or its chat, never another agent’s', () => {
    expect(isTradingJob(job())).toBe(true)
    expect(isTradingJob(job({ agentId: 'main', targetSessionKey: OTHER }))).toBe(true)
    expect(isTradingJob(job({ agentId: 'main', targetSessionKey: 'agent:main:webchat:x' }))).toBe(
      false,
    )
  })

  it('groups by what the user has to do: running, needs you, paused, finished', () => {
    const items = automationItems({
      jobs: [
        job(),
        job({ id: 'j2', enabled: false }),
        job({ id: 'j3', agentId: 'main', targetSessionKey: 'agent:main:webchat:x' }),
      ],
      running: new Set(),
      pendingApprovals: 0,
      mandates: [
        mandate(),
        mandate({
          id: 'dca_p',
          status: 'paused',
          statusReason: 'paused after 3 runs: insufficient USDC',
        }),
        mandate({ id: 'dca_w', status: 'awaiting_approval' }),
        mandate({ id: 'dca_d', status: 'completed' }),
      ],
      triggers: [trigger(), trigger({ id: 'trg_d', status: 'done' })],
      brackets: [bracket(), bracket({ id: 'brk_w', status: 'awaiting_approval' })],
    })
    const by = (g: string) => items.filter((it) => it.group === g).map((it) => it.id)
    expect(by('running')).toEqual(
      expect.arrayContaining(['j1', 'dca_1a2b3c4d', 'trg_1a2b3c4d', 'brk_1a2b3c4d']),
    )
    expect(by('needs')).toEqual(expect.arrayContaining(['dca_w', 'brk_w']))
    expect(by('paused')).toEqual(expect.arrayContaining(['j2', 'dca_p']))
    expect(by('finished')).toEqual(expect.arrayContaining(['dca_d', 'trg_d']))
    // Another agent's job is the Jobs panel's, not the desk's.
    expect(items.some((it) => it.id === 'j3')).toBe(false)
    expect(automationCounts(items)).toEqual({
      all: 10,
      running: 4,
      needs: 2,
      paused: 2,
      finished: 2,
    })
    // Sections keep the group order and leave empty groups out.
    expect(automationSections(items, 'all').map((s) => s.group)).toEqual([
      'running',
      'needs',
      'paused',
      'finished',
    ])
    expect(automationSections(items, 'paused').map((s) => s.group)).toEqual(['paused'])
    // A mission waits on the user while an order of this chat awaits approval.
    expect(missionGroup(job(), { running: false, pendingApprovals: 1 })).toBe('needs')
  })

  it('names the chat an item was filed by, when it is not this one', () => {
    const [mine, theirs, unfiled] = automationItems({
      jobs: [],
      running: new Set(),
      pendingApprovals: 0,
      mandates: [
        mandate(),
        mandate({ id: 'o', sessionKey: OTHER }),
        mandate({ id: 'u', sessionKey: null }),
      ],
      triggers: [],
      brackets: [],
    })
    expect(automationElsewhere(mine!, SESSION)).toBeNull()
    expect(automationElsewhere(theirs!, SESSION)).toBe(OTHER)
    expect(automationElsewhere(unfiled!, SESSION)).toBeNull()
  })

  it('turns a DCA’s buys, a trigger’s fires and a mission’s runs into one kind of row', () => {
    const buys = mandateRunRows(MANDATE)
    expect(buys).toHaveLength(MANDATE.history.length)
    const filled = buys.find((r) => r.wordKey === 'trading.auto.run.filled')!
    expect(filled.tone).toBe('ok')
    expect(filled.figures).toMatch(/^\$10 → .* ETH @ \$/)
    expect(filled.explorerUrl).toBeTruthy()
    const skipped = buys.find((r) => r.wordKey === 'trading.auto.run.skipped')!
    expect(skipped.tone).toBe('warn')
    expect(skipped.reason).toBeTruthy()

    const fires = triggerFireRows(TRIGGER)
    expect(fires[0]).toMatchObject({
      tone: 'danger',
      wordKey: 'trading.auto.run.failed',
      figures: '@ $3,796.2',
    })

    const legs = bracketFireRows(
      bracket({
        takeProfit: {
          ...BRACKET.takeProfit,
          fires: [{ ...TRIGGER.fires[0]!, status: 'filled', n: 1 }],
        },
      }),
    )
    expect(legs[0]).toMatchObject({ leg: 'take-profit', wordKey: 'trading.auto.run.filled' })

    const runs = missionRunRows('j1', [
      {
        id: 'r2',
        started_at: '2026-09-28T05:00:00Z',
        success: true,
        status: 'ok',
        duration_ms: 84_000,
        summary: 'Reported.',
      },
      {
        id: 'r1',
        started_at: '2026-09-27T05:00:00Z',
        success: false,
        status: 'error',
        duration_ms: 900,
        error: 'boom',
      },
    ])
    expect(runs.map((r) => [r.wordKey, r.figures, r.reason])).toEqual([
      ['trading.auto.run.ok', '1 min', 'Reported.'],
      ['trading.auto.run.error', '1 s', 'boom'],
    ])
    const item = automationItems({
      jobs: [job()],
      running: new Set(),
      pendingApprovals: 0,
      mandates: [],
      triggers: [],
      brackets: [],
    })[0]!
    expect(itemHistory(item)).toEqual([])
  })
})

describe('Automations tab', () => {
  const actions = () => ({
    pending: null,
    busy: false,
    pause: vi.fn(async () => null),
    resume: vi.fn(async () => null),
    stop: vi.fn(async () => null),
    run: vi.fn(async () => null),
    fire: vi.fn(async () => null),
    approve: vi.fn(async () => null),
    reject: vi.fn(async () => null),
    create: vi.fn(async () => null),
    update: vi.fn(async () => null),
  })
  function missions(): MissionsApi {
    return {
      missions: [],
      loading: false,
      running: new Set<string>(),
      create: vi.fn(async () => null),
      update: vi.fn(async () => true),
      setEnabled: vi.fn(),
      runNow: vi.fn(),
      remove: vi.fn(),
      busy: false,
      mandates: [],
      awaitingMandates: [],
      mandate: actions() as unknown as MissionsApi['mandate'],
      triggers: [],
      awaitingTriggers: [],
      trigger: actions() as unknown as MissionsApi['trigger'],
      brackets: [],
      awaitingBrackets: [],
      bracket: actions() as unknown as MissionsApi['bracket'],
    }
  }
  const lists = {
    mandates: [mandate(), mandate({ id: 'dca_o', name: 'DCA elsewhere', sessionKey: OTHER })],
    triggers: [trigger()],
    brackets: [bracket()],
    jobs: [job(), job({ id: 'j2', name: 'Off mission', enabled: false })],
  }
  beforeEach(() => {
    rpcCall.mockReset()
    rpcCall.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === 'cron.list') return lists.jobs
      if (method === 'trading.dca.list') return { mandates: params.all ? lists.mandates : [] }
      if (method === 'trading.trigger.list') return { triggers: params.all ? lists.triggers : [] }
      if (method === 'trading.bracket.list') return { brackets: params.all ? lists.brackets : [] }
      if (method === 'cron.runs')
        return [
          {
            id: 'r1',
            started_at: '2026-09-28T05:00:00Z',
            success: true,
            status: 'ok',
            duration_ms: 1200,
            summary: 'Reported.',
            sessionKey: SESSION,
          },
        ]
      return {}
    })
  })

  it('lists every automation under its group, counts the chips, and names another chat’s', async () => {
    const api = missions()
    renderDesk(
      <Automations
        missions={api}
        sessionKey={SESSION}
        pendingApprovals={0}
        enabled
        onOpenSession={vi.fn()}
      />,
    )
    await waitFor(() => expect(screen.getAllByTestId('auto-item').length).toBe(6))
    expect(screen.getByTestId('auto-filter-all')).toHaveTextContent('6')
    expect(screen.getByTestId('auto-filter-running')).toHaveTextContent('5')
    expect(screen.getByTestId('auto-filter-paused')).toHaveTextContent('1')
    // Needs you / Finished are empty: their chips are there but not clickable.
    expect(screen.getByTestId('auto-filter-needs')).toBeDisabled()
    expect(screen.getByTestId('auto-group-running')).toBeInTheDocument()
    expect(screen.getByTestId('auto-group-paused')).toBeInTheDocument()
    expect(screen.queryByTestId('auto-group-finished')).toBeNull()
    // The row filed by another chat says so; this chat's does not.
    const other = screen.getAllByTestId('auto-item').find((el) => el.dataset.id === 'dca_o')!
    expect(within(other).getByTestId('mission-elsewhere')).toHaveAttribute('data-session', OTHER)
    const mine = screen.getAllByTestId('auto-item').find((el) => el.dataset.id === 'dca_1a2b3c4d')!
    expect(within(mine).queryByTestId('mission-elsewhere')).toBeNull()
    // The state word is the same one the row above the composer says.
    expect(within(mine).getByTestId('auto-word')).toHaveTextContent(/^Active/)
    expect(within(mine).getByTestId('auto-facts')).toHaveTextContent('$120 / $300')
  })

  it('filters by chip, and steers a row with the same controls as the composer row', async () => {
    const api = missions()
    renderDesk(
      <Automations
        missions={api}
        sessionKey={SESSION}
        pendingApprovals={0}
        enabled
        onOpenSession={vi.fn()}
      />,
    )
    await waitFor(() => expect(screen.getAllByTestId('auto-item').length).toBe(6))
    fireEvent.click(screen.getByTestId('auto-filter-paused'))
    expect(screen.getAllByTestId('auto-item').map((el) => el.dataset.id)).toEqual(['j2'])
    // A disabled mission offers Continue through the desk's one missions hook.
    fireEvent.click(screen.getByTestId('mission-continue'))
    expect(api.setEnabled).toHaveBeenCalledWith(expect.objectContaining({ id: 'j2' }), true)
    fireEvent.click(screen.getByTestId('auto-filter-all'))
    const mine = screen.getAllByTestId('auto-item').find((el) => el.dataset.id === 'dca_1a2b3c4d')!
    fireEvent.click(within(mine).getByTestId('mandate-pause'))
    expect(api.mandate.pause).toHaveBeenCalledWith(expect.objectContaining({ id: 'dca_1a2b3c4d' }))
    // No Edit here: the BOOK has no contract form to open.
    expect(within(mine).queryByTestId('mandate-edit')).toBeNull()
    const trg = screen.getAllByTestId('auto-item').find((el) => el.dataset.id === 'trg_1a2b3c4d')!
    fireEvent.click(within(trg).getByTestId('trigger-pause'))
    expect(api.trigger.pause).toHaveBeenCalledWith(expect.objectContaining({ id: 'trg_1a2b3c4d' }))
  })

  it('opens a row to its history: a DCA’s buys folded past five, a mission’s runs fetched', async () => {
    const api = missions()
    renderDesk(
      <Automations
        missions={api}
        sessionKey={SESSION}
        pendingApprovals={0}
        enabled
        onOpenSession={vi.fn()}
      />,
    )
    await waitFor(() => expect(screen.getAllByTestId('auto-item').length).toBe(6))
    const mine = screen.getAllByTestId('auto-item').find((el) => el.dataset.id === 'dca_1a2b3c4d')!
    expect(within(mine).queryByTestId('auto-history')).toBeNull()
    fireEvent.click(within(mine).getByTestId('auto-toggle'))
    expect(within(mine).getAllByTestId('auto-run')).toHaveLength(5)
    const more = within(mine).getByTestId('auto-history-more')
    expect(more).toHaveTextContent(`+${MANDATE.history.length - 5} earlier`)
    fireEvent.click(more)
    expect(within(mine).getAllByTestId('auto-run')).toHaveLength(MANDATE.history.length)
    // Newest first: the parked buy awaiting approval, then the skipped one and why.
    const runs = within(mine).getAllByTestId('auto-run')
    expect(runs[0]).toHaveTextContent('Awaiting approval')
    expect(runs[1]).toHaveTextContent('Skipped')
    expect(runs[1]).toHaveTextContent(MANDATE.history[1]!.reason as string)

    const mission = screen.getAllByTestId('auto-item').find((el) => el.dataset.id === 'j1')!
    await act(async () => {
      fireEvent.click(within(mission).getByTestId('auto-toggle'))
    })
    await waitFor(() => expect(within(mission).getAllByTestId('auto-run')).toHaveLength(1))
    expect(rpcCall).toHaveBeenCalledWith('cron.runs', { id: 'j1', limit: 20 })
    expect(within(mission).getByTestId('auto-run')).toHaveTextContent('Ran')
    expect(within(mission).getByTestId('auto-run')).toHaveTextContent('Reported.')
  })

  it('says so when nothing runs on its own', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'cron.list') return []
      if (method.startsWith('trading.dca.')) return { mandates: [] }
      if (method.startsWith('trading.trigger.')) return { triggers: [] }
      if (method.startsWith('trading.bracket.')) return { brackets: [] }
      return {}
    })
    renderDesk(
      <Automations
        missions={missions()}
        sessionKey={SESSION}
        pendingApprovals={0}
        enabled
        onOpenSession={vi.fn()}
      />,
    )
    expect(await screen.findByText('Nothing runs on its own yet')).toBeInTheDocument()
  })
})
