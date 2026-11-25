import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { STOPPED_GATEWAY } from '@shared/gateway'
import { useGateway } from '~/stores/gateway'
import { useUi } from '~/stores/ui'
import { FilesPanel } from './FilesPanel'

type Params = { path?: string }
type Reply = (params: Params) => unknown

const replies = new Map<string, Reply>()
const rpc = {
  call: vi.fn(async (method: string, params: Params) => {
    const reply = replies.get(method)
    if (!reply) throw Object.assign(new Error('unknown method'), { code: 'METHOD_NOT_FOUND' })
    return reply(params)
  }),
  waitForConnection: vi.fn(async () => {}),
  on: () => () => {},
}
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))

const entry = (path: string, extra: Record<string, unknown>) => ({
  name: path.split('/').pop(),
  path,
  openable: true,
  ...extra,
})

const TREE: Record<string, unknown[]> = {
  '': [
    entry('documents', { type: 'directory' }),
    entry('MEMORY.md', { type: 'file', kind: 'markdown', size: 18 }),
    entry('data.json', { type: 'file', kind: 'code', language: 'json', size: 8 }),
    entry('shortcut', { type: 'symlink', openable: false, reason: 'symlink' }),
    entry('blob.bin', { type: 'file', kind: 'other', size: 300 }),
  ],
  documents: [entry('documents/Plan.md', { type: 'file', kind: 'markdown', size: 4 })],
}

const TEXT: Record<string, Record<string, unknown>> = {
  'MEMORY.md': { kind: 'markdown', content: '# Memory\n\nhello **there**', previewable: true },
  'data.json': { kind: 'code', language: 'json', content: '{"a": 1}', previewable: true },
  'documents/Plan.md': { kind: 'markdown', content: 'the plan', previewable: true },
  'blob.bin': { kind: 'other', content: '', previewable: false, reason: 'binary' },
}

function denied(code: string) {
  return Object.assign(new Error(code), { code: 'INVALID_REQUEST', details: { code } })
}

function mount() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <FilesPanel />
    </QueryClientProvider>,
  )
}

const row = (name: string) => screen.findByRole('treeitem', { name })
const calls = (method: string) => rpc.call.mock.calls.filter(([m]) => m === method)

beforeEach(() => {
  rpc.call.mockClear()
  replies.clear()
  replies.set('workspace.files.list', ({ path = '' }) => {
    if (!(path in TREE)) throw denied('workspace.not_found')
    return { path, entries: TREE[path], truncated: false, total: TREE[path]!.length }
  })
  replies.set('workspace.files.read', ({ path = '' }) => {
    if (!(path in TEXT)) throw denied('workspace.denied')
    return { path, size: 10, mtimeMs: 1_700_000_000_000, truncated: false, ...TEXT[path] }
  })
  useUi.setState({ filesOpen: true, filesSelected: null, filesExpanded: new Set() })
  useGateway.setState({ status: { ...STOPPED_GATEWAY, state: 'running' } })
})
afterEach(() => useUi.setState({ filesOpen: false }))

