import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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

function renderPage() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ConfigPage />
    </QueryClientProvider>,
  )
}

async function loadWith(config: Record<string, unknown>) {
  mockRpc.call.mockImplementation((method: string) => {
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

  it('renders host/port as read-only (no editable input, no save tracking)', async () => {
    await loadWith(sampleConfig())
    await waitFor(() => expect(screen.getByText('host')).toBeInTheDocument())
    // The readonly value renders as text; there is no input carrying data-cfg-key host.
    expect(document.querySelector('[data-cfg-readonly="host"]')).not.toBeNull()
    expect(document.querySelector('input[data-cfg-key="host"]')).toBeNull()
  })

  it('sets a field dirty → the sticky save bar appears; no-op reset hides it again', async () => {
    await loadWith(sampleConfig())
    const toggle = await screen.findByLabelText('debug')
    // Sticky bar hidden initially.
    expect(screen.queryByText(/changes? pending/i)).not.toBeInTheDocument()
    // Flip debug false→true → dirty.
    fireEvent.click(toggle)
    await waitFor(() => expect(screen.getByText(/changes? pending/i)).toBeInTheDocument())
    // Flip back true→false → no-op → sticky bar gone (computeDirty short-circuit).
    fireEvent.click(toggle)
    await waitFor(() => expect(screen.queryByText(/changes? pending/i)).not.toBeInTheDocument())
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

  it('invalid JSON in an object field blocks Save (config.patch never fires)', async () => {
    await loadWith(sampleConfig())
    // Open the control_ui.allowed_origins object field editor.
    const jsonArea = await screen.findByDisplayValue(/https:\/\/a\.example\.com/)
    fireEvent.change(jsonArea, { target: { value: '{ broken' } })
    await waitFor(() => expect(screen.getByText('Invalid JSON')).toBeInTheDocument())
    // Sticky bar not shown for an invalid-only edit; use the header Save.
    fireEvent.click(screen.getByRole('button', { name: /save config/i }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringMatching(/invalid json/i),
        expect.anything(),
      ),
    )
    expect(mockRpc.call).not.toHaveBeenCalledWith('config.patch', expect.anything())
  })

  it('Save with no dirty fields short-circuits with a "No changes to save" toast', async () => {
    await loadWith(sampleConfig())
    await screen.findByLabelText('debug')
    fireEvent.click(screen.getByRole('button', { name: /save config/i }))
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        expect.stringMatching(/no changes to save/i),
        expect.anything(),
      ),
    )
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
})
