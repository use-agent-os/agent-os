import '@/test/web'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { usePendingQuestions } from '@/lib/pending-questions'
import { normalizeDoctor, useRuntimeSignals } from '@/lib/runtime-signals'
import { approvalMonitor, useApprovals } from '@/services/approval-monitor'
import { NOW, renderShell, resetShellState, sessionRow, shellRpc } from '@/test/web-shell'
import { SESSIONS_LIST_LIMIT } from '@/views/sessions/logic'
import { startOfLocalDay } from '@/views/usage/logic'
import { homeGreeting, homeUsageFigures, sessionsPulse } from './HomePage'
import { runCycle, usageColumns } from './home-widgets'

/** Matches the element whose whole text (children included) is `text`. */
const fullText =
  (text: string | RegExp, tag = 'P') =>
  (_: string, el: Element | null) =>
    el?.tagName === tag &&
    (typeof text === 'string' ? el.textContent === text : text.test(el.textContent ?? ''))

beforeEach(() => {
  resetShellState()
  usePendingQuestions.getState().reset()
})
afterEach(() => {
  vi.restoreAllMocks()
  useRuntimeSignals.setState({ doctor: null })
})

function homeRpc() {
  return shellRpc()
    .handle('doctor.status', { status: 'ready', summary: 'All checks passed' })
    .handle('status', { uptime_ms: 3_725_000, version: '2026.11.7', provider: 'openrouter' })
    .handle('config.get', { llm: { model: 'anthropic/claude' } })
    .handle('cron.list', {
      jobs: [{ id: 'j1', name: 'Morning brief', enabled: true, next_run: NOW + 3_600_000 }],
    })
    .handle('usage.status', {
      sessions: [
        { session: 'agent:main:webchat:a', inputTokens: 1000, outputTokens: 200, updatedAt: NOW },
      ],
    })
}

