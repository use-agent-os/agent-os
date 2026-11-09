import '@/test/web'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { usePendingQuestions } from '@/lib/pending-questions'
import { approvalMonitor, useApprovals } from '@/services/approval-monitor'
import { NOW, renderShell, resetShellState, shellRpc } from '@/test/web-shell'

beforeEach(() => {
  resetShellState()
  usePendingQuestions.getState().reset()
})
afterEach(() => {
  vi.restoreAllMocks()
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

    const usage = await screen.findByRole('region', { name: 'Today' })
    expect(await within(usage).findByRole('alert')).toHaveTextContent('usage store offline')
    const readiness = screen.getByRole('region', { name: 'Readiness' })
    expect(await within(readiness).findByText('Ready')).toBeInTheDocument()
    expect(within(readiness).queryByRole('alert')).not.toBeInTheDocument()
    expect(await screen.findByText('openrouter')).toBeInTheDocument()

    fail = false
    await user.click(within(usage).getByRole('button', { name: 'Retry' }))
    expect(await within(usage).findByText('42 tokens')).toBeInTheDocument()
    expect(within(usage).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('links each figure to its detail page and shows the next run and live events', async () => {
    const rpc = homeRpc()
    renderShell('/home', rpc)
    expect(await screen.findByRole('link', { name: 'Open Health' })).toHaveAttribute(
      'href',
      '/settings/health',
    )
    expect(screen.getByRole('link', { name: 'Open Usage' })).toHaveAttribute('href', '/usage')
    expect(screen.getByRole('link', { name: 'Open Automations' })).toHaveAttribute(
      'href',
      '/automations',
    )
    const next = screen.getByRole('region', { name: 'Next automation' })
    expect(await within(next).findByText(/Morning brief/)).toBeInTheDocument()
    expect(rpc.callsTo('doctor.status')).toEqual([{ agentId: 'main', deep: false }])

    act(() => rpc.emit('presence', { who: 'me' }))
    const feed = screen.getByRole('log', { name: 'Live events' })
    expect(within(feed).getByText('presence')).toBeInTheDocument()
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
    expect(await screen.findByRole('link', { name: 'agent:main:webchat:a' })).toBeInTheDocument()

    // The multi-model row expands into its per-model breakdown.
    await user.click(screen.getByRole('button', { name: /auto · 2 models/ }))
    expect(screen.getByText('gpt')).toBeInTheDocument()

    fail = true
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('gateway timed out')
    expect(within(alert).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'agent:main:webchat:a' })).toBeInTheDocument()
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
    await screen.findByRole('link', { name: 'small' })
    await user.click(screen.getByRole('button', { name: /^Input/ }))
    const sessionLinks = () =>
      within(screen.getByRole('table'))
        .getAllByRole('link')
        .map((a) => a.textContent)
    expect(sessionLinks()).toEqual(['large', 'small'])
    await user.click(screen.getByRole('button', { name: /^Input/ }))
    expect(sessionLinks()).toEqual(['small', 'large'])
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
    expect(await screen.findByText('Nothing needs you')).toBeInTheDocument()
  })
})
