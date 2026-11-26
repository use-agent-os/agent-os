import '@/test/web'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FakeRpc } from '@/test/fake-rpc'
import { KEY, assistantRow, chatRpc, openChat, resetChatState, userRow } from '@/test/web-chat'
import { PANEL_LAYOUT_PREFIX } from './panel-store'

beforeEach(resetChatState)
afterEach(() => {
  vi.restoreAllMocks()
})

const entry = (path: string, type: 'file' | 'directory', extra: Record<string, unknown> = {}) => ({
  name: path.split('/').at(-1),
  path,
  type,
  openable: true,
  ...extra,
})

const LISTINGS: Record<string, Record<string, unknown>> = {
  '': {
    entries: [
      entry('docs', 'directory'),
      entry('src', 'directory'),
      entry('README.md', 'file', { kind: 'markdown', size: 120 }),
      entry('blob.bin', 'file', { kind: 'other', size: 4096 }),
      { name: 'link', path: 'link', type: 'symlink', openable: false, reason: 'symlink' },
    ],
    exists: true,
    truncated: false,
  },
  docs: { entries: [entry('docs/guide.md', 'file', { kind: 'markdown', size: 40 })], exists: true },
  src: {
    entries: [entry('src/report.py', 'file', { kind: 'code', language: 'python', size: 60 })],
    exists: true,
  },
}

const READS: Record<string, Record<string, unknown>> = {
  'README.md': {
    kind: 'markdown',
    previewable: true,
    size: 120,
    content: '# Market notes\n\nSee the [guide](docs/guide.md).\n',
  },
  'docs/guide.md': { kind: 'markdown', previewable: true, size: 40, content: '# Guide\n' },
  'src/report.py': {
    kind: 'code',
    language: 'python',
    previewable: true,
    size: 60,
    content: 'def build(week):\n    return week\n',
  },
  'blob.bin': { kind: 'other', previewable: false, reason: 'binary', size: 4096, content: '' },
}

function filesRpc(rpc: FakeRpc = chatRpc()): FakeRpc {
  return rpc
    .handle('workspace.files.list', (p: { path?: string }) => {
      const listing = LISTINGS[p.path ?? '']
      if (!listing)
        throw Object.assign(new Error('gone'), { details: { code: 'workspace.not_found' } })
      return { path: p.path ?? '', ...listing }
    })
    .handle('workspace.files.read', (p: { path: string }) => ({ path: p.path, ...READS[p.path] }))
}

function panel() {
  return screen.getByRole('complementary', { name: 'Workspace panel' })
}

async function openFiles(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Show workspace panel' }))
  await user.click(within(panel()).getByRole('button', { name: /Files/ }))
  return within(panel()).findByRole('tree', { name: 'Workspace files' })
}

