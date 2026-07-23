import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

  it('traps focus, blocks implicit dismissal, and restores the previous focus', () => {
    const trigger = document.createElement('button')
    trigger.textContent = 'External trigger'
    document.body.appendChild(trigger)
    trigger.focus()

    setPending([{ id: 'a1', namespace: 'exec', command: 'ls', toolName: 'shell' }])
    render(<ApprovalPrompt />)

    const dialog = screen.getByRole('alertdialog')
    const first = screen.getByRole('button', { name: /copy command/i })
    const last = screen.getByRole('button', { name: /^deny$/i })
    expect(first).toHaveFocus()

    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()

    fireEvent.keyDown(dialog, { key: 'Escape' })
    fireEvent.mouseDown(document.querySelector('.approval-backdrop')!)
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()

    act(() => setPending([]))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
    trigger.remove()
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

  // Reviewer findings 1-3: the prompt must pin the item captured at open time,
  // survive transient poll failures that zero the badge, and release the pin on
  // resolve success.
  describe('pinning + poll resilience', () => {
    function updateStore(next: { pending: Approval[]; count?: number; mode?: string }) {
      act(() => {
        useApprovals.setState({
          pending: next.pending,
          count: next.count ?? next.pending.length,
          mode: next.mode ?? 'prompt',
        })
      })
    }

    it('keeps an open prompt when a transient poll failure zeroes the badge (finding 1)', () => {
      // A failing poll calls zeroBadge() → count 0 but pending untouched. The
      // open prompt must NOT unmount mid-read.
      setPending([{ id: 'a1', namespace: 'exec', command: 'ls', toolName: 'shell' }])
      render(<ApprovalPrompt />)
      expect(screen.getByRole('alertdialog')).toBeInTheDocument()
      expect(screen.getByText('shell')).toBeInTheDocument()
      // Simulate the service's failure path: badge zeroed, pending preserved.
      act(() => useApprovals.getState().zeroBadge())
      expect(screen.getByRole('alertdialog')).toBeInTheDocument()
      expect(screen.getByText('shell')).toBeInTheDocument()
    })

    it('does NOT swap the displayed item when the queue head changes while open (finding 2)', () => {
      setPending([{ id: 'a1', namespace: 'exec', command: 'ls', toolName: 'first' }])
      render(<ApprovalPrompt />)
      expect(screen.getByText('first')).toBeInTheDocument()
      // A new higher-priority approval arrives at the head on a later poll; the
      // pinned item (a1) is still pending, so the shown item must not change.
      updateStore({
        pending: [
          { id: 'a2', namespace: 'exec', command: 'rm', toolName: 'second' },
          { id: 'a1', namespace: 'exec', command: 'ls', toolName: 'first' },
        ],
      })
      expect(screen.getByText('first')).toBeInTheDocument()
      expect(screen.queryByText('second')).not.toBeInTheDocument()
    })

    it('advances to the new head once the pinned item leaves the queue (finding 2 follow-through)', () => {
      setPending([{ id: 'a1', namespace: 'exec', command: 'ls', toolName: 'first' }])
      render(<ApprovalPrompt />)
      expect(screen.getByText('first')).toBeInTheDocument()
      // a1 is resolved elsewhere and drops out; the next poll carries only a2.
      updateStore({
        pending: [{ id: 'a2', namespace: 'exec', command: 'rm', toolName: 'second' }],
      })
      expect(screen.getByText('second')).toBeInTheDocument()
      expect(screen.queryByText('first')).not.toBeInTheDocument()
    })

    it('closes when the pinned item is resolved and the queue empties', () => {
      setPending([{ id: 'a1', namespace: 'exec', command: 'ls', toolName: 'first' }])
      render(<ApprovalPrompt />)
      expect(screen.getByRole('alertdialog')).toBeInTheDocument()
      updateStore({ pending: [] })
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    })

    it('releases the pin on resolve success even before the store updates (finding 3)', async () => {
      // resolve() resolves; the component must clear the pinned item itself
      // (setItem(null)) rather than waiting for the re-poll to drain pending.
      const item: Approval = { id: 'a1', namespace: 'exec', command: 'ls', toolName: 'shell' }
      setPending([item])
      render(<ApprovalPrompt />)
      screen.getByRole('button', { name: /approve this time/i }).click()
      await waitFor(() => expect(resolveSpy).toHaveBeenCalledWith(item, 'once'))
      // Pin released → dialog gone, even though the store still lists a1 (the
      // re-poll that drains it hasn't been simulated here).
      await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    })

    it('keeps the pinned item on resolve FAILURE so the operator can retry (finding 3)', async () => {
      resolveSpy.mockRejectedValueOnce(new Error('boom'))
      const item: Approval = { id: 'a1', namespace: 'exec', command: 'ls', toolName: 'shell' }
      setPending([item])
      render(<ApprovalPrompt />)
      screen.getByRole('button', { name: /approve this time/i }).click()
      await waitFor(() => expect(resolveSpy).toHaveBeenCalled())
      // The prompt stays open on the same item (buttons re-enabled).
      expect(screen.getByRole('alertdialog')).toBeInTheDocument()
      expect(screen.getByText('shell')).toBeInTheDocument()
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /approve this time/i })).toBeEnabled(),
      )
    })
  })
})
