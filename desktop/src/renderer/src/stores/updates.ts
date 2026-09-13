import { create } from 'zustand'
import {
  IDLE_ENGINE,
  idleAppState,
  type AppUpdateState,
  type EngineUpdateState,
} from '@shared/updates'
import { desktopApi } from '~/lib/desktop-api'

interface UpdatesStore {
  engine: EngineUpdateState
  app: AppUpdateState
  loaded: boolean
  load(): Promise<void>
  checkEngine(): Promise<void>
  applyEngine(): Promise<void>
  checkApp(): Promise<void>
  downloadApp(): Promise<void>
  installApp(): Promise<void>
  /** Both at once: check each, then upgrade the engine and fetch the app. */
  checkAll(): Promise<void>
  updateAll(): Promise<void>
}

/** Renderer mirror of main's EngineUpdater + AppUpdateController. */
export const useUpdates = create<UpdatesStore>((set, get) => ({
  engine: { ...IDLE_ENGINE },
  app: idleAppState(''),
  loaded: false,
  async load() {
    const api = desktopApi().updates
    const [engine, app] = await Promise.all([api.engine.state(), api.app.state()])
    set({ engine, app, loaded: true })
  },
  async checkEngine() {
    set({ engine: await desktopApi().updates.engine.check() })
  },
  async applyEngine() {
    set({ engine: await desktopApi().updates.engine.apply() })
  },
  async checkApp() {
    set({ app: await desktopApi().updates.app.check() })
  },
  async downloadApp() {
    set({ app: await desktopApi().updates.app.download() })
  },
  async installApp() {
    set({ app: await desktopApi().updates.app.install() })
  },
  async checkAll() {
    await Promise.all([get().checkEngine(), get().checkApp()])
  },
  async updateAll() {
    // Engine first: it does not need a relaunch, and the new app may require
    // the new engine. The app download then waits for a "Restart" click.
    if (get().engine.availability === 'outdated') await get().applyEngine()
    if (get().app.phase === 'available') await get().downloadApp()
  },
}))

/** Subscribe once from the app root; returns a disposer. */
export function bindUpdateEvents(): () => void {
  void useUpdates.getState().load()
  const api = desktopApi().updates
  const offEngine = api.engine.onChanged((engine) => useUpdates.setState({ engine }))
  const offApp = api.app.onChanged((app) => useUpdates.setState({ app }))
  return () => {
    offEngine()
    offApp()
  }
}