describe('workspace panel', () => {
  it('opens from the header toggle, hides on Escape and gives focus back', async () => {
    const user = userEvent.setup()
    await openChat(filesRpc())
    const toggle = screen.getByRole('button', { name: 'Show workspace panel' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await user.click(toggle)
    expect(within(panel()).getByText('Nothing open')).toBeInTheDocument()
    const hide = screen.getByRole('button', { name: 'Hide workspace panel' })
    expect(hide).toHaveAttribute('aria-expanded', 'true')
    expect(hide).toHaveAttribute('aria-controls', panel().id)
    await waitFor(() =>
      expect(within(panel()).getByRole('button', { name: /Files/ })).toHaveFocus(),
    )
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('complementary', { name: 'Workspace panel' })).toBeNull()
    await waitFor(() => expect(hide).toHaveFocus())
  })

  it('toggles with Mod+\\ from the composer', async () => {
    const { composer } = await openChat(filesRpc())
    composer.focus()
    fireEvent.keyDown(composer, { key: '\\', code: 'Backslash', ctrlKey: true })
    expect(panel()).toBeInTheDocument()
    fireEvent.keyDown(document.activeElement!, { key: '\\', code: 'Backslash', ctrlKey: true })
    expect(screen.queryByRole('complementary', { name: 'Workspace panel' })).toBeNull()
  })

  it('lists a tool the gateway does not offer as unavailable', async () => {
    const user = userEvent.setup()
    const rpc = filesRpc()
    rpc.methods = ['chat.history', 'workspace.files.list', 'workspace.files.read']
    await openChat(rpc)
    await user.click(screen.getByRole('button', { name: 'Show workspace panel' }))
    expect(within(panel()).getByRole('button', { name: /Files/ })).toBeEnabled()
    const browser = within(panel()).getByRole('button', { name: /Browser/ })
    expect(browser).toBeDisabled()
    expect(browser).toHaveTextContent('This gateway doesn’t offer it')
  })

  it('switches, moves and closes tabs from the keyboard', async () => {
    const user = userEvent.setup()
    await openChat(
      filesRpc().handle('browser.status', {
        enabled: false,
        installed: false,
        running: false,
        canStart: false,
      }),
    )
    await openFiles(user)
    await user.click(within(panel()).getByRole('button', { name: 'Open a tool' }))
    await user.click(await screen.findByRole('menuitem', { name: /Browser/ }))
    const tablist = within(panel()).getByRole('tablist', { name: 'Panel tabs' })
    const tabNames = () =>
      within(tablist)
        .getAllByRole('tab')
        .map((t) => t.textContent)
    await waitFor(() => expect(within(tablist).getByRole('tab', { name: 'Browser' })).toHaveFocus())
    await user.keyboard('{ArrowLeft}')
    expect(within(tablist).getByRole('tab', { name: 'Files' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await user.keyboard('{Control>}{Shift>}{ArrowRight}{/Shift}{/Control}')
    expect(tabNames()).toEqual(['Browser', 'Files'])
    expect(within(panel()).getByRole('status')).toHaveTextContent('Files moved to position 2 of 2')
    await user.keyboard('{Delete}')
    expect(tabNames()).toEqual(['Browser'])
  })

  it('remembers the session layout across a reload, and a new session starts closed', async () => {
    localStorage.setItem(
      PANEL_LAYOUT_PREFIX + KEY,
      JSON.stringify({ tabs: [{ kind: 'files' }], activeId: 'tool:files', open: true, width: 500 }),
    )
    await openChat(filesRpc())
    expect(await within(panel()).findByRole('tab', { name: 'Files' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(within(panel()).getByRole('separator', { name: 'Resize panel width' })).toHaveAttribute(
      'aria-valuenow',
      '500',
    )
  })

  it('resizes from the keyboard and saves the width', async () => {
    const user = userEvent.setup()
    await openChat(filesRpc())
    await openFiles(user)
    await waitFor(() => expect(within(panel()).getByRole('tab', { name: 'Files' })).toHaveFocus())
    const separator = within(panel()).getByRole('separator', { name: 'Resize panel width' })
    const before = Number(separator.getAttribute('aria-valuenow'))
    act(() => separator.focus())
    await user.keyboard('{ArrowLeft}')
    expect(Number(separator.getAttribute('aria-valuenow'))).toBe(before + 16)
    const saved = JSON.parse(localStorage.getItem(PANEL_LAYOUT_PREFIX + KEY)!)
    expect(saved.width).toBe(before + 16)
  })

  it('docks to the bottom and expands over the chat from the layout menu', async () => {
    const user = userEvent.setup()
    await openChat(filesRpc())
    await openFiles(user)
    await user.click(within(panel()).getByRole('button', { name: 'Panel layout' }))
    await user.click(await screen.findByRole('menuitemradio', { name: 'Dock to the bottom' }))
    expect(panel()).toHaveAttribute('data-presentation', 'bottom')
    expect(
      within(panel()).getByRole('separator', { name: 'Resize panel height' }),
    ).toBeInTheDocument()
    await user.click(within(panel()).getByRole('button', { name: 'Panel layout' }))
    await user.click(await screen.findByRole('menuitemcheckbox', { name: 'Expand over the chat' }))
    expect(panel()).toHaveAttribute('data-presentation', 'expanded')
    // The covered conversation is out of reach until the panel is restored.
    expect(screen.getByRole('textbox', { name: 'Message' }).closest('[inert]')).not.toBeNull()
    await user.click(within(panel()).getByRole('button', { name: 'Restore beside the chat' }))
    expect(panel()).toHaveAttribute('data-presentation', 'bottom')
  })
})

describe('Files tab', () => {
  it('browses a lazy tree and opens highlighted source', async () => {
    const user = userEvent.setup()
    const rpc = filesRpc()
    await openChat(rpc)
    const tree = await openFiles(user)
    expect(within(tree).getByRole('treeitem', { name: /link/ })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(rpc.callsTo('workspace.files.list').map((p) => p.path)).toEqual([''])
    await user.click(within(tree).getByRole('treeitem', { name: /^src/ }))
    expect(within(tree).getByRole('treeitem', { name: /^src/ })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    await user.click(await within(tree).findByRole('treeitem', { name: /report\.py/ }))
    const source = await within(panel()).findByRole('region', { name: 'report.py' })
    expect(source).toHaveTextContent('def build(week):')
    expect(source.querySelector('.hljs-keyword')).not.toBeNull()
    expect(within(panel()).getByText(/2 lines/)).toBeInTheDocument()
    // A narrow panel shows the file alone, with the way back.
    await user.click(within(panel()).getByRole('button', { name: 'All files' }))
    expect(within(panel()).getByRole('tree')).toBeInTheDocument()
  })

  it('previews Markdown, shows its source, and follows a relative link', async () => {
    const user = userEvent.setup()
    const rpc = filesRpc()
    await openChat(rpc)
    const tree = await openFiles(user)
    await user.click(within(tree).getByRole('treeitem', { name: /README\.md/ }))
    const preview = await within(panel()).findByRole('region', { name: 'README.md' })
    expect(within(preview).getByRole('heading', { name: 'Market notes' })).toBeInTheDocument()
    await user.click(within(panel()).getByRole('button', { name: 'Source' }))
    expect(within(panel()).getByRole('region', { name: 'README.md' })).toHaveTextContent(
      '# Market notes',
    )
    await user.click(within(panel()).getByRole('button', { name: 'Preview' }))
    await user.click(within(panel()).getByRole('link', { name: 'guide' }))
    await waitFor(() =>
      expect(rpc.callsTo('workspace.files.read').map((p) => p.path)).toContain('docs/guide.md'),
    )
    expect(await within(panel()).findByRole('heading', { name: 'Guide' })).toBeInTheDocument()
  })

  it('filters by name and hides an open folder with nothing matching', async () => {
    const user = userEvent.setup()
    await openChat(filesRpc())
    const tree = await openFiles(user)
    await user.click(within(tree).getByRole('treeitem', { name: 'docs' }))
    await within(tree).findByRole('treeitem', { name: /guide\.md/ })
    await user.click(within(tree).getByRole('treeitem', { name: 'src' }))
    await within(tree).findByRole('treeitem', { name: /report\.py/ })
    await user.type(within(panel()).getByRole('searchbox', { name: 'Filter files' }), 'guide')
    expect(within(tree).getByRole('treeitem', { name: 'docs' })).toBeInTheDocument()
    expect(within(tree).getByRole('treeitem', { name: /guide\.md/ })).toBeInTheDocument()
    expect(within(tree).queryByRole('treeitem', { name: 'src' })).toBeNull()
    expect(within(tree).queryByRole('treeitem', { name: /README/ })).toBeNull()
    // A row is named by itself: an open folder's name is not its contents.
    expect(within(tree).getByRole('treeitem', { name: /^guide\.md\s?40 B$/ })).toBeInTheDocument()
    expect(within(tree).getByRole('treeitem', { name: 'docs' })).toHaveAttribute('aria-level', '1')
  })

  it('says why a binary file is not shown', async () => {
    const user = userEvent.setup()
    await openChat(filesRpc())
    const tree = await openFiles(user)
    await user.click(within(tree).getByRole('treeitem', { name: /blob\.bin/ }))
    expect(await within(panel()).findByText('This file can’t be previewed')).toBeInTheDocument()
    expect(within(panel()).getByText('It looks like binary data (4.0 KB).')).toBeInTheDocument()
  })

  it('shows an empty workspace, a missing one, an old gateway and a failure', async () => {
    const user = userEvent.setup()
    const cases: [unknown, string][] = [
      [{ path: '', entries: [], exists: true }, 'This workspace is empty'],
      [{ path: '', entries: [], exists: false }, 'No workspace yet'],
    ]
    for (const [answer, text] of cases) {
      resetChatState()
      const { unmount } = await openChat(chatRpc().handle('workspace.files.list', answer))
      await user.click(screen.getByRole('button', { name: 'Show workspace panel' }))
      await user.click(within(panel()).getByRole('button', { name: /Files/ }))
      expect(await within(panel()).findByText(text)).toBeInTheDocument()
      unmount()
    }
    resetChatState()
    const old = chatRpc().handle('workspace.files.list', () => {
      throw Object.assign(new Error('no such method'), { code: 'METHOD_NOT_FOUND' })
    })
    const first = await openChat(old)
    await user.click(screen.getByRole('button', { name: 'Show workspace panel' }))
    await user.click(within(panel()).getByRole('button', { name: /Files/ }))
    expect(await within(panel()).findByText('This gateway can’t browse files')).toBeInTheDocument()
    first.unmount()
    resetChatState()
    let fail = true
    const flaky = chatRpc().handle('workspace.files.list', () => {
      if (fail) throw new Error('disk on fire')
      return LISTINGS['']
    })
    await openChat(flaky)
    await user.click(screen.getByRole('button', { name: 'Show workspace panel' }))
    await user.click(within(panel()).getByRole('button', { name: /Files/ }))
    expect(await within(panel()).findByRole('alert')).toHaveTextContent('disk on fire')
    fail = false
    await user.click(within(panel()).getByRole('button', { name: 'Retry' }))
    expect(await within(panel()).findByRole('tree')).toBeInTheDocument()
  })

  it('opens a file a tool call wrote, from the transcript', async () => {
    const user = userEvent.setup()
    const rpc = filesRpc(
      chatRpc([
        userRow('u1', 'write it'),
        assistantRow('a1', '', {
          tool_calls: [
            {
              type: 'tool_use',
              tool_use_id: 'w',
              name: 'write_file',
              input: { path: '/var/lib/agentos/workspace/src/report.py', content: 'x' },
            },
            {
              type: 'tool_result',
              tool_use_id: 'w',
              name: 'write_file',
              result: 'ok',
              is_error: false,
            },
          ],
        }),
      ]),
    ).handle('workspace.files.resolve', { agentId: 'main', path: 'src/report.py' })
    await openChat(rpc)
    const log = screen.getByRole('log', { name: 'Conversation' })
    await user.click(await within(log).findByRole('button', { name: /write_file/ }))
    await user.click(within(log).getByRole('button', { name: 'Open in Files' }))
    expect(rpc.callsTo('workspace.files.resolve')).toEqual([
      { path: '/var/lib/agentos/workspace/src/report.py', agentId: 'main' },
    ])
    expect(await within(panel()).findByRole('region', { name: 'report.py' })).toHaveTextContent(
      'def build',
    )
  })
})

describe('Browser tab', () => {
  const status = (extra: Record<string, unknown> = {}) => ({
    sessionKey: KEY,
    enabled: true,
    installed: true,
    mode: 'managed',
    running: false,
    attachConfirmed: true,
    canStart: true,
    ...extra,
  })
  const snapshot = (extra: Record<string, unknown> = {}) => ({
    running: true,
    tabs: [
      { tabId: 't1', title: 'Example Domain', url: 'https://example.com/', active: true },
      { tabId: 't2', title: 'Docs', url: 'https://docs.example.com/', active: false },
    ],
    activeTabId: 't1',
    url: 'https://example.com/',
    title: 'Example Domain',
    image: { mime: 'image/jpeg', data: 'AAAA' },
    ...extra,
  })

  async function openBrowser(rpc: FakeRpc) {
    const user = userEvent.setup()
    await openChat(rpc)
    await user.click(screen.getByRole('button', { name: 'Show workspace panel' }))
    await user.click(within(panel()).getByRole('button', { name: /Browser/ }))
    return user
  }

  it('explains a browser that is turned off, missing or unconsented', async () => {
    const cases: [Record<string, unknown>, string][] = [
      [{ enabled: false }, 'The browser is turned off'],
      [
        {
          installed: false,
          installHint: 'agent-browser is not installed. Install it with: npm i -g agent-browser',
        },
        'The browser engine isn’t installed',
      ],
      [
        { attachConfirmed: false, canStart: false, mode: 'attach' },
        'Attach mode needs your consent',
      ],
    ]
    for (const [extra, text] of cases) {
      resetChatState()
      const view = await openChat(chatRpc().handle('browser.status', status(extra)))
      await userEvent.setup().click(screen.getByRole('button', { name: 'Show workspace panel' }))
      await userEvent.setup().click(within(panel()).getByRole('button', { name: /Browser/ }))
      expect(await within(panel()).findByText(text)).toBeInTheDocument()
      if (extra.installHint)
        expect(within(panel()).getByText('npm i -g agent-browser')).toBeInTheDocument()
      view.unmount()
    }
  })

  it('degrades on a gateway without the browser view', async () => {
    const rpc = chatRpc()
    rpc.methods = ['chat.history']
    resetChatState()
    const user = userEvent.setup()
    await openChat(rpc)
    await user.click(screen.getByRole('button', { name: 'Show workspace panel' }))
    expect(within(panel()).getByRole('button', { name: /Browser/ })).toBeDisabled()
  })

  it('starts a stopped browser and shows the live page with its tabs', async () => {
    let running = false
    const rpc = chatRpc()
      .handle('browser.status', () => status({ running, canStart: !running }))
      .handle('browser.start', () => {
        running = true
        return status({ running: true, canStart: false })
      })
      .handle('browser.snapshot', () => snapshot())
    const user = await openBrowser(rpc)
    await user.click(await within(panel()).findByRole('button', { name: 'Start browser' }))
    expect(rpc.callsTo('browser.start')).toEqual([{ sessionKey: KEY }])
    const page = await within(panel()).findByRole('img', {
      name: 'The agent’s browser showing Example Domain',
    })
    expect(page).toHaveAttribute('src', 'data:image/jpeg;base64,AAAA')
    const tabs = within(panel()).getByRole('tablist', { name: 'Browser tabs' })
    expect(within(tabs).getByRole('tab', { name: 'Example Domain' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(within(panel()).getByRole('textbox', { name: 'Address' })).toHaveValue(
      'https://example.com/',
    )
    expect(within(panel()).getByRole('link', { name: 'Open in your browser' })).toHaveAttribute(
      'href',
      'https://example.com/',
    )
  })

  it('navigates, goes back and switches tabs through the gateway', async () => {
    const rpc = chatRpc()
      .handle('browser.status', status({ running: true, canStart: false }))
      .handle('browser.snapshot', () => snapshot())
      .handle('browser.navigate', { url: 'https://docs.example.com/' })
      .handle('browser.history', { action: 'back' })
      .handle('browser.tabs.select', { tabId: 't2' })
    const user = await openBrowser(rpc)
    const address = await within(panel()).findByRole('textbox', { name: 'Address' })
    await user.clear(address)
    await user.type(address, 'hello world{Enter}')
    expect(within(panel()).getByRole('alert')).toHaveTextContent('Enter a web address')
    expect(rpc.callsTo('browser.navigate')).toEqual([])
    await user.clear(address)
    await user.type(address, 'docs.example.com{Enter}')
    await waitFor(() =>
      expect(rpc.callsTo('browser.navigate')).toEqual([
        { sessionKey: KEY, url: 'https://docs.example.com/' },
      ]),
    )
    await user.click(within(panel()).getByRole('button', { name: 'Back' }))
    await waitFor(() =>
      expect(rpc.callsTo('browser.history')).toEqual([{ sessionKey: KEY, action: 'back' }]),
    )
    await user.click(within(panel()).getByRole('tab', { name: 'Docs' }))
    await waitFor(() =>
      expect(rpc.callsTo('browser.tabs.select')).toEqual([{ sessionKey: KEY, tabId: 't2' }]),
    )
  })

  it('names a blank tab instead of showing its white frame', async () => {
    const rpc = chatRpc()
      .handle('browser.status', status({ running: true, canStart: false }))
      .handle('browser.snapshot', () =>
        snapshot({
          tabs: [{ tabId: 't1', title: '', url: 'about:blank', active: true }],
          url: 'about:blank',
          title: '',
        }),
      )
    await openBrowser(rpc)
    expect(await within(panel()).findByText('Blank page')).toBeInTheDocument()
    expect(within(panel()).queryByRole('img')).toBeNull()
    expect(within(panel()).queryByRole('button', { name: '100%' })).toBeNull()
  })

  it('reports a refused navigation and a page it does not capture', async () => {
    const rpc = chatRpc()
      .handle('browser.status', status({ running: true, canStart: false }))
      .handle('browser.snapshot', () => snapshot({ image: null, withheld: 'private' }))
      .handle('browser.navigate', () => {
        throw Object.assign(new Error('Blocked: 10.0.0.5 is a private address'), {
          details: { code: 'browser.blocked' },
        })
      })
    const user = await openBrowser(rpc)
    expect(await within(panel()).findByText('Page not shown')).toBeInTheDocument()
    const address = within(panel()).getByRole('textbox', { name: 'Address' })
    await user.clear(address)
    await user.type(address, 'intranet.example.com{Enter}')
    expect(await within(panel()).findByRole('alert')).toHaveTextContent('private address')
  })
})
