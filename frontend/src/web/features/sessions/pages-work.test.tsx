import '@/test/web'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { usePendingQuestions } from '@/lib/pending-questions'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { renderShell, resetShellState, sessionRow, shellRpc } from '@/test/web-shell'
import { JOBS_POLL_MS, runNowStatus } from '@/web/features/automations/AutomationsPage'

beforeEach(() => {
  resetShellState()
  usePendingQuestions.getState().reset()
})

type User = ReturnType<typeof userEvent.setup>

const A = 'agent:main:webchat:a'
const B = 'agent:main:webchat:b'
const GHOST = 'agent:ghost:webchat:g'

function main() {
  return screen.getByRole('main')
}

/** Handlers the chat route needs once a test navigates into a session. */
function withChat<T extends ReturnType<typeof shellRpc>>(rpc: T): T {
  return rpc
    .handle('chat.history', { messages: [] })
    .handle('sessions.messages.subscribe', { subscribed: true })
    .handle('sessions.messages.unsubscribe', null)
}

// ── Sessions ────────────────────────────────────────────────────────────────

function sessionsRpc() {
  return shellRpc([
    sessionRow(A, { display_name: 'Alpha chat', message_count: 3 }),
    sessionRow(B, { display_name: 'Beta chat', message_count: 4, project_id: 'p1' }),
    sessionRow(GHOST, { display_name: 'Ghost chat' }),
  ])
}

async function table() {
  return within(await within(main()).findByRole('region', { name: 'Sessions table' }))
}

