import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { INITIAL_BOOTSTRAP, type BootstrapState, type StageProgress } from '@shared/bootstrap'
import { useBootstrap } from '~/stores/bootstrap'
import { formatElapsed, SetupOverlay } from './SetupOverlay'

vi.mock('@/app/providers', () => ({ useRpc: () => ({ call: vi.fn(async () => ({})) }) }))
vi.mock('./ProviderStep', () => ({
  ProviderStep: ({ onDone }: { onDone: () => void }) => (
    <button type="button" data-testid="provider-step" onClick={onDone}>
      provider step
    </button>
  ),
}))

const install = vi.fn(async () => {})
const cancel = vi.fn(async () => {})
const connectExisting = vi.fn(async () => {})
const openLog = vi.fn(async () => {})

function stage(
  name: string,
  state: StageProgress['state'],
  extra: Partial<StageProgress> = {},
): StageProgress {
  return {
    name,
    title: `Stage ${name}`,
    category: 'runtime',
    needsUserInput: false,
    state,
    startedAt: null,
    durationMs: null,
    reason: null,
    ...extra,
  }
}

function setState(patch: Partial<BootstrapState>) {
  useBootstrap.setState({
    state: { ...INITIAL_BOOTSTRAP, ...patch },
    loaded: true,
    dismissed: false,
    install,
    cancel,
    connectExisting,
    openLog,
  })
}

beforeEach(() => {
  install.mockClear()
  cancel.mockClear()
  connectExisting.mockClear()
  openLog.mockClear()
  useConnection.getState().setState('disconnected')
})

describe('SetupOverlay', () => {
  it('stays out of the way when the engine is ready', () => {
    setState({ phase: 'ready' })
    const { container } = render(<SetupOverlay />)
    expect(container).toBeEmptyDOMElement()
  })

  it('welcomes a missing engine with step 1 current, and updates an older one', () => {
    setState({
      phase: 'choice',
      mode: 'install',
      discovery: {
        source: 'missing',
        cliPath: null,
        version: null,
        appVersion: '2026.9.12',
        relation: null,
        needsInstall: true,
        reason: '',
      },
    })
    const view = render(<SetupOverlay />)
    expect(screen.getByRole('heading', { name: 'Welcome to AgentOS' })).toBeInTheDocument()
    expect(screen.getByText('Install').closest('li')).toHaveAttribute('aria-current', 'step')
    expect(screen.getByText('Provider').closest('li')).toHaveAttribute('data-state', 'todo')
    fireEvent.click(screen.getByRole('button', { name: 'Install the engine' }))
    expect(install).toHaveBeenCalledTimes(1)

    setState({
      phase: 'choice',
      mode: 'update',
      discovery: {
        source: 'found',
        cliPath: '/u/.local/bin/agentos',
        version: '2026.8.23',
        appVersion: '2026.9.12',
        relation: 'older',
        needsInstall: true,
        reason: '',
      },
    })
    view.rerender(<SetupOverlay />)
    expect(screen.getByRole('heading', { name: 'A newer engine is ready' })).toBeInTheDocument()
    expect(screen.getByText('2026.8.23')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /I already run a gateway/ }))
    expect(connectExisting).toHaveBeenCalledTimes(1)
  })

  it('shows stage progress with the running stage half-counted, and can cancel', () => {
    setState({
      phase: 'running',
      mode: 'install',
      stages: [
        stage('prerequisites', 'succeeded', { durationMs: 1200 }),
        stage('uv', 'running', { startedAt: Date.now() - 65_000 }),
        stage('package', 'pending'),
        stage('complete', 'pending'),
      ],
      log: [{ stage: 'uv', stream: 'stderr', line: 'downloading uv…' }],
    })
    render(<SetupOverlay />)
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '38')
    expect(screen.getByText(/1 \/ 4 steps complete/)).toBeInTheDocument()
    expect(screen.getByText(/1m 0[45]s/)).toBeInTheDocument()
    expect(screen.queryByTestId('setup-log')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show details' }))
    expect(screen.getByTestId('setup-log')).toHaveTextContent('downloading uv…')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(cancel).toHaveBeenCalledTimes(1)
  })

  it('opens the output on failure and offers retry, copy, log and the manual command', async () => {
    const writeText = vi.fn(async () => {})
    Object.assign(navigator, { clipboard: { writeText } })
    setState({
      phase: 'failed',
      error: '"Install the AgentOS engine" failed: stage failed (exit 1)',
      logPath: '/u/.agentos/logs/bootstrap-1.log',
      stages: [stage('package', 'failed', { reason: 'exit 1' })],
      log: [{ stage: 'package', stream: 'stderr', line: 'error: 404 for wheel' }],
    })
    render(<SetupOverlay />)
    expect(screen.getByTestId('setup-error')).toHaveTextContent('stage failed (exit 1)')
    expect(screen.getByTestId('setup-log')).toHaveTextContent('404 for wheel')
    expect(screen.getByText(/curl -fsSL .*install\.sh \| bash/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Copy output' }))
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining('[stderr] error: 404 for wheel'))
    fireEvent.click(screen.getByRole('button', { name: 'Show log in Finder' }))
    expect(openLog).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(install).toHaveBeenCalledTimes(1)
  })

  it('waits for the gateway after success, then moves to step 2 and hands over', () => {
    setState({ phase: 'succeeded' })
    const view = render(<SetupOverlay />)
    expect(screen.getByText('Starting the gateway…')).toBeInTheDocument()
    expect(screen.getByText('Provider').closest('li')).toHaveAttribute('aria-current', 'step')
    expect(screen.getByText('Install').closest('li')).toHaveAttribute('data-state', 'done')
    expect(screen.queryByTestId('provider-step')).toBeNull()

    useConnection.getState().setState('connected')
    view.rerender(<SetupOverlay />)
    fireEvent.click(screen.getByTestId('provider-step'))
    expect(useBootstrap.getState().dismissed).toBe(true)
    view.rerender(<SetupOverlay />)
    expect(screen.queryByTestId('provider-step')).toBeNull()
  })
})

describe('formatElapsed', () => {
  it('formats seconds and minutes', () => {
    expect(formatElapsed(0)).toBe('0s')
    expect(formatElapsed(59_400)).toBe('59s')
    expect(formatElapsed(65_000)).toBe('1m 05s')
    expect(formatElapsed(600_000)).toBe('10m 00s')
  })
})