describe('Home', () => {
  it('keeps the other figures when one fails, and retries that one', async () => {
    const user = userEvent.setup()
    let fail = true
    const rpc = homeRpc().handle('usage.status', () => {
      if (fail) throw new Error('usage store offline')
      return { sessions: [{ session: 'k', inputTokens: 40, outputTokens: 2, updatedAt: NOW }] }
    })
    renderShell('/home', rpc)

    const usage = await screen.findByRole('region', { name: 'Usage, all time' })
    expect(await within(usage).findByRole('alert')).toHaveTextContent('usage store offline')
    const readiness = screen.getByRole('region', { name: 'Readiness' })
    expect(await within(readiness).findByText('Ready')).toBeInTheDocument()
    expect(within(readiness).queryByRole('alert')).not.toBeInTheDocument()
    expect(await screen.findByText('openrouter')).toBeInTheDocument()

    fail = false
    await user.click(within(usage).getByRole('button', { name: 'Retry' }))
    // The figure reads as one phrase; "tokens" is set smaller beside it.
    expect(await within(usage).findByText(fullText('42 tokens'))).toBeInTheDocument()
    expect(within(usage).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('links each figure to its detail page and shows the next run and live events', async () => {
    const rpc = homeRpc()
    renderShell('/home', rpc)
    expect(await screen.findByRole('link', { name: 'Details: Health' })).toHaveAttribute(
      'href',
      '/settings/health',
    )
    expect(screen.getByRole('link', { name: 'Details: Usage' })).toHaveAttribute('href', '/usage')
    expect(screen.getByRole('link', { name: 'Details: Automations' })).toHaveAttribute(
      'href',
      '/automations',
    )
    // Label in name (WCAG 2.5.3): every link reading "Details" is named starting with it.
    const detailLinks = screen.getAllByText('Details').map((text) => text.closest('a'))
    expect(detailLinks).toHaveLength(5)
    for (const link of detailLinks) expect(link?.getAttribute('aria-label')).toMatch(/^Details: /)
    // The compact provider and uptime cells link with an arrow, named the same way.
    expect(screen.getByRole('link', { name: 'Details: Models settings' })).toHaveAttribute(
      'href',
      '/settings/models',
    )
    expect(screen.getByRole('link', { name: 'Details: Gateway settings' })).toHaveAttribute(
      'href',
      '/settings/gateway',
    )
    const next = screen.getByRole('region', { name: 'Next automation' })
    expect(await within(next).findByText(/Morning brief/)).toBeInTheDocument()
    expect(rpc.callsTo('doctor.status')).toEqual([{ agentId: 'main', deep: false }])

    act(() => rpc.emit('presence', { who: 'me' }))
    const feed = screen.getByRole('log', { name: 'Live events' })
    expect(within(feed).getByText('presence')).toBeInTheDocument()
  })
  it('sets every figure in the same face and lets a long hint wrap instead of cutting it', async () => {
    renderShell('/home', homeRpc().handle('status', { uptime_ms: 1000, version: '2026.11.7' }))
    const provider = screen.getByRole('region', { name: 'Provider and route' })
    // "Not configured" was set in monospace, unlike every other card's figure.
    expect((await within(provider).findByText('Not configured')).className).not.toMatch(/font-mono/)
    const next = screen.getByRole('region', { name: 'Next automation' })
    const hint = await within(next).findByText(/Morning brief/)
    expect(hint.className).toMatch(/line-clamp-2/)
    expect(hint.className).not.toMatch(/(^|\s)truncate(\s|$)/)
    expect(hint).toHaveAttribute('title', hint.textContent)
  })

  it('lists what keeps the runtime from ready in the readiness lead', async () => {
    const finding = (id: string, title: string, extra: Record<string, unknown> = {}) => ({
      id,
      title,
      severity: 'error',
      readinessImpact: 'blocks_ready',
      surface: 'provider',
      ...extra,
    })
    const one = homeRpc().handle('doctor.status', {
      status: 'action_required',
      summary: '1 action required',
      findings: [
        finding('provider.missing', 'Active provider is not configured', {
          detail: 'Set OPENROUTER_API_KEY for the gateway.',
        }),
        finding('gateway.rpc.ready', 'Gateway RPC ready', {
          severity: 'ok',
          readinessImpact: 'none',
        }),
      ],
    })
    const { unmount } = renderShell('/home', one)
    const readiness = await screen.findByRole('region', { name: 'Readiness' })
    const list = await within(readiness).findByRole('list', { name: 'What needs attention' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(1)
    // A lone finding also says what to do about it.
    expect(list).toHaveTextContent('Active provider is not configured')
    expect(list).toHaveTextContent('Set OPENROUTER_API_KEY for the gateway.')
    expect(list).not.toHaveTextContent('Gateway RPC ready')
    expect(within(readiness).getByRole('link', { name: /Review and fix/ })).toHaveAttribute(
      'href',
      '/settings/health',
    )
    unmount()

    renderShell(
      '/home',
      homeRpc().handle('doctor.status', {
        status: 'degraded',
        summary: 'Ready, 4 degraded checks',
        findings: ['a', 'b', 'c', 'd'].map((id) =>
          finding(id, `Check ${id}`, { severity: 'warn', readinessImpact: 'degrades' }),
        ),
      }),
    )
    const many = await within(await screen.findByRole('region', { name: 'Readiness' })).findByRole(
      'list',
      { name: 'What needs attention' },
    )
    expect(
      within(many)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['Check aprovider', 'Check bprovider', 'Check cprovider', '1 more finding'])
  })

  it('lists no findings when the runtime is ready', async () => {
    renderShell('/home', homeRpc())
    const readiness = await screen.findByRole('region', { name: 'Readiness' })
    expect(await within(readiness).findByText('Ready')).toBeInTheDocument()
    expect(within(readiness).queryByRole('list')).toBeNull()
  })
})

describe('Usage', () => {
  it('keeps the loaded data under an error notice when a refresh fails', async () => {
    const user = userEvent.setup()
    let fail = false
    const rpc = shellRpc().handle('usage.status', () => {
      if (fail) throw new Error('gateway timed out')
      return {
        sessions: [
          {
            session: 'agent:main:webchat:a',
            inputTokens: 1200,
            outputTokens: 300,
            costUsd: 0.5,
            costSource: 'provider_billed',
            updatedAt: NOW,
            modelBreakdown: [
              { model: 'openai/gpt', inputTokens: 1000, outputTokens: 200, costUsd: 0.4 },
              { model: 'anthropic/claude', inputTokens: 200, outputTokens: 100, costUsd: 0.1 },
            ],
          },
        ],
      }
    })
    renderShell('/usage', rpc)
    const table = () => within(screen.getByRole('table'))
    expect(
      await within(await screen.findByRole('table')).findByRole('link', {
        name: 'agent:main:webchat:a',
      }),
    ).toBeInTheDocument()

    // The multi-model row expands into its per-model breakdown.
    await user.click(screen.getByRole('button', { name: /auto · 2 models/ }))
    expect(table().getByText('gpt')).toBeInTheDocument()

    fail = true
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('gateway timed out')
    expect(within(alert).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(table().getByRole('link', { name: 'agent:main:webchat:a' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Spend' })).toHaveTextContent('$0.5000')

    fail = false
    await user.click(within(alert).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })

  it('sorts the sessions table by a column', async () => {
    const user = userEvent.setup()
    const rpc = shellRpc().handle('usage.status', {
      sessions: [
        { session: 'small', inputTokens: 10, updatedAt: NOW },
        { session: 'large', inputTokens: 900, updatedAt: NOW - 1000 },
      ],
    })
    renderShell('/usage', rpc)
    await within(await screen.findByRole('table')).findByRole('link', { name: 'small' })
    await user.click(screen.getByRole('button', { name: /^Input/ }))
    const sessionLinks = () =>
      within(screen.getByRole('table'))
        .getAllByRole('link')
        .map((a) => a.textContent)
    expect(sessionLinks()).toEqual(['large', 'small'])
    await user.click(screen.getByRole('button', { name: /^Input/ }))
    expect(sessionLinks()).toEqual(['small', 'large'])
  })

  it('cuts long session keys in the middle so the distinguishing tail always shows', async () => {
    const long = 'agent:main:webchat:2f9c1d7e-4b8a-4e0f-9a51-tail-one'
    const rpc = shellRpc().handle('usage.status', {
      sessions: [
        { session: long, inputTokens: 900, updatedAt: NOW },
        { session: 'a:b', inputTokens: 10, updatedAt: NOW },
      ],
    })
    renderShell('/usage', rpc)
    const table = within(await screen.findByRole('table'))
    const chart = within(
      (await screen.findByRole('heading', { name: 'Top sessions' })).closest('section')!,
    )
    for (const scope of [table, chart]) {
      // The whole key stays the link's text (accessible name, copy) and title.
      const link = scope.getByRole('link', { name: long })
      expect(link).toHaveAttribute('title', expect.stringContaining(long))
      // The head truncates; the last characters sit in their own unshrinking span.
      const [head, tail] = Array.from(link.children)
      expect(head).toHaveClass('truncate')
      expect(tail).toHaveTextContent(/^tail-one$|-tail-one$/)
      expect(tail).toHaveClass('shrink-0')
      expect(head!.textContent! + tail!.textContent!).toBe(long)
      expect(scope.getByRole('link', { name: 'a:b' })).toBeInTheDocument()
    }
  })
})

describe('Inbox', () => {
  it('lists an approval and a question and answers both in place', async () => {
    const user = userEvent.setup()
    const resolve = vi.spyOn(approvalMonitor, 'resolve').mockResolvedValue(undefined as never)
    const rpc = shellRpc().handle('chat.send', { ok: true })
    useApprovals.getState().setFromPoll(
      [
        {
          id: 'a1',
          namespace: 'exec',
          toolName: 'shell',
          command: 'rm -rf build',
          sessionKey: 'agent:main:webchat:x',
        },
      ],
      'prompt',
    )
    usePendingQuestions.getState().present({
      id: 'question:agent:main:webchat:y',
      sessionKey: 'agent:main:webchat:y',
      questions: [
        {
          question: 'Which branch?',
          options: [{ label: 'main' }, { label: 'dev' }],
          multiSelect: false,
        },
      ],
      toolUseId: 't1',
      presentedAt: NOW,
    })
    renderShell('/inbox', rpc)

    const list = await screen.findByRole('region', { name: 'Waiting items' })
    const approval = within(list).getByRole('article', { name: /^Approval · exec: shell/ })
    await user.click(within(approval).getByRole('button', { name: 'Allow once' }))
    expect(resolve).toHaveBeenCalledWith(expect.objectContaining({ id: 'a1' }), 'once')

    const question = within(list).getByRole('article', { name: 'Question: Which branch?' })
    await user.click(within(question).getByRole('button', { name: 'dev' }))
    await user.click(within(question).getByRole('button', { name: 'Send answer' }))
    await waitFor(() =>
      expect(rpc.callsTo('chat.send')[0]).toMatchObject({
        message: 'dev',
        sessionKey: 'agent:main:webchat:y',
      }),
    )
    await waitFor(() =>
      expect(
        within(list).queryByRole('article', { name: 'Question: Which branch?' }),
      ).not.toBeInTheDocument(),
    )
  })

  it('says nothing is waiting when the list is empty and live', async () => {
    renderShell('/inbox')
    expect(await screen.findByRole('heading', { name: 'All caught up' })).toBeInTheDocument()
    expect(screen.getByText(/Nothing needs you right now/)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Waiting items' })).toBeNull()
  })
})

// ── Review fixes (2026-11-10) ───────────────────────────────────────────────

describe('Home usage and sessions figures', () => {
  it('reports all-time usage and how many sessions were active today, not lifetime as "today"', async () => {
    const yesterday = startOfLocalDay(NOW) - 60_000
    const rpc = homeRpc().handle('usage.status', {
      sessions: [
        // A session that spent $40 over weeks and got one message today.
        { session: 'old', inputTokens: 900, outputTokens: 100, costUsd: 40, updatedAt: NOW },
        { session: 'idle', inputTokens: 10, outputTokens: 0, costUsd: 0.5, updatedAt: yesterday },
      ],
    })
    renderShell('/home', rpc)
    const usage = await screen.findByRole('region', { name: 'Usage, all time' })
    expect(await within(usage).findByText(fullText(/^1,?010 tokens$/))).toBeInTheDocument()
    expect(within(usage).getByText(fullText('$40.50 spent'))).toBeInTheDocument()
    expect(within(usage).getByText('1 session active today')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Today' })).toBeNull()
  })

  it('keeps the Home arithmetic on the local day boundary', () => {
    const midnight = new Date(2026, 10, 10).getTime()
    const figures = homeUsageFigures(
      [
        { inputTokens: 5, outputTokens: 5, costUsd: 1, updatedAt: midnight },
        { inputTokens: 1, outputTokens: 1, costUsd: 2, updatedAt: midnight - 1 },
        { inputTokens: 1, outputTokens: 0, costUsd: 0 },
      ],
      midnight + 3_600_000,
    )
    expect(figures).toEqual({ tokens: 13, cost: 3, activeToday: 1 })
  })

  it(
    'says how many sessions exist when the list is cut at the limit',
    { timeout: 15_000 },
    async () => {
      const many = Array.from({ length: SESSIONS_LIST_LIMIT }, (_, i) =>
        sessionRow(`agent:main:webchat:s${i}`),
      )
      const rpc = homeRpc().handle('sessions.list', (p: Record<string, unknown>) =>
        p.limit === 1
          ? { sessions: many.slice(0, 1), total: 1234 }
          : { sessions: many, total: 1234 },
      )
      renderShell('/home', rpc)
      const sessions = await screen.findByRole('region', { name: 'Sessions' })
      // 500 sidebar rows render first; give them time on a loaded machine.
      expect(await within(sessions).findByText('1,234', {}, { timeout: 5000 })).toBeInTheDocument()
    },
  )

  it('says "500+" when a gateway does not report the total', { timeout: 15_000 }, async () => {
    const many = Array.from({ length: SESSIONS_LIST_LIMIT }, (_, i) =>
      sessionRow(`agent:main:webchat:s${i}`),
    )
    renderShell('/home', homeRpc().handle('sessions.list', { sessions: many }))
    const sessions = await screen.findByRole('region', { name: 'Sessions' })
    expect(await within(sessions).findByText('500+', {}, { timeout: 5000 })).toBeInTheDocument()
  })
})

describe('Usage ranges, chart, models and export', () => {
  const rows = () => [
    {
      session: 'agent:main:webchat:recent',
      inputTokens: 100,
      outputTokens: 20,
      costUsd: 2,
      model: 'openai/gpt',
      updatedAt: NOW - 86_400_000,
    },
    {
      session: 'agent:main:webchat:pricey',
      inputTokens: 10,
      outputTokens: 5,
      costUsd: 9,
      model: 'anthropic/claude',
      updatedAt: NOW - 2 * 86_400_000,
    },
    {
      session: 'agent:main:webchat:old',
      inputTokens: 5000,
      outputTokens: 0,
      costUsd: 1,
      model: 'openai/gpt',
      updatedAt: NOW - 40 * 86_400_000,
    },
  ]

  it('filters by sessions active in the range and says the figures are lifetime totals', async () => {
    const user = userEvent.setup()
    renderShell('/usage', shellRpc().handle('usage.status', { sessions: rows() }))
    const table = within(await screen.findByRole('table'))
    await table.findByRole('link', { name: 'agent:main:webchat:recent' })
    // The default range is the last 7 days: the 40-day-old session is out.
    expect(table.queryByRole('link', { name: 'agent:main:webchat:old' })).toBeNull()
    expect(screen.getByRole('region', { name: 'Spend' })).toHaveTextContent('$11.0000')
    expect(screen.getByRole('group', { name: 'Sessions active in' })).toBeInTheDocument()
    expect(screen.getByText(/lifetime total/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'All time' }))
    expect(table.getByRole('link', { name: 'agent:main:webchat:old' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Spend' })).toHaveTextContent('$12.0000')
    expect(localStorage.getItem('agentos-usage-range')).toBe('all')
  })

  it('says where the spend comes from as a sentence per cost source', async () => {
    const sourced = rows().map((row, i) => ({
      ...row,
      updatedAt: NOW - 86_400_000,
      costSource: i === 0 ? 'provider_billed' : 'agentos_estimate',
    }))
    renderShell('/usage', shellRpc().handle('usage.status', { sessions: sourced }))
    const spend = await screen.findByRole('region', { name: 'Spend' })
    // Was "actual 1 · estimated 2", which read like a broken string.
    expect(
      await within(spend).findByText('Actual for 1 session · Estimated for 2 sessions'),
    ).toBeInTheDocument()
  })

  it('charts the top sessions by tokens or by cost, and rolls usage up by model', async () => {
    const user = userEvent.setup()
    renderShell('/usage', shellRpc().handle('usage.status', { sessions: rows() }))
    const chart = within(
      (await screen.findByRole('heading', { name: 'Top sessions' })).closest('section')!,
    )
    const order = () => chart.getAllByRole('listitem').map((li) => li.getAttribute('aria-label'))
    expect(order()).toEqual(['agent:main:webchat:recent: 120', 'agent:main:webchat:pricey: 15'])
    await user.click(chart.getByRole('button', { name: 'Cost' }))
    expect(order()).toEqual([
      'agent:main:webchat:pricey: $9.0000',
      'agent:main:webchat:recent: $2.0000',
    ])

    const models = within(screen.getByRole('heading', { name: 'By model' }).closest('section')!)
    const claude = models.getByRole('listitem', { name: 'anthropic/claude' })
    expect(claude).toHaveTextContent('$9.0000')
    expect(claude).toHaveTextContent('81.8% of spend')
    expect(models.getByRole('listitem', { name: 'openai/gpt' })).toHaveTextContent('1 session')
  })

  it('exports only the sessions in range as CSV', async () => {
    const user = userEvent.setup()
    const blobs: Blob[] = []
    const create = vi.fn((blob: Blob) => {
      blobs.push(blob)
      return 'blob:usage'
    })
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() })
    let download = ''
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      download = this.download
    })
    renderShell('/usage', shellRpc().handle('usage.status', { sessions: rows() }))
    await within(await screen.findByRole('table')).findByRole('link', {
      name: 'agent:main:webchat:recent',
    })
    await user.click(screen.getByRole('button', { name: 'Export CSV' }))
    expect(click).toHaveBeenCalledTimes(1)
    expect(download).toBe('agentos-usage-7d.csv')
    const text = await new Promise<string>((resolve) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.readAsText(blobs[0]!)
    })
    const lines = text.split('\n')
    expect(lines[0]).toMatch(/^"session","input_tokens"/)
    expect(lines).toHaveLength(3)
    expect(text).toContain('"agent:main:webchat:pricey"')
    expect(text).not.toContain('agent:main:webchat:old')
  })
})

describe('Inbox fixes', () => {
  it('drops a question whose session has been deleted', async () => {
    usePendingQuestions.getState().present({
      id: 'question:agent:main:webchat:gone',
      sessionKey: 'agent:main:webchat:gone',
      questions: [{ question: 'Proceed?', options: [{ label: 'yes' }], multiSelect: false }],
      toolUseId: 't1',
      presentedAt: NOW - 60_000,
    })
    renderShell('/inbox', shellRpc([sessionRow('agent:main:webchat:other')]))
    await waitFor(() => expect(usePendingQuestions.getState().items).toEqual([]))
    expect(screen.queryByRole('article', { name: 'Question: Proceed?' })).toBeNull()
  })

  it('keeps a question that arrived after the session list was read', async () => {
    usePendingQuestions.getState().present({
      id: 'question:agent:main:webchat:fresh',
      sessionKey: 'agent:main:webchat:fresh',
      questions: [{ question: 'Proceed?', options: [{ label: 'yes' }], multiSelect: false }],
      toolUseId: 't1',
      presentedAt: Date.now() + 60_000,
    })
    renderShell('/inbox', shellRpc([sessionRow('agent:main:webchat:other')]))
    expect(await screen.findByRole('article', { name: 'Question: Proceed?' })).toBeInTheDocument()
  })

  it('names the requesting agent and copies the approval command', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    useApprovals.getState().setFromPoll(
      [
        {
          id: 'a1',
          namespace: 'exec',
          toolName: 'shell',
          agent: 'research',
          command: 'rm -rf build',
          sessionKey: 'agent:research:webchat:x',
        },
      ],
      'prompt',
    )
    renderShell('/inbox')
    const approval = await screen.findByRole('article', { name: /^Approval · exec: shell/ })
    expect(within(approval).getByText('Agent research')).toBeInTheDocument()
    await user.click(within(approval).getByRole('button', { name: 'Copy command' }))
    expect(writeText).toHaveBeenCalledWith('rm -rf build')
  })

  it('brings the approval a push notification links to into view', async () => {
    const scrolled: Element[] = []
    const scrollIntoView = vi
      .spyOn(Element.prototype, 'scrollIntoView')
      .mockImplementation(function (this: Element) {
        scrolled.push(this)
      })
    useApprovals.getState().setFromPoll(
      [
        { id: 'a1', namespace: 'exec', toolName: 'shell', command: 'ls' },
        { id: 'a2', namespace: 'exec', toolName: 'git', command: 'git push' },
      ],
      'prompt',
    )
    renderShell('/inbox?approval=a2')
    const list = await screen.findByRole('region', { name: 'Waiting items' })
    const target = await waitFor(() => {
      const el = list.querySelector('[data-approval-id="a2"]')
      expect(el).not.toBeNull()
      return el as HTMLElement
    })
    expect(target).toHaveAttribute('data-highlighted')
    expect(list.querySelector('[data-approval-id="a1"]')).not.toHaveAttribute('data-highlighted')
    await waitFor(() => expect(target).toHaveFocus())
    expect(scrolled).toEqual([target])
    scrollIntoView.mockRestore()
  })

  it('copies a fix-step command', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderShell('/inbox')
    act(() =>
      useRuntimeSignals.setState({
        doctor: normalizeDoctor(
          {
            status: 'degraded',
            ready: true,
            findings: [
              {
                id: 'disk.low',
                severity: 'warn',
                title: 'Disk almost full',
                fixSteps: [{ label: 'Prune old logs', command: 'agentos logs prune' }],
              },
            ],
          },
          NOW,
        ),
      }),
    )
    const issue = await screen.findByRole('article', { name: /Disk almost full/ })
    const toggle = within(issue).getByRole('button', { name: 'Open doctor fix steps' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await user.click(within(issue).getByRole('button', { name: 'Copy the command of step 1' }))
    expect(writeText).toHaveBeenCalledWith('agentos logs prune')
  })
})

// ── Pages v2: Home dashboard and Inbox triage (2026-11-12) ─────────────────

describe('Home dashboard', () => {
  it('greets by the hour and states readiness in a pill beside it', async () => {
    expect(homeGreeting(8)).toBe('Good morning')
    expect(homeGreeting(14)).toBe('Good afternoon')
    expect(homeGreeting(21)).toBe('Good evening')
    expect(homeGreeting(3)).toBe('Good evening')
    renderShell('/home', homeRpc())
    const title = await screen.findByRole('heading', { level: 1 })
    expect(title.textContent).toMatch(/^Good (morning|afternoon|evening)$/)
    expect(await screen.findByText('All systems ready')).toBeInTheDocument()
    // The one-line summary comes from the figures already loaded.
    expect(await screen.findByText(/Running on openrouter/)).toBeInTheDocument()
  })

  it('sums the sessions up by channel and recent activity without repeating the rail', async () => {
    const rows = [
      sessionRow('agent:main:webchat:a', { display_name: 'Unique title A' }),
      sessionRow('agent:main:webchat:b'),
      sessionRow('agent:main:telegram:direct:1:c'),
      sessionRow('agent:main:webchat:d', { run_status: 'running' }),
    ]
    renderShell('/home', homeRpc().handle('sessions.list', { sessions: rows }))
    const sessions = await screen.findByRole('region', { name: 'Sessions' })
    expect(await within(sessions).findByText('4')).toBeInTheDocument()
    expect(within(sessions).getByText('1 running')).toBeInTheDocument()
    const split = within(sessions).getByRole('list', { name: 'Sessions by channel' })
    expect(
      within(split)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['webchat3', 'telegram1'])
    // Titles stay in the rail: Home does not list them a second time.
    expect(screen.getAllByText('Unique title A')).toHaveLength(1)
  })

  it('places each session on the past day by when it was last active', () => {
    const now = NOW
    const pulse = sessionsPulse(
      [
        { key: 'agent:main:webchat:a', updated_at: now - 6 * 3_600_000 },
        { key: 'agent:main:webchat:b', updated_at: now - 2 * 86_400_000 },
        { key: 'agent:main:cron:c', updated_at: now },
        { key: 'agent:main:webchat:child', parent_session_key: 'agent:main:webchat:a' },
      ],
      now,
    )
    expect(pulse.day).toEqual([75, 100])
    expect(pulse.channels).toEqual([
      ['webchat', 2],
      ['cron', 1],
    ])
    expect(pulse.running).toBe(0)
  })

  it('shows what an empty Home will hold instead of zeros alone', async () => {
    renderShell(
      '/home',
      homeRpc().handle('usage.status', { sessions: [] }).handle('cron.list', { jobs: [] }),
    )
    const usage = await screen.findByRole('region', { name: 'Usage, all time' })
    expect(await within(usage).findByText('No usage yet')).toBeInTheDocument()
    expect(
      within(screen.getByRole('region', { name: 'Sessions' })).getByText(
        'Waiting for the first session',
      ),
    ).toBeInTheDocument()
    const next = screen.getByRole('region', { name: 'Next automation' })
    expect(await within(next).findByText('Nothing scheduled')).toBeInTheDocument()
    expect(within(next).getByRole('link', { name: /Create an automation/ })).toHaveAttribute(
      'href',
      '/automations',
    )
  })

  it('charts the newest sessions with usage, oldest on the left', () => {
    const columns = usageColumns([
      { session: 'old', inputTokens: 10, outputTokens: 5, updatedAt: 1_000 },
      { session: 'none', inputTokens: 0, outputTokens: 0, updatedAt: 3_000 },
      { session: 'new', inputTokens: 30, outputTokens: 0, updatedAt: 2_000 },
    ])
    expect(columns).toEqual([
      { key: 'old', input: 10, output: 5 },
      { key: 'new', input: 30, output: 0 },
    ])
    const many = Array.from({ length: 20 }, (_, i) => ({
      session: `s${i}`,
      inputTokens: 1,
      updatedAt: i * 1000,
    }))
    expect(usageColumns(many).map((c) => c.key)).toEqual(
      Array.from({ length: 14 }, (_, i) => `s${i + 6}`),
    )
  })

  it('measures the countdown ring from the last run, else the cron period', () => {
    const next = new Date(2026, 10, 12, 9, 0).getTime()
    expect(runCycle({ next_run: next, last_run: next - 3_600_000 })).toEqual({
      start: next - 3_600_000,
      end: next,
    })
    // Daily at 9:00 and never run: the wait spans the day before.
    expect(runCycle({ next_run: next, expression: '0 9 * * *' })).toEqual({
      start: next - 86_400_000,
      end: next,
    })
    expect(runCycle({ next_run: next, expression: '*/30 * * * *' })).toEqual({
      start: next - 1_800_000,
      end: next,
    })
    expect(runCycle({ next_run: null })).toBeNull()
  })
})

describe('Inbox triage', () => {
  it('groups waiting items by kind with counts, first row of each group open', async () => {
    const user = userEvent.setup()
    useApprovals.getState().setFromPoll(
      [
        { id: 'a1', namespace: 'exec', toolName: 'shell', command: 'ls' },
        { id: 'a2', namespace: 'exec', toolName: 'git', command: 'git push' },
      ],
      'prompt',
    )
    usePendingQuestions.getState().present({
      id: 'question:agent:main:webchat:y',
      sessionKey: 'agent:main:webchat:y',
      questions: [{ question: 'Which branch?', options: [{ label: 'main' }], multiSelect: false }],
      toolUseId: 't1',
      presentedAt: NOW,
    })
    renderShell('/inbox')
    const list = await screen.findByRole('region', { name: 'Waiting items' })
    const approvals = within(list).getByRole('region', { name: /^Approvals/ })
    expect(within(approvals).getByRole('heading', { level: 2 })).toHaveTextContent('Approvals2')
    expect(within(list).getByRole('region', { name: /^Questions/ })).toBeInTheDocument()
    expect(within(list).queryByRole('region', { name: /^Issues/ })).toBeNull()
    expect(screen.getByText('3 waiting')).toBeInTheDocument()

    const [first, second] = within(approvals).getAllByRole('article')
    const head = (row: HTMLElement) =>
      within(row)
        .getAllByRole('button')
        .find((b) => b.hasAttribute('aria-controls'))!
    expect(head(first!)).toHaveAttribute('aria-expanded', 'true')
    expect(head(second!)).toHaveAttribute('aria-expanded', 'false')
    // A folded row previews its command.
    expect(within(second!).getByText('git push', { selector: '.ib-preview' })).toBeInTheDocument()
    await user.click(head(second!))
    expect(head(second!)).toHaveAttribute('aria-expanded', 'true')
    await user.click(head(first!))
    expect(head(first!)).toHaveAttribute('aria-expanded', 'false')
  })

  it('opens the row a notification points at, wherever it sits in its group', async () => {
    useApprovals.getState().setFromPoll(
      [
        { id: 'a1', namespace: 'exec', toolName: 'shell', command: 'ls' },
        { id: 'a2', namespace: 'exec', toolName: 'git', command: 'git push' },
      ],
      'prompt',
    )
    renderShell('/inbox?approval=a2')
    const row = await screen.findByRole('article', { name: /^Approval · exec: git/ })
    expect(
      within(row)
        .getAllByRole('button')
        .find((b) => b.hasAttribute('aria-controls')),
    ).toHaveAttribute('aria-expanded', 'true')
  })
})