describe('Sessions page', () => {
  it('lists sessions with stat tiles, chat links and an orphan flag', async () => {
    renderShell('/sessions', sessionsRpc())
    const rows = await table()
    expect(rows.getByRole('link', { name: 'Alpha chat' })).toHaveAttribute(
      'href',
      '/chat/main/webchat/a',
    )
    const ghost = rows.getByRole('link', { name: 'Ghost chat' }).closest('tr')!
    await waitFor(() => expect(within(ghost).getByText('Agent missing')).toBeInTheDocument())
    const alpha = rows.getByRole('link', { name: 'Alpha chat' }).closest('tr')!
    expect(within(alpha).queryByText('Agent missing')).toBeNull()
    expect(within(main()).getByText('Messages', { selector: 'dt' }).nextSibling).toHaveTextContent(
      '7',
    )
  })

  it('filters by agent and project', async () => {
    const user = userEvent.setup()
    renderShell('/sessions', sessionsRpc())
    const rows = await table()
    await user.selectOptions(within(main()).getByRole('combobox', { name: 'Project' }), 'p1')
    expect(rows.queryByRole('link', { name: 'Alpha chat' })).toBeNull()
    expect(rows.getByRole('link', { name: 'Beta chat' })).toBeInTheDocument()
    await user.selectOptions(within(main()).getByRole('combobox', { name: 'Project' }), 'all')
    await user.selectOptions(within(main()).getByRole('combobox', { name: 'Agent' }), 'ghost')
    expect(rows.getByRole('link', { name: 'Ghost chat' })).toBeInTheDocument()
    expect(rows.queryByRole('link', { name: 'Beta chat' })).toBeNull()
  })

  it('renames a session inline', async () => {
    const user = userEvent.setup()
    const rpc = sessionsRpc().handle('sessions.rename', (p: Record<string, unknown>) => p)
    renderShell('/sessions', rpc)
    const rows = await table()
    await user.click(rows.getByRole('button', { name: 'Rename Alpha chat' }))
    const input = rows.getByRole('textbox', { name: 'Session name' })
    await user.clear(input)
    await user.type(input, 'Morning brief{Enter}')
    await waitFor(() =>
      expect(rpc.callsTo('sessions.rename')).toEqual([{ key: A, name: 'Morning brief' }]),
    )
  })

  it('a row menu opens the chat in a new tab, like the sidebar menu', async () => {
    const user = userEvent.setup()
    const opened = { opener: window as Window | null }
    const open = vi.spyOn(window, 'open').mockReturnValue(opened as unknown as Window)
    renderShell('/sessions', sessionsRpc())
    const rows = await table()
    await user.click(rows.getByRole('button', { name: 'Actions for Alpha chat' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Open in new tab' }))
    expect(open).toHaveBeenCalledWith('/chat/main/webchat/a', '_blank')
    expect(opened.opener).toBeNull()
    open.mockRestore()
  })

  it('bulk-deletes the selected sessions after confirming', async () => {
    const user = userEvent.setup()
    const rpc = sessionsRpc().handle('sessions.delete', { deleted: [A, B], errors: [] })
    renderShell('/sessions', rpc)
    const rows = await table()
    await user.click(rows.getByRole('checkbox', { name: 'Select Alpha chat' }))
    await user.click(rows.getByRole('checkbox', { name: 'Select Beta chat' }))
    const bulk = within(main()).getByRole('region', { name: 'Selected sessions' })
    expect(bulk).toHaveTextContent('2 sessions selected')
    await user.click(within(bulk).getByRole('button', { name: 'Delete selected' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Delete 2 sessions?')
    expect(rpc.callsTo('sessions.delete')).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(rpc.callsTo('sessions.delete')).toEqual([{ keys: [A, B] }]))
  })

  it('reports a partial bulk delete', async () => {
    const user = userEvent.setup()
    const rpc = sessionsRpc().handle('sessions.delete', {
      deleted: [A],
      errors: [{ key: B, message: 'busy' }],
    })
    renderShell('/sessions', rpc)
    const rows = await table()
    await user.click(rows.getByRole('checkbox', { name: 'Select all shown sessions' }))
    await user.click(within(main()).getByRole('button', { name: 'Delete selected' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }),
    )
    expect(await within(main()).findByRole('alert')).toHaveTextContent(
      '1 deleted, 1 could not be deleted.',
    )
  })

  it('moves a session to a project', async () => {
    const user = userEvent.setup()
    const rpc = sessionsRpc().handle('sessions.patch', { ok: true })
    renderShell('/sessions', rpc)
    const rows = await table()
    await user.click(rows.getByRole('button', { name: 'Actions for Alpha chat' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Move to project…' }))
    const dialog = await screen.findByRole('dialog')
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Project' }), 'p1')
    await user.click(within(dialog).getByRole('button', { name: 'Move' }))
    await waitFor(() =>
      expect(rpc.callsTo('sessions.patch')).toEqual([{ key: A, projectId: 'p1' }]),
    )
  })

  it('creates an agent from the new-session dialog, then opens the session', async () => {
    const user = userEvent.setup()
    const rpc = withChat(sessionsRpc())
      .handle('agents.create', { ok: true })
      .handle('sessions.create', { key: 'agent:scout:webchat:n1' })
    const { router } = renderShell('/sessions', rpc)
    await table()
    await user.click(within(main()).getByRole('button', { name: 'New session' }))
    const dialog = await screen.findByRole('dialog')
    await user.selectOptions(
      within(dialog).getByRole('combobox', { name: 'Agent' }),
      'Create a new agent…',
    )
    await user.type(within(dialog).getByRole('textbox', { name: 'New agent id' }), 'scout')
    await user.click(within(dialog).getByRole('button', { name: 'Create agent and start' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/chat/scout/webchat/n1'))
    expect(rpc.callsTo('agents.create')).toEqual([{ id: 'scout', name: 'scout' }])
    expect(rpc.callsTo('sessions.create')).toEqual([{ agentId: 'scout' }])
  })

  it('offers New session in the empty state', async () => {
    renderShell('/sessions', shellRpc([]))
    expect(await within(main()).findByText('No sessions yet')).toBeInTheDocument()
    expect(within(main()).getAllByRole('button', { name: 'New session' }).length).toBe(2)
  })
})

// ── Projects ────────────────────────────────────────────────────────────────

interface ServerProject {
  project_id: string
  name: string
  knowledge: string
  updated_at: number
  agent_id: string
}

function projectsRpc(server: { project: ServerProject }) {
  return shellRpc([sessionRow(B, { display_name: 'Beta chat', project_id: 'p1' })]).handle(
    'projects.list',
    () => ({ projects: [structuredClone(server.project)] }),
  )
}

async function editKnowledge(user: User, text: string) {
  const field = await within(main()).findByRole('textbox', { name: 'Knowledge' })
  await user.type(field, text)
  await user.click(within(main()).getByRole('button', { name: 'Save knowledge' }))
}

describe('Projects page', () => {
  const base = (): ServerProject => ({
    project_id: 'p1',
    name: 'Alpha',
    knowledge: 'old',
    updated_at: 100,
    agent_id: 'main',
  })

  it('shows the newer text on a knowledge conflict and can overwrite it', async () => {
    const user = userEvent.setup()
    const server = { project: base() }
    let first = true
    const rpc = projectsRpc(server).handle('projects.update', (p: Record<string, unknown>) => {
      if (first) {
        // Another client saved in between: the CAS fails.
        first = false
        server.project = { ...server.project, knowledge: 'theirs', updated_at: 200 }
        throw Object.assign(new Error('project changed'), { code: 'project.conflict' })
      }
      server.project = { ...server.project, knowledge: String(p.knowledge), updated_at: 300 }
      return { project: server.project }
    })
    renderShell('/projects/p1', rpc)
    await editKnowledge(user, ' mine')

    const notice = await within(main()).findByRole('alert')
    expect(notice).toHaveTextContent('Someone else saved this knowledge first')
    expect(within(notice).getByLabelText('Newer saved version')).toHaveTextContent('theirs')
    // The draft survives the conflict.
    expect(within(main()).getByRole('textbox', { name: 'Knowledge' })).toHaveValue('old mine')
    expect(rpc.callsTo('projects.update')[0]).toEqual({
      projectId: 'p1',
      knowledge: 'old mine',
      expectedUpdatedAt: 100,
    })

    await user.click(within(notice).getByRole('button', { name: 'Overwrite with my draft' }))
    await waitFor(() => expect(rpc.callsTo('projects.update')).toHaveLength(2))
    expect(rpc.callsTo('projects.update')[1]).toEqual({
      projectId: 'p1',
      knowledge: 'old mine',
      expectedUpdatedAt: 200,
    })
    await waitFor(() => expect(within(main()).queryByRole('alert')).toBeNull())
  })

  it('can take the newer version after a conflict instead', async () => {
    const user = userEvent.setup()
    const server = { project: base() }
    const rpc = projectsRpc(server).handle('projects.update', () => {
      server.project = { ...server.project, knowledge: 'theirs', updated_at: 200 }
      throw Object.assign(new Error('project changed'), { code: 'project.conflict' })
    })
    renderShell('/projects/p1', rpc)
    await editKnowledge(user, ' mine')
    const notice = await within(main()).findByRole('alert')
    await user.click(within(notice).getByRole('button', { name: 'Use the newer version' }))
    expect(within(main()).getByRole('textbox', { name: 'Knowledge' })).toHaveValue('theirs')
    expect(rpc.callsTo('projects.update')).toHaveLength(1)
  })

  it('starts a new chat in the project and lists its sessions', async () => {
    const user = userEvent.setup()
    const rpc = projectsRpc({ project: { ...base(), agent_id: 'research' } }).handle(
      'sessions.create',
      { key: 'agent:research:webchat:fresh' },
    )
    const { router } = renderShell('/projects/p1', rpc)
    const sessions = await within(main()).findByRole('region', {
      name: 'Sessions in this project',
    })
    await waitFor(() =>
      expect(within(sessions).getByRole('link', { name: /Beta chat/ })).toHaveAttribute(
        'href',
        '/chat/main/webchat/b',
      ),
    )
    await user.click(within(sessions).getByRole('button', { name: 'New chat in project' }))
    // Created at once under the project's owner, then opened: no first message needed.
    await waitFor(() => expect(router.state.location.pathname).toBe('/chat/research/webchat/fresh'))
    expect(rpc.callsTo('sessions.create')).toEqual([{ agentId: 'research', projectId: 'p1' }])
  })

  it('sends a new chat in a project without an owner through the agent picker', async () => {
    const user = userEvent.setup()
    const rpc = projectsRpc({ project: { ...base(), agent_id: '' } })
    const { router } = renderShell('/projects/p1', rpc)
    const sessions = await within(main()).findByRole('region', {
      name: 'Sessions in this project',
    })
    await user.click(within(sessions).getByRole('button', { name: 'New chat in project' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/new'))
    expect(router.state.location.search).toBe('?project=p1')
    expect(rpc.callsTo('sessions.create')).toEqual([])
  })

  it('says why a new chat in the project could not start', async () => {
    const user = userEvent.setup()
    const rpc = projectsRpc({ project: base() }).handle('sessions.create', () => {
      throw new Error('agent busy')
    })
    const { router } = renderShell('/projects/p1', rpc)
    const sessions = await within(main()).findByRole('region', {
      name: 'Sessions in this project',
    })
    await user.click(within(sessions).getByRole('button', { name: 'New chat in project' }))
    expect(await within(main()).findByText(/Couldn’t create the session: agent busy/)).toBeVisible()
    expect(router.state.location.pathname).toBe('/projects/p1')
  })

  it('follows projects.changed live', async () => {
    const server = { project: base() }
    const rpc = projectsRpc(server)
    renderShell('/projects', rpc)
    const list = await within(main()).findByRole('list', { name: 'Projects' })
    await within(list).findByText('Alpha')
    server.project = { ...server.project, name: 'Renamed elsewhere' }
    rpc.emit('projects.changed', {})
    expect(await within(list).findByText('Renamed elsewhere')).toBeInTheDocument()
  })

  it('deletes a project after confirming', async () => {
    const user = userEvent.setup()
    const rpc = projectsRpc({ project: base() }).handle('projects.delete', { ok: true })
    const { router } = renderShell('/projects/p1', rpc)
    await user.click(await within(main()).findByRole('button', { name: 'Delete project' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(rpc.callsTo('projects.delete')).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/projects'))
    expect(rpc.callsTo('projects.delete')).toEqual([{ projectId: 'p1' }])
  })
})

// ── Automations ─────────────────────────────────────────────────────────────

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
    expect(await within(sheet).findByRole('alert')).toBeInTheDocument()
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

    await user.click(within(card).getByRole('button', { name: 'Delete Nightly' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(rpc.callsTo('cron.remove')).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(rpc.callsTo('cron.remove')).toEqual([{ id: 'job-1' }]))
  })
})

// ── Review fixes (2026-11-10) ───────────────────────────────────────────────

describe('Sessions page: status, totals and failures', () => {
  it('shows each row’s lifecycle, the open/done/aborted breakdown and what is executing', async () => {
    renderShell(
      '/sessions',
      shellRpc([
        sessionRow(A, {
          display_name: 'Working',
          status: 'running',
          active_task: { status: 'running' },
        }),
        sessionRow(B, { display_name: 'Finished', status: 'done' }),
        sessionRow('agent:main:webchat:c', {
          display_name: 'Stopped',
          status: 'done',
          last_task: { status: 'cancelled' },
        }),
      ]),
    )
    const rows = await table()
    const row = (name: string) => rows.getByRole('link', { name }).closest('tr')!
    expect(row('Working').querySelector('[data-lifecycle]')).toHaveTextContent('Open')
    expect(within(row('Working')).getByText('Task running')).toBeInTheDocument()
    expect(row('Finished').querySelector('[data-lifecycle]')).toHaveTextContent('Completed')
    expect(row('Stopped').querySelector('[data-lifecycle]')).toHaveTextContent('Aborted')
    expect(within(main()).getByRole('status')).toHaveTextContent('1 session executing now')
    expect(
      within(main()).getByText('Sessions', { selector: 'dt' }).parentElement,
    ).toHaveTextContent('1 open · 1 completed · 0 failed or timed out · 1 aborted')
  })

  it('says the list is the newest 500 of the real total', { timeout: 15_000 }, async () => {
    const many = Array.from({ length: 500 }, (_, i) =>
      sessionRow(`agent:main:webchat:s${i}`, { display_name: `Chat ${i}` }),
    )
    const rpc = shellRpc().handle('sessions.list', (p: Record<string, unknown>) =>
      p.limit === 1 ? { sessions: [many[0]], total: 812 } : { sessions: many, total: 812 },
    )
    renderShell('/sessions', rpc)
    await within(main()).findByRole('region', { name: 'Sessions table' }, { timeout: 5000 })
    expect(await within(main()).findByRole('note', {}, { timeout: 5000 })).toHaveTextContent(
      'Showing the newest 500 of 812 sessions.',
    )
    expect(within(main()).getByText('Sessions', { selector: 'dt' }).nextSibling).toHaveTextContent(
      '812',
    )
  })

  it('reports a single delete the gateway refused, and keeps the row', async () => {
    const user = userEvent.setup()
    const rpc = sessionsRpc().handle('sessions.delete', {
      deleted: [],
      errors: [`${A}: session is busy`],
    })
    renderShell('/sessions', rpc)
    const rows = await table()
    await user.click(rows.getByRole('button', { name: 'Actions for Alpha chat' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Delete' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }),
    )
    expect(await within(main()).findByRole('alert')).toHaveTextContent(
      `Couldn’t delete: ${A}: session is busy`,
    )
    expect(rpc.callsTo('sessions.delete')).toEqual([{ key: A }])
    expect(rows.getByRole('link', { name: 'Alpha chat' })).toBeInTheDocument()
  })

  it('reports a delete that throws', async () => {
    const user = userEvent.setup()
    const rpc = sessionsRpc().handle('sessions.delete', () => {
      throw new Error('gateway went away')
    })
    renderShell('/sessions', rpc)
    const rows = await table()
    await user.click(rows.getByRole('button', { name: 'Actions for Alpha chat' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Delete' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }),
    )
    expect(await within(main()).findByRole('alert')).toHaveTextContent(
      'Couldn’t delete: gateway went away',
    )
  })

  it('clears a pending question when its session is deleted', async () => {
    const user = userEvent.setup()
    usePendingQuestions.getState().present({
      id: `question:${A}`,
      sessionKey: A,
      questions: [{ question: 'Ship it?', options: [{ label: 'yes' }], multiSelect: false }],
      toolUseId: 't1',
      presentedAt: Date.now() + 60_000,
    })
    const rpc = sessionsRpc().handle('sessions.delete', { deleted: [A], errors: [] })
    renderShell('/sessions', rpc)
    const rows = await table()
    await user.click(rows.getByRole('button', { name: 'Actions for Alpha chat' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Delete' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }),
    )
    await waitFor(() => expect(usePendingQuestions.getState().items).toEqual([]))
  })

  it('keeps the rename open with the error when the rename fails', async () => {
    const user = userEvent.setup()
    const rpc = sessionsRpc().handle('sessions.rename', () => {
      throw new Error('name too long')
    })
    renderShell('/sessions', rpc)
    const rows = await table()
    await user.click(rows.getByRole('button', { name: 'Rename Alpha chat' }))
    const input = rows.getByRole('textbox', { name: 'Session name' })
    await user.clear(input)
    await user.type(input, 'Brief{Enter}')
    expect(await rows.findByRole('alert')).toHaveTextContent('Couldn’t rename: name too long')
    expect(rows.getByRole('textbox', { name: 'Session name' })).toHaveValue('Brief')
    expect(rows.getByRole('textbox', { name: 'Session name' })).toHaveAttribute(
      'aria-invalid',
      'true',
    )
  })
})

describe('Projects page: rename then knowledge save', () => {
  it('saves the knowledge draft after a rename without a false conflict', async () => {
    const user = userEvent.setup()
    const server = {
      project: {
        project_id: 'p1',
        name: 'Alpha',
        knowledge: 'old',
        updated_at: 100,
        agent_id: 'main',
      },
    }
    const rpc = projectsRpc(server).handle('projects.update', (p: Record<string, unknown>) => {
      if (p.expectedUpdatedAt !== server.project.updated_at)
        throw Object.assign(new Error('project changed'), { code: 'project.conflict' })
      const next = server.project.updated_at + 100
      server.project = {
        ...server.project,
        ...(p.name !== undefined ? { name: String(p.name) } : {}),
        ...(p.knowledge !== undefined ? { knowledge: String(p.knowledge) } : {}),
        updated_at: next,
      }
      return { project: server.project }
    })
    renderShell('/projects/p1', rpc)
    const field = await within(main()).findByRole('textbox', { name: 'Knowledge' })
    await user.type(field, ' mine')
    const name = within(main()).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'Beta')
    await user.click(within(main()).getByRole('button', { name: 'Save name' }))
    await waitFor(() => expect(rpc.callsTo('projects.update')).toHaveLength(1))
    await user.click(within(main()).getByRole('button', { name: 'Save knowledge' }))
    await waitFor(() => expect(rpc.callsTo('projects.update')).toHaveLength(2))
    expect(rpc.callsTo('projects.update')[1]).toEqual({
      projectId: 'p1',
      knowledge: 'old mine',
      expectedUpdatedAt: 200,
    })
    expect(within(main()).queryByText('Someone else saved this knowledge first')).toBeNull()
    await waitFor(() => expect(server.project.knowledge).toBe('old mine'))
  })

  it('Enter in the name field saves the name', async () => {
    const user = userEvent.setup()
    const server = {
      project: {
        project_id: 'p1',
        name: 'Alpha',
        knowledge: '',
        updated_at: 100,
        agent_id: 'main',
      },
    }
    const rpc = projectsRpc(server).handle('projects.update', (p: Record<string, unknown>) => {
      server.project = { ...server.project, name: String(p.name), updated_at: 200 }
      return { project: server.project }
    })
    renderShell('/projects/p1', rpc)
    const name = await within(main()).findByRole('textbox', { name: 'Name' })
    // Unchanged: Enter does nothing.
    await user.type(name, '{Enter}')
    expect(rpc.callsTo('projects.update')).toEqual([])
    await user.clear(name)
    await user.type(name, 'Gamma{Enter}')
    await waitFor(() =>
      expect(rpc.callsTo('projects.update')).toEqual([
        { projectId: 'p1', name: 'Gamma', expectedUpdatedAt: 100 },
      ]),
    )
  })

  it('notes when the project has more sessions than are loaded', async () => {
    const rpc = shellRpc([sessionRow(B, { display_name: 'Beta chat', project_id: 'p1' })]).handle(
      'projects.list',
      {
        projects: [
          { project_id: 'p1', name: 'Alpha', knowledge: '', updated_at: 1, session_count: 7 },
        ],
      },
    )
    renderShell('/projects/p1', rpc)
    const sessions = await within(main()).findByRole('region', {
      name: 'Sessions in this project',
    })
    expect(await within(sessions).findByRole('note')).toHaveTextContent(
      'Showing 1 of the project’s 7 sessions',
    )
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
