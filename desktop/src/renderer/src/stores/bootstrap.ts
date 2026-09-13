import { create } from 'zustand'
import {
  INITIAL_BOOTSTRAP,
  type BootstrapState,
  type LogLine,
  type StageProgress,
} from '@shared/bootstrap'
import { desktopApi } from '~/lib/desktop-api'

interface BootstrapStore {
  state: BootstrapState
  loaded: boolean
  /** The user closed the success screen; the overlay stays away this session. */
  dismissed: boolean
  /**
   * Step 2's own state, kept here rather than in the component: saving a
   * provider restarts the gateway, the connection drops for a moment and the
   * step unmounts. Without this the person lands back on the grid.
   */
  provider: { selected: string | null; saved: string | null }
  setProvider(patch: Partial<{ selected: string | null; saved: string | null }>): void
  load(): Promise<void>
  install(): Promise<void>
  cancel(): Promise<void>
  connectExisting(): Promise<void>
  reinstall(): Promise<void>
  uninstallEngine(): Promise<{ ok: boolean; detail: string }>
  openLog(): Promise<void>
  dismiss(): void
}

/** Renderer mirror of main's BootstrapRunner, kept fresh by IPC pushes. */
export const useBootstrap = create<BootstrapStore>((set) => ({
  state: { ...INITIAL_BOOTSTRAP },
  loaded: false,
  dismissed: false,
  provider: { selected: null, saved: null },
  setProvider(patch) {
    set((s) => ({ provider: { ...s.provider, ...patch } }))
  },
  async load() {
    set({ state: await desktopApi().bootstrap.state(), loaded: true })
  },
  async install() {
    set({ dismissed: false })
    set({ state: await desktopApi().bootstrap.install() })
  },
  async cancel() {
    set({ state: await desktopApi().bootstrap.cancel() })
  },
  async connectExisting() {
    set({ state: await desktopApi().bootstrap.connectExisting() })
  },
  async reinstall() {
    set({ dismissed: false })
    set({ state: await desktopApi().bootstrap.reinstall() })
  },
  uninstallEngine() {
    return desktopApi().bootstrap.uninstallEngine()
  },
  openLog() {
    return desktopApi().bootstrap.openLog()
  },
  dismiss() {
    set({ dismissed: true })
  },
}))

/** Subscribe once from the app root; returns a disposer. */
export function bindBootstrapEvents(): () => void {
  if (import.meta.env.DEV && startFakeBoot()) return () => {}
  void useBootstrap.getState().load()
  return desktopApi().bootstrap.onChanged((state) => useBootstrap.setState({ state }))
}

/**
 * Development only: `?fake=install|update|failure` in the renderer URL plays
 * a synthetic install so every setup screen can be designed in a browser
 * tab with nothing installed and nothing at risk.
 */
function startFakeBoot(): boolean {
  let mode: string | null = null
  try {
    mode = new URLSearchParams(window.location.search).get('fake')
  } catch {
    return false
  }
  if (mode !== 'install' && mode !== 'update' && mode !== 'failure') return false

  const stages: StageProgress[] = [
    ['prerequisites', 'Check this Mac'],
    ['uv', 'Install the uv package manager'],
    ['python', 'Install Python 3.12'],
    ['package', 'Install the AgentOS engine'],
    ['path', 'Put agentos on your PATH'],
    ['complete', 'Finish'],
  ].map(([name, title]) => ({
    name: name!,
    title: title!,
    category: 'runtime',
    needsUserInput: false,
    state: 'pending',
    startedAt: null,
    durationMs: null,
    reason: null,
  }))
  const discovery = {
    source: mode === 'update' ? ('found' as const) : ('missing' as const),
    cliPath: mode === 'update' ? '/Users/you/.local/bin/agentos' : null,
    version: mode === 'update' ? '2026.8.23' : null,
    appVersion: '2026.9.12',
    relation: mode === 'update' ? ('older' as const) : null,
    needsInstall: true,
    reason: 'fake',
  }
  const base: BootstrapState = {
    ...INITIAL_BOOTSTRAP,
    phase: 'choice',
    discovery,
    mode: mode === 'update' ? 'update' : 'install',
    stages: [],
  }
  useBootstrap.setState({ state: base, loaded: true })

  const play = async () => {
    const log: LogLine[] = []
    const emit = (patch: Partial<BootstrapState>) =>
      useBootstrap.setState((s) => ({ state: { ...s.state, ...patch } }))
    emit({ phase: 'running', stages: stages.map((s) => ({ ...s })), startedAt: Date.now() })
    const failAt = mode === 'failure' ? 'package' : null
    for (const [i, stage] of stages.entries()) {
      const startedAt = Date.now()
      stages[i] = { ...stage, state: 'running', startedAt }
      emit({ stages: stages.map((s) => ({ ...s })) })
      for (let n = 0; n < 4; n++) {
        await new Promise((r) => setTimeout(r, 350))
        log.push({
          stage: stage.name,
          stream: n % 3 === 2 ? 'stderr' : 'stdout',
          line: `install.sh: ${stage.name} step ${n + 1}…`,
        })
        emit({ log: [...log] })
      }
      if (stage.name === failAt) {
        stages[i] = {
          ...stages[i]!,
          state: 'failed',
          durationMs: Date.now() - startedAt,
          reason: 'stage failed (exit 1)',
        }
        log.push({
          stage: stage.name,
          stream: 'stderr',
          line: 'error: failed to resolve wheel: 404',
        })
        emit({
          stages: stages.map((s) => ({ ...s })),
          log: [...log],
          phase: 'failed',
          error: '"Install the AgentOS engine" failed: stage failed (exit 1)',
          finishedAt: Date.now(),
          logPath: '/Users/you/.agentos/logs/bootstrap-fake.log',
        })
        return
      }
      stages[i] = { ...stages[i]!, state: 'succeeded', durationMs: Date.now() - startedAt }
      emit({ stages: stages.map((s) => ({ ...s })) })
    }
    emit({ phase: 'succeeded', finishedAt: Date.now() })
  }

  // The real store methods talk to main; in fake mode they drive the script.
  useBootstrap.setState({
    install: async () => {
      useBootstrap.setState({ dismissed: false })
      await play()
    },
    reinstall: async () => {
      await play()
    },
    cancel: async () => {
      useBootstrap.setState((s) => ({
        state: { ...s.state, phase: 'cancelled', error: 'Cancelled.' },
      }))
    },
    connectExisting: async () => {
      useBootstrap.setState((s) => ({ state: { ...s.state, phase: 'ready' } }))
    },
  })
  return true
}
