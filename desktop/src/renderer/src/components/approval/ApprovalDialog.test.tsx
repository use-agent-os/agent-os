import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { approvalMonitor, useApprovals, type Approval } from '@/services/approval-monitor'
import { ApprovalDialog } from './ApprovalDialog'

vi.mock('@/services/approval-monitor', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/approval-monitor')>()
  return { ...actual, approvalMonitor: { resolve: vi.fn().mockResolvedValue(undefined) } }
})

const resolveSpy = vi.mocked(approvalMonitor.resolve)

function setPending(pending: Approval[]) {
  useApprovals.setState({ pending, count: pending.length, mode: 'prompt' })
}

const PATCH: Approval = {
  id: 'p1',
  namespace: 'exec',
  toolName: 'apply_patch',
  sessionKey: 'agent:main:s1',
  mode: 'patch',
  command: 'apply_patch 34536ae06e0cedb2ca2e716fd5747b69bcebbd1ab424a24159efb6e563b6c05b',
  warning: 'apply_patch writes outside active workspace (/tmp/ws): /srv/notes/notes.md',
  args: { outside_paths: ['/srv/notes/notes.md'], fingerprint: '34536ae0' },
}

describe('ApprovalDialog', () => {
  beforeEach(() => {
    resolveSpy.mockReset().mockResolvedValue(undefined)
    setPending([])
  })
  afterEach(() => setPending([]))

  it('renders nothing while the queue is empty', () => {
    const { container } = render(<ApprovalDialog />)
    expect(container).toBeEmptyDOMElement()
  })

  it('names the tool and the files, hides the patch fingerprint, defaults to Deny', () => {
    setPending([PATCH])
    render(<ApprovalDialog />)
    const dialog = screen.getByRole('alertdialog')
    expect(dialog).toHaveTextContent('Approval needed for apply_patch')
    expect(screen.getByText('/srv/notes/notes.md')).toBeInTheDocument()
    expect(dialog).not.toHaveTextContent('34536ae06e0cedb2')
    expect(dialog).toHaveTextContent('Session agent:main:s1')
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Deny' }))
    // Escape must not dismiss an approval: silence is not a decision.
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })

  it('shows the command for a shell approval and offers Always Allow only for exec', async () => {
    setPending([{ id: 's1', namespace: 'exec', toolName: 'shell', command: 'rm -rf build' }])
    render(<ApprovalDialog />)
    expect(screen.getByText('rm -rf build')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Always Allow' })).toBeInTheDocument()
    setPending([])
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    setPending([{ id: 'w1', namespace: 'web', toolName: 'web_fetch', command: 'GET x' }])
    expect(await screen.findByText('GET x')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Always Allow' })).toBeNull()
  })

  it('maps every button to its action and closes once the item drains', async () => {
    setPending([PATCH])
    render(<ApprovalDialog />)
    fireEvent.click(screen.getByRole('button', { name: 'Allow Once' }))
    await waitFor(() => expect(resolveSpy).toHaveBeenCalledWith(PATCH, 'once'))
    setPending([])
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())

    for (const [label, action] of [
      ['Deny', 'deny'],
      ['Always Allow', 'always'],
      ['Stop Asking', 'bypass'],
    ] as const) {
      resolveSpy.mockClear()
      const item = { ...PATCH, id: `p-${action}` }
      setPending([item])
      fireEvent.click(await screen.findByRole('button', { name: label }))
      await waitFor(() => expect(resolveSpy).toHaveBeenCalledWith(item, action))
      setPending([])
    }
  })

  it('keeps the shown item pinned when a new approval jumps the queue', () => {
    setPending([PATCH])
    render(<ApprovalDialog />)
    setPending([{ id: 'p0', namespace: 'exec', toolName: 'shell', command: 'ls' }, PATCH])
    expect(screen.getByRole('alertdialog')).toHaveTextContent('apply_patch')
  })
})
