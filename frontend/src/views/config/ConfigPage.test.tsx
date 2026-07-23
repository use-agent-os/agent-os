import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ComponentProps } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { ConfigPage } from './ConfigPage'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  },
}))

const navigateSpy = vi.fn()
vi.mock('react-router', () => ({
  useNavigate: () => navigateSpy,
}))

const mockRpc = {
  waitForConnection: vi.fn().mockResolvedValue(undefined),
  call: vi.fn(),
}
vi.mock('@/app/providers', () => ({
  useRpc: () => mockRpc,
}))

// A representative config.get payload exercising each field kind + tab routing.
function sampleConfig() {
  return {
    host: '127.0.0.1', // core, readonly (prefix 'host')
    port: 18791, // core, readonly (prefix 'port')
    version: '2026.7.19', // core, readonly distribution metadata
    debug: false, // core, boolean (prefix 'debug')
    diagnostics: { retention_days: 8000 }, // core, number (prefix 'diagnostics' → flattened)
    provider: 'openai', // ai, string
    memory: {
      inject_limit: 4000, // memory, number (flattened)
      embedding: { remote: { api_key: 'sk-secret' } }, // memory, sensitive string
    },
    control_ui: { allowed_origins: ['https://a.example.com'] }, // core, object/JSON
  }
}

function renderPage(props: ComponentProps<typeof ConfigPage> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const page = (nextProps: ComponentProps<typeof ConfigPage>) => (
    <QueryClientProvider client={client}>
      <ConfigPage {...nextProps} />
    </QueryClientProvider>
  )
  const result = render(page(props))
  return {
    ...result,
    rerenderPage: (nextProps: ComponentProps<typeof ConfigPage>) =>
      result.rerender(page(nextProps)),
  }
}

async function loadWith(config: Record<string, unknown>) {
  mockRpc.call.mockImplementation((method: string) => {
    if (method === 'config.snapshot') {
      return Promise.reject(Object.assign(new Error('missing'), { code: 'METHOD_NOT_FOUND' }))
    }
    if (method === 'config.get') return Promise.resolve(config)
    return Promise.resolve({})
  })
  renderPage()
  await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('config.get'))
}

