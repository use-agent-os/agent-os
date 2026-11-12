import '@/test/web'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { usePendingQuestions } from '@/lib/pending-questions'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { renderShell, resetShellState, shellRpc } from '@/test/web-shell'
import { JOBS_POLL_MS, runNowStatus } from './AutomationsPage'
import { cronTimesBetween, jobTimesBetween } from './schedule'

beforeEach(() => {
  resetShellState()
  usePendingQuestions.getState().reset()
})

function main() {
  return screen.getByRole('main')
}

function cronRpc(jobs: Record<string, unknown>[] = []) {
  const state = { jobs }
  const rpc = shellRpc()
    .handle('cron.list', () => ({ jobs: structuredClone(state.jobs) }))
    .handle('channels.deliveryTargets', { targets: {} })
    .handle('cron.subscribe', { ok: true })
    .handle('cron.unsubscribe', { ok: true })
  return { rpc, state }
}

describe('Automations page', () => {
  it('explains the empty page and offers New automation and presets', async () => {
    renderShell('/automations', cronRpc().rpc)
    const empty = await within(main()).findByText('No automations yet')
    const region = empty.closest('[data-slot="empty-state"]') as HTMLElement
    expect(region).toHaveTextContent('Scheduled jobs appear here')
    expect(within(region).getByRole('button', { name: 'New automation' })).toBeInTheDocument()
    expect(within(region).getByRole('button', { name: /Daily standup nudge/ })).toBeInTheDocument()
  })

  it('creates an automation from the empty state with cron.create', async () => {
    const user = userEvent.setup()
    const { rpc, state } = cronRpc()
    rpc.handle('cron.create', (p: Record<string, unknown>) => {
      state.jobs = [{ id: 'job-1', enabled: true, expression: '0 9 * * *', ...p }]
      return { id: 'job-1' }
    })
    renderShell('/automations', rpc)
    const empty = (await within(main()).findByText('No automations yet')).closest(
      '[data-slot="empty-state"]',
    ) as HTMLElement
    await user.click(within(empty).getByRole('button', { name: 'New automation' }))

    const sheet = await screen.findByRole('dialog', { name: 'New automation' })
    await user.type(within(sheet).getByRole('textbox', { name: 'Name' }), 'Daily report')
    await user.type(within(sheet).getByRole('textbox', { name: 'Cron expression' }), '0 9 * * *')
    expect(within(sheet).getByText('Every day at 09:00')).toBeInTheDocument()
    expect(within(sheet).getByText('Next runs')).toBeInTheDocument()
    await user.click(within(sheet).getByRole('button', { name: 'Create automation' }))

    await waitFor(() => expect(rpc.callsTo('cron.create')).toHaveLength(1))
    expect(rpc.callsTo('cron.create')[0]).toMatchObject({
      name: 'Daily report',
      enabled: true,
      payloadKind: 'reminder',
      schedule: { kind: 'cron', expr: '0 9 * * *' },
    })
    expect(await within(main()).findByRole('article', { name: 'Daily report' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'New automation' })).toBeNull()
  })

  it('seeds the editor from a preset and refuses to save without a name', async () => {
    const user = userEvent.setup()
    const { rpc } = cronRpc()
    rpc.handle('cron.create', { id: 'x' })
    renderShell('/automations', rpc)
    await user.click(await within(main()).findByRole('button', { name: /Friday wrap-up/ }))
    const sheet = await screen.findByRole('dialog', { name: 'New automation' })
    expect(within(sheet).getByRole('textbox', { name: 'Name' })).toHaveValue('Friday wrap-up')
    expect(within(sheet).getByRole('textbox', { name: 'Cron expression' })).toHaveValue(
      '0 17 * * 5',
    )
    await user.clear(within(sheet).getByRole('textbox', { name: 'Name' }))
    await user.click(within(sheet).getByRole('button', { name: 'Create automation' }))
    const alert = await within(sheet).findByRole('alert')
    // The message sits with the field it is about.
    const name = within(sheet).getByRole('textbox', { name: 'Name' })
    expect(name).toHaveAttribute('aria-invalid', 'true')
    expect(name).toHaveAttribute('aria-describedby', alert.id)
    expect(rpc.callsTo('cron.create')).toEqual([])
  })

  it('runs, pauses and deletes a job, and refreshes on cron.run.finished', async () => {
    const user = userEvent.setup()
    const { rpc } = cronRpc([
      { id: 'job-1', name: 'Nightly', enabled: true, expression: '0 2 * * *' },
    ])
    rpc
      .handle('cron.run', { reply: 'all good' })
      .handle('cron.update', { ok: true })
      .handle('cron.remove', { ok: true })
    renderShell('/automations', rpc)
    const card = await within(main()).findByRole('article', { name: 'Nightly' })

    await user.click(within(card).getByRole('button', { name: 'Run now: Nightly' }))
    expect(await within(card).findByText('Run complete: all good')).toBeInTheDocument()
    expect(rpc.callsTo('cron.run')).toEqual([{ id: 'job-1' }])

    await user.click(within(card).getByRole('button', { name: 'Pause: Nightly' }))
    await waitFor(() =>
      expect(rpc.callsTo('cron.update')).toEqual([{ id: 'job-1', enabled: false }]),
    )

    const listed = rpc.callsTo('cron.list').length
    rpc.emit('cron.run.finished', { id: 'job-1' })
    await waitFor(() => expect(rpc.callsTo('cron.list').length).toBeGreaterThan(listed))

    // Delete lives in the row menu, behind More actions.
    await user.click(within(card).getByRole('button', { name: 'More actions: Nightly' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Delete' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(rpc.callsTo('cron.remove')).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(rpc.callsTo('cron.remove')).toEqual([{ id: 'job-1' }]))
  })
})

describe('Automations page: reconnect and Run now', () => {
  it('subscribes again after a reconnect', async () => {
    const { rpc } = cronRpc([
      { id: 'job-1', name: 'Nightly', enabled: true, expression: '0 2 * * *' },
    ])
    renderShell('/automations', rpc)
    await within(main()).findByRole('article', { name: 'Nightly' })
    await waitFor(() => expect(rpc.callsTo('cron.subscribe')).toHaveLength(1))
    // The gateway restarted: subscriptions are per connection, so the new one has none.
    act(() => useRuntimeSignals.setState({ connectedAt: Date.now() + 1 }))
    await waitFor(() => expect(rpc.callsTo('cron.subscribe')).toHaveLength(2))
    expect(rpc.callsTo('cron.unsubscribe')).toEqual([])
  })

  it('polls the jobs slowly as a fallback for missed run events', () => {
    expect(JOBS_POLL_MS).toBeGreaterThanOrEqual(30_000)
    expect(JOBS_POLL_MS).toBeLessThanOrEqual(120_000)
  })

  it('reports a failed or refused run as a failure even when it carries text', async () => {
    const user = userEvent.setup()
    const { rpc } = cronRpc([
      { id: 'job-1', name: 'Nightly', enabled: true, expression: '0 2 * * *' },
    ])
    rpc.handle('cron.run', {
      success: false,
      status: 'accepted',
      reply: 'partial stdout',
      error: 'exit status 2',
    })
    renderShell('/automations', rpc)
    const card = await within(main()).findByRole('article', { name: 'Nightly' })
    await user.click(within(card).getByRole('button', { name: 'Run now: Nightly' }))
    expect(await within(card).findByRole('alert')).toHaveTextContent('Run failed: exit status 2')

    rpc.handle('cron.run', { success: false, status: 'busy', reason: 'busy', error: null })
    await user.click(within(card).getByRole('button', { name: 'Run now: Nightly' }))
    await waitFor(() =>
      expect(within(card).getByRole('alert')).toHaveTextContent('Didn’t run: busy'),
    )
  })

  it('maps the cron.run answer to the card status', () => {
    expect(runNowStatus({ success: true, reply: 'ok' })).toEqual({
      tone: 'ok',
      text: 'Run complete: ok',
    })
    expect(runNowStatus({ success: false, status: 'accepted', reply: 'out' }).tone).toBe('danger')
    expect(runNowStatus({ success: false, status: 'disabled', reason: 'disabled' })).toEqual({
      tone: 'danger',
      text: 'Didn’t run: disabled',
    })
    expect(runNowStatus(null)).toEqual({ tone: 'ok', text: 'Run started' })
  })
})

describe('Automations page: schedule first', () => {
  const inAnHour = () => new Date(Date.now() + 3_600_000).toISOString()

  it('leads with the next run and draws the next 24 hours as one lane per job', async () => {
    const { rpc } = cronRpc([
      {
        id: 'job-1',
        name: 'Nightly',
        enabled: true,
        expression: '0 2 * * *',
        next_run: new Date(Date.now() + 5 * 3_600_000).toISOString(),
      },
      {
        id: 'job-2',
        name: 'Triage',
        enabled: true,
        expression: '*/30 * * * *',
        next_run: inAnHour(),
      },
      { id: 'job-3', name: 'Off', enabled: false, expression: '0 9 * * *' },
    ])
    renderShell('/automations', rpc)
    const lead = await within(main()).findByRole('region', { name: 'Up next' })
    // The soonest job leads; the other enabled job follows under "Then".
    expect(lead).toHaveTextContent('Triage')
    expect(lead).toHaveTextContent(/in \d+m/)
    expect(within(lead).getByRole('list', { name: 'Then' })).toHaveTextContent('Nightly')

    const day = within(main()).getByRole('region', { name: 'Next 24 hours' })
    const lanes = within(day).getAllByRole('listitem')
    // Paused jobs have no lane; a job every 30 minutes fires 48 times a day.
    expect(lanes.map((li) => li.getAttribute('aria-label')?.split(':')[0])).toEqual([
      'Triage',
      'Nightly',
    ])
    expect(lanes[0]).toHaveAttribute('aria-label', expect.stringMatching(/^Triage: 4[78] runs/))
    expect(lanes[1]).toHaveAttribute('aria-label', expect.stringMatching(/^Nightly: 1 run in/))

    // The schedule in words is the row's headline; cron stays beside it.
    const row = within(main()).getByRole('article', { name: 'Triage' })
    expect(within(row).getByText('Every 30 minutes')).toBeInTheDocument()
    expect(within(row).getByText('*/30 * * * *')).toBeInTheDocument()
  })

  it('says nothing is scheduled when every job is paused', async () => {
    const { rpc } = cronRpc([{ id: 'job-1', name: 'Off', enabled: false, expression: '0 9 * * *' }])
    renderShell('/automations', rpc)
    const lead = await within(main()).findByRole('region', { name: 'Up next' })
    expect(lead).toHaveTextContent('Nothing scheduled')
  })

  it('shows the templates as cards with the schedule in words and the cron beside it', async () => {
    renderShell('/automations', cronRpc().rpc)
    const gallery = await within(main()).findByRole('region', { name: 'Start from a template' })
    const cards = within(gallery).getAllByRole('button')
    expect(cards).toHaveLength(3)
    expect(cards[0]).toHaveTextContent('Weekdays at 09:00')
    expect(cards[0]).toHaveTextContent('0 9 * * 1-5')
    expect(cards[0]).toHaveTextContent('Reminder')
    // The zero state carries the page's one primary action; the header has none.
    expect(within(main()).getAllByRole('button', { name: 'New automation' })).toHaveLength(1)
  })

  it('sets the overlap policy and copies the id from the row menu', async () => {
    const user = userEvent.setup()
    const { rpc } = cronRpc([
      {
        id: 'job-1',
        name: 'Nightly',
        enabled: true,
        expression: '0 2 * * *',
        concurrency: 'forbid',
      },
    ])
    rpc.handle('cron.update', { ok: true })
    renderShell('/automations', rpc)
    const card = await within(main()).findByRole('article', { name: 'Nightly' })
    await user.click(within(card).getByRole('button', { name: 'More actions: Nightly' }))
    const menu = await screen.findByRole('menu')
    expect(within(menu).getByRole('menuitem', { name: /Copy ID/ })).toBeInTheDocument()
    expect(within(menu).getByRole('menuitemradio', { checked: true })).toHaveTextContent(/Forbid/)
    await user.click(within(menu).getByRole('menuitemradio', { name: /Allow/ }))
    await waitFor(() =>
      expect(rpc.callsTo('cron.update')).toEqual([{ id: 'job-1', concurrency: 'allow' }]),
    )
  })

  it('sums up the run history above the list', async () => {
    const user = userEvent.setup()
    const { rpc } = cronRpc([
      { id: 'job-1', name: 'Nightly', enabled: true, expression: '0 2 * * *' },
    ])
    rpc.handle('cron.runs', {
      runs: [
        { id: 'r3', status: 'ok', duration_ms: 1200, started_at: Date.now() - 1000 },
        { id: 'r2', status: 'error', duration_ms: 300, started_at: Date.now() - 2000 },
        { id: 'r1', status: 'ok', duration_ms: 900, started_at: Date.now() - 3000 },
      ],
    })
    renderShell('/automations', rpc)
    const card = await within(main()).findByRole('article', { name: 'Nightly' })
    await user.click(within(card).getByRole('button', { name: 'History: Nightly' }))
    const sheet = await screen.findByRole('dialog')
    const summary = await within(sheet).findByRole('region', { name: 'Run summary' })
    expect(summary).toHaveTextContent('2 of 3 succeeded')
    expect(summary).toHaveTextContent('Average 800ms')
    expect(within(sheet).getByRole('list', { name: 'Runs' }).children).toHaveLength(3)
  })
})

describe('schedule helpers', () => {
  const from = Date.UTC(2026, 10, 12, 0, 0)
  const day = 86_400_000

  it('expands a cron expression inside a window, in UTC when the job has no zone', () => {
    const times = cronTimesBetween('0 9 * * *', '', from, from + 3 * day)
    expect(times.map((ts) => new Date(ts).toISOString())).toEqual([
      '2026-11-12T09:00:00.000Z',
      '2026-11-13T09:00:00.000Z',
      '2026-11-14T09:00:00.000Z',
    ])
    expect(cronTimesBetween('*/5 * * * *', '', from, from + day)).toHaveLength(96)
    expect(cronTimesBetween('not cron', '', from, from + day)).toEqual([])
  })

  it('steps an interval job from its next run and skips paused jobs', () => {
    const job = {
      enabled: true,
      scheduleKind: 'every',
      scheduleRaw: 3600,
      next_run: new Date(from + 1800_000).toISOString(),
    }
    expect(jobTimesBetween(job, from, from + 4 * 3_600_000)).toHaveLength(4)
    expect(jobTimesBetween({ ...job, enabled: false }, from, from + day)).toEqual([])
  })
})
