import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApprovalPrompt } from './ApprovalPrompt'
import { approvalMonitor, useApprovals, type Approval } from '@/services/approval-monitor'

// The modal drives resolution through the singleton; spy on it and assert the
// action mapping. resolve() itself is unit-tested in approval-monitor.test.ts.
vi.mock('@/services/approval-monitor', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/approval-monitor')>()
  return {
    ...actual,
    approvalMonitor: {
      resolve: vi.fn().mockResolvedValue(undefined),
    },
  }
})

const resolveSpy = vi.mocked(approvalMonitor.resolve)

function setPending(pending: Approval[], mode = 'prompt') {
  useApprovals.setState({ pending, count: pending.length, mode })
}

describe('ApprovalPrompt', () => {
  beforeEach(() => {
    resolveSpy.mockReset().mockResolvedValue(undefined)
    useApprovals.setState({ pending: [], count: 0, mode: 'prompt' })
  })
  afterEach(() => {
    useApprovals.setState({ pending: [], count: 0, mode: 'prompt' })
  })

  it('renders nothing when the queue is empty', () => {
    const { container } = render(<ApprovalPrompt />)
    expect(container).toBeEmptyDOMElement()
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('renders the first pending approval as an alertdialog with tool, meta, and command', () => {
    setPending([
      {
        id: 'a1',
        namespace: 'exec',
        toolName: 'shell',
        sessionKey: 's-42',
        command: 'rm -rf /tmp/x',
      },
    ])
    render(<ApprovalPrompt />)
    const dialog = screen.getByRole('alertdialog')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(screen.getByText('Approval Required')).toBeInTheDocument()
    expect(screen.getByText('shell')).toBeInTheDocument()
    expect(screen.getByText('Namespace: exec · Mode: prompt · Session: s-42')).toBeInTheDocument()
    expect(screen.getByText('rm -rf /tmp/x')).toBeInTheDocument()
  })

  it('prompts only the FIRST pending item', () => {
    setPending([
      { id: 'a1', namespace: 'exec', toolName: 'first', command: 'ls' },
      { id: 'a2', namespace: 'exec', toolName: 'second', command: 'pwd' },
    ])
    render(<ApprovalPrompt />)
    expect(screen.getByText('first')).toBeInTheDocument()
    expect(screen.queryByText('second')).not.toBeInTheDocument()
  })

  it('shows "Always Allow This Type" only for exec items with a command', () => {
    setPending([{ id: 'a1', namespace: 'exec', command: 'ls', toolName: 't' }])
    const { rerender } = render(<ApprovalPrompt />)
    expect(screen.getByRole('button', { name: /always allow/i })).toBeInTheDocument()

    // Plugin namespace → no "Always Allow".
    setPending([{ id: 'a2', namespace: 'plugin', toolName: 'p' }])
    rerender(<ApprovalPrompt />)
    expect(screen.queryByRole('button', { name: /always allow/i })).not.toBeInTheDocument()
  })

  it('falls back to actionKind / "Tool execution" for the tool label', () => {
    setPending([{ id: 'a1', namespace: 'plugin', actionKind: 'write_file' }])
    const { rerender } = render(<ApprovalPrompt />)
    expect(screen.getByText('write_file')).toBeInTheDocument()

    setPending([{ id: 'a2', namespace: 'plugin' }])
    rerender(<ApprovalPrompt />)
    expect(screen.getByText('Tool execution')).toBeInTheDocument()
  })

  it('renders the detail JSON block when no command is present', () => {
    setPending([{ id: 'a1', namespace: 'plugin', toolName: 'p', args: { path: '/etc' } }])
    render(<ApprovalPrompt />)
    expect(screen.getByText(/"path": "\/etc"/)).toBeInTheDocument()
  })

  it('Approve This Time resolves with action "once"', async () => {
    const item: Approval = { id: 'a1', namespace: 'exec', command: 'ls', toolName: 't' }
    setPending([item])
    render(<ApprovalPrompt />)
    screen.getByRole('button', { name: /approve this time/i }).click()
    await waitFor(() => expect(resolveSpy).toHaveBeenCalledWith(item, 'once'))
  })

  it('Always Allow resolves with action "always"', async () => {
    const item: Approval = { id: 'a1', namespace: 'exec', command: 'ls', toolName: 't' }
    setPending([item])
    render(<ApprovalPrompt />)
    screen.getByRole('button', { name: /always allow/i }).click()
    await waitFor(() => expect(resolveSpy).toHaveBeenCalledWith(item, 'always'))
  })

  it('Bypass Approvals resolves with action "bypass"', async () => {
    const item: Approval = { id: 'a1', namespace: 'exec', command: 'ls', toolName: 't' }
    setPending([item])
    render(<ApprovalPrompt />)
    screen.getByRole('button', { name: /bypass approvals/i }).click()
    await waitFor(() => expect(resolveSpy).toHaveBeenCalledWith(item, 'bypass'))
  })

  it('Deny resolves with action "deny"', async () => {
    const item: Approval = { id: 'a1', namespace: 'exec', command: 'ls', toolName: 't' }
    setPending([item])
    render(<ApprovalPrompt />)
    screen.getByRole('button', { name: /^deny$/i }).click()
    await waitFor(() => expect(resolveSpy).toHaveBeenCalledWith(item, 'deny'))
  })

  it('disables all buttons while a resolve is in flight', async () => {
    let release: (() => void) | undefined
    resolveSpy.mockImplementation(
      () =>
        new Promise<void>((r) => {
          release = r
        }),
    )
    setPending([{ id: 'a1', namespace: 'exec', command: 'ls', toolName: 't' }])
    render(<ApprovalPrompt />)
    const deny = screen.getByRole('button', { name: /^deny$/i })
    screen.getByRole('button', { name: /approve this time/i }).click()
    await waitFor(() => expect(deny).toBeDisabled())
    release?.()
  })
})