describe('ConfigPage', () => {
  beforeEach(() => {
    mockRpc.call.mockReset()
    mockRpc.waitForConnection.mockReset().mockResolvedValue(undefined)
    navigateSpy.mockReset()
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.error).mockClear()
    vi.mocked(toast.info).mockClear()
    vi.mocked(toast.warning).mockClear()
  })

  it('calls config.get after waitForConnection and renders the Core tab fields', async () => {
    await loadWith(sampleConfig())
    expect(mockRpc.waitForConnection).toHaveBeenCalled()
    // Core tab is active by default: debug + diagnostics.retention_days visible
    // (the flattened leaf shows its prefix-stripped label under the group).
    await waitFor(() => expect(screen.getByLabelText('debug')).toBeInTheDocument())
    expect(screen.getByText('retention_days')).toBeInTheDocument()
  })

  it('uses config.get only when config.snapshot is explicitly unavailable', async () => {
    await loadWith(sampleConfig())
    expect(mockRpc.call).toHaveBeenCalledWith('config.snapshot')
    expect(mockRpc.call).toHaveBeenCalledWith('config.get')
  })

  it('surfaces snapshot authorization errors without issuing legacy reads', async () => {
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'config.snapshot') {
        return Promise.reject(Object.assign(new Error('forbidden'), { code: 'FORBIDDEN' }))
      }
      return Promise.resolve({})
    })
    renderPage()

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringContaining('forbidden'),
        expect.anything(),
      ),
    )
    expect(mockRpc.call).not.toHaveBeenCalledWith('config.get')
  })

  it('rejects a malformed successful snapshot instead of masking it with config.get', async () => {
    mockRpc.call.mockResolvedValue({})
    renderPage()

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringContaining('invalid response'),
        expect.anything(),
      ),
    )
    expect(mockRpc.call).not.toHaveBeenCalledWith('config.get')
  })

  it('uses the Settings-owned snapshot without issuing a duplicate config read', async () => {
    renderPage({
      embedded: true,
      externalSnapshot: { revision: 'revision-a', config: sampleConfig() },
    })

    await screen.findByLabelText('debug')
    expect(mockRpc.call).not.toHaveBeenCalled()
  })

  it('blocks a form draft when the owning snapshot advances until it is discarded', async () => {
    const reload = vi.fn().mockResolvedValue({
      revision: 'revision-b',
      config: { ...sampleConfig(), diagnostics: { retention_days: 8100 } },
    })
    const firstProps: ComponentProps<typeof ConfigPage> = {
      embedded: true,
      externalSnapshot: { revision: 'revision-a', config: sampleConfig() },
      onSnapshotReload: reload,
    }
    const view = renderPage(firstProps)
    fireEvent.click(await screen.findByLabelText('debug'))
    await screen.findByText(/change pending/i)

    view.rerenderPage({
      ...firstProps,
      externalSnapshot: {
        revision: 'revision-b',
        config: { ...sampleConfig(), diagnostics: { retention_days: 8100 } },
      },
    })

    expect(
      await screen.findByText(/configuration changed while this draft was open/i),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: /save config/i })).toBeDisabled()
    expect(
      within(screen.getByRole('region', { name: /pending changes/i })).getByRole('button', {
        name: /^save$/i,
      }),
    ).toBeDisabled()
    expect(mockRpc.call).not.toHaveBeenCalledWith('config.patch', expect.anything())

    fireEvent.click(screen.getByRole('button', { name: /discard draft/i }))
    await waitFor(() => expect(reload).toHaveBeenCalled())
    expect(await screen.findByDisplayValue('8100')).toBeInTheDocument()
    expect(screen.queryByText(/change pending/i)).not.toBeInTheDocument()
  })

  it('does not create a stale-draft conflict after YAML is reverted exactly to baseline', async () => {
    const firstProps: ComponentProps<typeof ConfigPage> = {
      embedded: true,
      externalSnapshot: { revision: 'revision-a', config: sampleConfig() },
    }
    const view = renderPage(firstProps)
    fireEvent.click(screen.getByRole('button', { name: /^yaml$/i }))
    const editor = (await screen.findByLabelText(/yaml editor/i)) as HTMLTextAreaElement
    const baseline = editor.value
    fireEvent.change(editor, { target: { value: `${baseline}\n# draft` } })
    await screen.findByText(/change pending/i)
    fireEvent.change(editor, { target: { value: baseline } })
    await waitFor(() => expect(screen.queryByText(/change pending/i)).not.toBeInTheDocument())

    view.rerenderPage({
      ...firstProps,
      externalSnapshot: {
        revision: 'revision-b',
        config: { ...sampleConfig(), diagnostics: { retention_days: 8100 } },
      },
    })

    expect(
      screen.queryByText(/configuration changed while this draft was open/i),
    ).not.toBeInTheDocument()
  })

  it('fails closed when the persisted config diverges from the running gateway', async () => {
    renderPage({
      embedded: true,
      externalSnapshot: {
        revision: undefined,
        config: sampleConfig(),
        diskDiverged: true,
        writeBlocked: true,
      },
    })

    expect(await screen.findByRole('button', { name: /save config/i })).toBeDisabled()
    expect(screen.queryByText(/config file changed outside AgentOS/i)).not.toBeInTheDocument()
  })

  it('renders host, port, and version as read-only metadata', async () => {
    await loadWith(sampleConfig())
    await waitFor(() => expect(screen.getByText('host')).toBeInTheDocument())
    // Read-only values render as text and cannot enter dirty/save tracking.
    expect(document.querySelector('[data-cfg-readonly="host"]')).not.toBeNull()
    expect(document.querySelector('[data-cfg-readonly="port"]')).not.toBeNull()
    expect(document.querySelector('[data-cfg-readonly="version"]')).toHaveTextContent('2026.7.19')
    expect(document.querySelector('input[data-cfg-key="host"]')).toBeNull()
    expect(screen.queryByDisplayValue('2026.7.19')).not.toBeInTheDocument()
  })

  it('reveals and re-masks write-only values with an accessible icon control', async () => {
    await loadWith(sampleConfig())
    fireEvent.click(screen.getByRole('tab', { name: 'Memory' }))
    const input = await screen.findByDisplayValue('sk-secret')
    expect(input).toHaveAttribute('type', 'password')

    fireEvent.click(screen.getByRole('button', { name: 'Show memory.embedding.remote.api_key' }))
    expect(input).toHaveAttribute('type', 'text')
    fireEvent.click(screen.getByRole('button', { name: 'Hide memory.embedding.remote.api_key' }))
    expect(input).toHaveAttribute('type', 'password')
  })

  it('supports arrow, Home, and End keyboard navigation across config sections', async () => {
    await loadWith(sampleConfig())
    const core = await screen.findByRole('tab', { name: 'Core' })
    core.focus()
    fireEvent.keyDown(core, { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: 'AI & Agents' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    fireEvent.keyDown(screen.getByRole('tab', { name: 'AI & Agents' }), { key: 'End' })
    expect(screen.getByRole('tab', { name: 'Other' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Other' }), { key: 'Home' })
    expect(core).toHaveAttribute('aria-selected', 'true')
  })

  it('sets a field dirty → the sticky save bar appears; no-op reset hides it again', async () => {
    await loadWith(sampleConfig())
    const toggle = await screen.findByLabelText('debug')
    const headerSave = screen.getByRole('button', { name: /save config/i })
    // Sticky bar hidden initially.
    expect(screen.queryByText(/changes? pending/i)).not.toBeInTheDocument()
    expect(headerSave).toBeDisabled()
    // Flip debug false→true → dirty.
    fireEvent.click(toggle)
    await waitFor(() => expect(screen.getByText(/changes? pending/i)).toBeInTheDocument())
    expect(headerSave).toBeEnabled()
    // Flip back true→false → no-op → sticky bar gone (computeDirty short-circuit).
    fireEvent.click(toggle)
    await waitFor(() => expect(screen.queryByText(/changes? pending/i)).not.toBeInTheDocument())
    expect(headerSave).toBeDisabled()
  })

  it('Save (form mode) calls config.patch with only the dirty dotted keys', async () => {
    await loadWith(sampleConfig())
    const numberInput = await screen.findByDisplayValue('8000')
    fireEvent.change(numberInput, { target: { value: '9000' } })
    await waitFor(() => expect(screen.getByText(/changes? pending/i)).toBeInTheDocument())
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'config.get') return Promise.resolve(sampleConfig())
      if (method === 'config.patch') return Promise.resolve({ restartRequired: false })
      return Promise.resolve({})
    })
    fireEvent.click(
      within(screen.getByRole('region', { name: /pending changes/i })).getByText('Save'),
    )
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('config.patch', {
        patches: { 'diagnostics.retention_days': 9000 },
      }),
    )
    expect(mockRpc.call).not.toHaveBeenCalledWith('config.apply', expect.anything())
  })

  it('a restartRequired patch response toasts the restart advisory', async () => {
    await loadWith(sampleConfig())
    const numberInput = await screen.findByDisplayValue('8000')
    fireEvent.change(numberInput, { target: { value: '9000' } })
    await waitFor(() => expect(screen.getByText(/changes? pending/i)).toBeInTheDocument())
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'config.get') return Promise.resolve(sampleConfig())
      if (method === 'config.patch') return Promise.resolve({ restartRequired: true })
      return Promise.resolve({})
    })
    fireEvent.click(
      within(screen.getByRole('region', { name: /pending changes/i })).getByText('Save'),
    )
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        expect.stringContaining('restart required'),
        expect.anything(),
      ),
    )
  })

  it('keeps an invalid-only JSON draft visible and protected until it is discarded', async () => {
    await loadWith(sampleConfig())
    const getCallsBefore = mockRpc.call.mock.calls.filter((call) => call[0] === 'config.get').length
    // Open the control_ui.allowed_origins object field editor.
    const jsonArea = await screen.findByDisplayValue(/https:\/\/a\.example\.com/)
    fireEvent.change(jsonArea, { target: { value: '{ broken' } })
    await waitFor(() => expect(screen.getByText('Invalid JSON')).toBeInTheDocument())
    const pending = screen.getByRole('region', { name: /pending changes/i })
    expect(within(pending).getByText('1')).toBeVisible()
    expect(screen.getByRole('button', { name: /save config/i })).toBeDisabled()
    expect(within(pending).getByRole('button', { name: /^save$/i })).toBeDisabled()
    expect(within(pending).getByRole('button', { name: /^discard$/i })).toBeEnabled()

    const beforeUnload = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(beforeUnload)
    expect(beforeUnload.defaultPrevented).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: /^reload$/i }))
    expect(toast.warning).toHaveBeenCalledWith(
      expect.stringMatching(/discard pending changes/i),
      expect.anything(),
    )
    expect(mockRpc.call.mock.calls.filter((call) => call[0] === 'config.get')).toHaveLength(
      getCallsBefore,
    )
    expect(screen.getByDisplayValue('{ broken')).toBeInTheDocument()
    expect(mockRpc.call).not.toHaveBeenCalledWith('config.patch', expect.anything())

    fireEvent.click(within(pending).getByRole('button', { name: /^discard$/i }))
    await waitFor(() =>
      expect(screen.queryByRole('region', { name: /pending changes/i })).toBeNull(),
    )
    expect(screen.getByDisplayValue(/https:\/\/a\.example\.com/)).toBeInTheDocument()
  })

  it('disables header Save when there are no changes', async () => {
    await loadWith(sampleConfig())
    await screen.findByLabelText('debug')
    expect(screen.getByRole('button', { name: /save config/i })).toBeDisabled()
    expect(toast.info).not.toHaveBeenCalled()
    expect(mockRpc.call).not.toHaveBeenCalledWith('config.patch', expect.anything())
  })

  it('Discard clears dirty state and reloads config.get', async () => {
    await loadWith(sampleConfig())
    const toggle = await screen.findByLabelText('debug')
    fireEvent.click(toggle)
    await waitFor(() => expect(screen.getByText(/changes? pending/i)).toBeInTheDocument())
    const callsBefore = mockRpc.call.mock.calls.filter((c) => c[0] === 'config.get').length
    fireEvent.click(screen.getByRole('button', { name: /^discard$/i }))
    await waitFor(() => expect(screen.queryByText(/changes? pending/i)).not.toBeInTheDocument())
    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter((c) => c[0] === 'config.get').length).toBe(
        callsBefore + 1,
      ),
    )
  })

  it('Discard remounts object fields so a stale JSON draft is dropped', async () => {
    await loadWith(sampleConfig())
    const jsonArea = (await screen.findByDisplayValue(
      /https:\/\/a\.example\.com/,
    )) as HTMLTextAreaElement
    // Edit the JSON draft (still valid) → dirty.
    fireEvent.change(jsonArea, { target: { value: '["https://b.example.com"]' } })
    await waitFor(() => expect(screen.getByText(/changes? pending/i)).toBeInTheDocument())
    // Discard → the fresh snapshot bumps resetKey, remounting the textarea back
    // to the loaded value (config.js:302 re-rendered the panel wholesale).
    fireEvent.click(screen.getByRole('button', { name: /^discard$/i }))
    await waitFor(() =>
      expect(screen.getByDisplayValue(/https:\/\/a\.example\.com/)).toBeInTheDocument(),
    )
    expect(screen.queryByDisplayValue(/https:\/\/b\.example\.com/)).not.toBeInTheDocument()
  })

  it('YAML mode Save calls config.apply with the edited YAML and the loaded baseline', async () => {
    await loadWith(sampleConfig())
    await screen.findByLabelText('debug')
    // Switch to YAML mode.
    fireEvent.click(screen.getByRole('button', { name: /^yaml$/i }))
    const area = (await screen.findByLabelText(/yaml editor/i)) as HTMLTextAreaElement
    const baseline = area.value
    expect(baseline).toContain('debug: false')
    fireEvent.change(area, { target: { value: baseline + '\n# edit\n' } })
    await waitFor(() => expect(screen.getByText(/changes? pending/i)).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /save config/i })).toBeEnabled()
    mockRpc.call.mockImplementation((method: string) => {
      if (method === 'config.get') return Promise.resolve(sampleConfig())
      if (method === 'config.apply') return Promise.resolve({ restartRequired: false })
      return Promise.resolve({})
    })
    fireEvent.click(
      within(screen.getByRole('region', { name: /pending changes/i })).getByText('Save'),
    )
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('config.apply', {
        config_yaml: baseline + '\n# edit\n',
        baseline_yaml: baseline,
      }),
    )
    expect(mockRpc.call).not.toHaveBeenCalledWith('config.patch', expect.anything())
  })

  it('keeps the sticky bar up (with the form dirty count) after switching to YAML mode with pending form edits', async () => {
    // config.js:667-679 — the bar is visible when there are pending form edits
    // OR the YAML draft is dirty; switching to YAML mode must not hide a form
    // edit that is still pending. The count follows the form keys until the YAML
    // draft itself diverges.
    await loadWith(sampleConfig())
    const numberInput = await screen.findByDisplayValue('8000')
    fireEvent.change(numberInput, { target: { value: '9000' } })
    await waitFor(() => expect(screen.getByText(/changes? pending/i)).toBeInTheDocument())
    // Switch to YAML mode — the (undirtied) YAML draft would give count 0, but
    // the pending form edit must keep the bar visible showing "1 change pending".
    fireEvent.click(screen.getByRole('button', { name: /^yaml$/i }))
    await screen.findByLabelText(/yaml editor/i)
    const bar = screen.getByRole('region', { name: /pending changes/i })
    expect(within(bar).getByText('1')).toBeInTheDocument()
    expect(within(bar).getByText(/change pending/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /save config/i })).toBeDisabled()
  })

  it('preserves an invalid object-field JSON draft across a tab switch (text and flag stay in sync)', async () => {
    // config.js:545,593-609 — the raw draft (including invalid text) is kept per
    // key so unmounting the field (switching tabs) and remounting it restores the
    // exact text the user typed, consistent with the still-set Invalid JSON flag.
    await loadWith(sampleConfig())
    const jsonArea = await screen.findByDisplayValue(/https:\/\/a\.example\.com/)
    // Type invalid JSON into the Core-tab object field.
    fireEvent.change(jsonArea, { target: { value: '{ broken' } })
    await waitFor(() => expect(screen.getByText('Invalid JSON')).toBeInTheDocument())
    // Switch away to another tab (unmounts the Core object field) then back.
    fireEvent.click(screen.getByRole('tab', { name: /ai & agents/i }))
    await waitFor(() => expect(screen.queryByDisplayValue('{ broken')).not.toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^core$/i }))
    // The invalid draft text is restored (not reverted to the canonical value),
    // and the Invalid JSON flag is still shown — the two agree.
    await waitFor(() => expect(screen.getByDisplayValue('{ broken')).toBeInTheDocument())
    expect(screen.getByText('Invalid JSON')).toBeInTheDocument()
    expect(screen.queryByDisplayValue(/https:\/\/a\.example\.com/)).not.toBeInTheDocument()
  })

  it('search filters the visible fields within the active tab', async () => {
    await loadWith(sampleConfig())
    await screen.findByLabelText('debug')
    const search = screen.getByPlaceholderText(/search keys/i)
    fireEvent.change(search, { target: { value: 'retention_days' } })
    await waitFor(() => expect(screen.queryByLabelText('debug')).not.toBeInTheDocument())
    expect(screen.getByText('retention_days')).toBeInTheDocument()
  })

  it('Guided setup navigates to /setup', async () => {
    await loadWith(sampleConfig())
    await screen.findByLabelText('debug')
    fireEvent.click(screen.getByRole('button', { name: /guided setup/i }))
    expect(navigateSpy).toHaveBeenCalledWith('/setup')
  })

  it('toasts when config.get fails', async () => {
    mockRpc.call.mockRejectedValue(new Error('boom'))
    renderPage()
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringContaining('Failed to load config'),
        expect.anything(),
      ),
    )
  })

  it('sets the document title', async () => {
    await loadWith(sampleConfig())
    await screen.findByLabelText('debug')
    expect(document.title).toBe('Config - AgentOS Control')
  })

  it('uses a level-two title when embedded below the Settings page heading', async () => {
    mockRpc.call.mockResolvedValue({ config: sampleConfig() })
    renderPage({ embedded: true })

    expect(
      await screen.findByRole('heading', { level: 2, name: 'Configuration editor' }),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { level: 1, name: 'Configuration editor' }),
    ).not.toBeInTheDocument()
  })
})
