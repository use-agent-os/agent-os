import { idleAppState, type AppUpdateState } from '@shared/updates'

type Listener = (state: AppUpdateState) => void

/**
 * The slice of `electron-updater`'s `AppUpdater` this controller drives.
 * Declared here so the controller (and its tests) never import the package,
 * which insists on a real Electron `app` at load time.
 */
export interface UpdaterLike {
  autoDownload: boolean
  autoInstallOnAppQuit: boolean
  allowPrerelease: boolean
  on(event: 'checking-for-update', listener: () => void): unknown
  on(event: 'update-available', listener: (info: { version: string }) => void): unknown
  on(event: 'update-not-available', listener: (info: { version: string }) => void): unknown
  on(event: 'download-progress', listener: (progress: { percent: number }) => void): unknown
  on(event: 'update-downloaded', listener: (info: { version: string }) => void): unknown
  on(event: 'error', listener: (error: Error) => void): unknown
  checkForUpdates(): Promise<unknown>
  downloadUpdate(): Promise<unknown>
  quitAndInstall(isSilent?: boolean, isForceRunAfter?: boolean): void
}

export interface AppUpdateControllerDeps {
  /** `null` when there is nothing to update from (dev build, no channel). */
  updater: UpdaterLike | null
  version: string
  /** Runs before `quitAndInstall`: stop the managed gateway, mark the quit. */
  beforeInstall: () => Promise<void>
  now?: () => number
}

/**
 * State machine over `electron-updater` for the About pane. Downloads are
 * explicit (never behind the user's back on a metered link) and the swap
 * happens on relaunch, the way macOS apps do it: nothing is replaced under a
 * running window.
 */
export class AppUpdateController {
  private state: AppUpdateState
  private readonly listeners = new Set<Listener>()
  private readonly now: () => number

  constructor(private readonly deps: AppUpdateControllerDeps) {
    this.now = deps.now ?? Date.now
    this.state = idleAppState(deps.version)
    if (!deps.updater) {
      this.state = { ...this.state, phase: 'unsupported' }
      return
    }
    const u = deps.updater
    u.autoDownload = false
    u.autoInstallOnAppQuit = true
    u.allowPrerelease = false
    u.on('checking-for-update', () => this.set({ ...this.state, phase: 'checking', error: null }))
    u.on('update-available', (info) =>
      this.set({
        ...this.state,
        phase: 'available',
        latest: info.version,
        percent: null,
        checkedAt: this.now(),
      }),
    )
    u.on('update-not-available', (info) =>
      this.set({
        ...this.state,
        phase: 'up-to-date',
        latest: info.version,
        checkedAt: this.now(),
      }),
    )
    u.on('download-progress', (progress) =>
      this.set({
        ...this.state,
        phase: 'downloading',
        percent: Math.max(0, Math.min(100, Math.round(progress.percent))),
      }),
    )
    u.on('update-downloaded', (info) =>
      this.set({ ...this.state, phase: 'downloaded', latest: info.version, percent: 100 }),
    )
    u.on('error', (error) =>
      this.set({ ...this.state, phase: 'error', percent: null, error: error.message }),
    )
  }

  current(): AppUpdateState {
    return { ...this.state }
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  async check(): Promise<AppUpdateState> {
    const u = this.deps.updater
    if (!u || this.state.phase === 'checking' || this.state.phase === 'downloading') {
      return this.current()
    }
    // A downloaded update stays downloaded; re-checking would only re-offer it.
    if (this.state.phase === 'downloaded') return this.current()
    try {
      await u.checkForUpdates()
    } catch (err) {
      this.set({ ...this.state, phase: 'error', error: errorMessage(err) })
    }
    return this.current()
  }

  async download(): Promise<AppUpdateState> {
    const u = this.deps.updater
    if (!u || this.state.phase !== 'available') return this.current()
    this.set({ ...this.state, phase: 'downloading', percent: 0, error: null })
    try {
      await u.downloadUpdate()
    } catch (err) {
      this.set({ ...this.state, phase: 'error', percent: null, error: errorMessage(err) })
    }
    return this.current()
  }

  async install(): Promise<AppUpdateState> {
    const u = this.deps.updater
    if (!u || this.state.phase !== 'downloaded') return this.current()
    await this.deps.beforeInstall()
    // Not silent (Squirrel shows its own progress) and relaunch afterwards.
    u.quitAndInstall(false, true)
    return this.current()
  }

  private set(next: AppUpdateState): AppUpdateState {
    this.state = next
    for (const fn of this.listeners) fn({ ...next })
    return { ...next }
  }
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}