describe('FilesPanel', () => {
  it('renders nothing while closed', () => {
    useUi.setState({ filesOpen: false })
    mount()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('lists the workspace root and asks for nothing else', async () => {
    mount()
    expect(await row('MEMORY.md')).toBeInTheDocument()
    expect(await row('documents')).toHaveAttribute('aria-expanded', 'false')
    expect(calls('workspace.files.list')).toEqual([['workspace.files.list', { path: '' }]])
    expect(calls('workspace.files.read')).toHaveLength(0)
    expect(screen.getByText('Pick a file to preview')).toBeInTheDocument()
  })

  it('reads a folder only when it is opened', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(await row('documents'))
    expect(await row('Plan.md')).toHaveAttribute('aria-level', '2')
    expect(calls('workspace.files.list')).toHaveLength(2)
    await user.click(await row('documents'))
    expect(screen.queryByRole('treeitem', { name: 'Plan.md' })).toBeNull()
  })

  it('previews markdown rendered, and as source on request', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(await row('MEMORY.md'))
    expect(await screen.findByRole('heading', { name: 'Memory' })).toBeInTheDocument()
    expect(await row('MEMORY.md')).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('radio', { name: 'Source' }))
    expect(screen.queryByRole('heading', { name: 'Memory' })).toBeNull()
    expect(document.querySelector('.files-code')?.textContent).toContain('# Memory')
  })

  it('previews code highlighted, with no rendered/source switch', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(await row('data.json'))
    await waitFor(() => expect(document.querySelector('.files-code .hljs-attr')).not.toBeNull())
    expect(screen.queryByRole('radiogroup')).toBeNull()
  })

  it('does not open a link, and says why', async () => {
    const user = userEvent.setup()
    mount()
    const link = await row('shortcut')
    expect(link).toHaveAttribute('aria-disabled', 'true')
    expect(link.title).toMatch(/not followed/)
    await user.click(link)
    expect(calls('workspace.files.read')).toHaveLength(0)
    expect(calls('workspace.files.download')).toHaveLength(0)
  })

  it('says so when a file has no preview', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(await row('blob.bin'))
    expect(await screen.findByText('No preview for this file')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy contents' })).toBeNull()
  })

  it('shows a refusal from the gateway instead of content', async () => {
    replies.set('workspace.files.read', () => {
      throw denied('workspace.denied')
    })
    const user = userEvent.setup()
    mount()
    await user.click(await row('MEMORY.md'))
    expect(await screen.findByText('This file is not shown here')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('asks for an update on a gateway without the file browser', async () => {
    replies.clear()
    mount()
    expect(await screen.findByText('Update the gateway to browse files')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('shows an empty workspace', async () => {
    replies.set('workspace.files.list', () => ({ path: '', entries: [], exists: false }))
    mount()
    expect(await screen.findByText('Nothing here yet')).toBeInTheDocument()
  })

  it('says how much of a long folder is shown', async () => {
    replies.set('workspace.files.list', () => ({
      path: '',
      entries: TREE['']!.slice(0, 2),
      truncated: true,
      total: 900,
    }))
    mount()
    expect(await screen.findByText('Showing the first 2 of 900 items')).toBeInTheDocument()
  })

  it('filters by name', async () => {
    const user = userEvent.setup()
    mount()
    await row('MEMORY.md')
    await user.type(screen.getByRole('searchbox', { name: 'Filter by name' }), 'json')
    expect(screen.queryByRole('treeitem', { name: 'MEMORY.md' })).toBeNull()
    expect(screen.getByRole('treeitem', { name: 'data.json' })).toBeInTheDocument()
  })

  it('walks the tree with the arrow keys', async () => {
    const user = userEvent.setup()
    mount()
    const first = await row('documents')
    first.focus()
    await user.keyboard('{ArrowRight}')
    expect(await row('Plan.md')).toBeInTheDocument()
    await user.keyboard('{ArrowDown}')
    expect(await row('Plan.md')).toHaveFocus()
    await user.keyboard('{ArrowLeft}')
    expect(await row('documents')).toHaveFocus()
    await user.keyboard('{ArrowLeft}')
    expect(screen.queryByRole('treeitem', { name: 'Plan.md' })).toBeNull()
    await user.keyboard('{End}')
    expect(await row('blob.bin')).toHaveFocus()
  })

  it('is one tab stop', async () => {
    mount()
    await row('MEMORY.md')
    const tree = screen.getByRole('tree')
    await waitFor(() =>
      expect(
        within(tree)
          .getAllByRole('treeitem')
          .filter((r) => r.tabIndex === 0),
      ).toHaveLength(1),
    )
  })

  it('reopens on the file it was showing, with its folders disclosed', async () => {
    useUi.setState({ filesSelected: 'documents/Plan.md' })
    mount()
    expect(await screen.findByText('the plan')).toBeInTheDocument()
    expect(await row('Plan.md')).toHaveAttribute('aria-selected', 'true')
  })

  it('waits for the gateway instead of calling it', () => {
    useGateway.setState({ status: { ...STOPPED_GATEWAY, state: 'stopped' } })
    mount()
    expect(screen.getByText('Gateway off')).toBeInTheDocument()
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('closes on Escape', async () => {
    const user = userEvent.setup()
    mount()
    await row('MEMORY.md')
    await user.keyboard('{Escape}')
    expect(useUi.getState().filesOpen).toBe(false)
  })
})

describe('one sheet at a time', () => {
  it('opening Files closes the others, and the others close Files', () => {
    useUi.setState({ filesOpen: false, jobsOpen: true })
    useUi.getState().openFiles()
    expect(useUi.getState()).toMatchObject({ filesOpen: true, jobsOpen: false })
    useUi.getState().openSkills()
    expect(useUi.getState()).toMatchObject({ filesOpen: false, skillsOpen: true })
    useUi.getState().toggleFiles()
    expect(useUi.getState()).toMatchObject({ filesOpen: true, skillsOpen: false })
    useUi.getState().openSettings()
    expect(useUi.getState()).toMatchObject({ filesOpen: false, settingsOpen: true })
    useUi.getState().closeSettings()
  })
})
