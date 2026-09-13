// @vitest-environment node
import { EventEmitter } from 'node:events'
import { describe, expect, it, vi } from 'vitest'
import { AppUpdateController, type UpdaterLike } from './app-updater'

function fakeUpdater() {
  const emitter = new EventEmitter()
  const updater = {
    autoDownload: true,
    autoInstallOnAppQuit: false,
    allowPrerelease: true,
    on: (event: string, listener: (...args: unknown[]) => void) => emitter.on(event, listener),
    checkForUpdates: vi.fn(async () => {
      emitter.emit('checking-for-update')
    }),
    downloadUpdate: vi.fn(async () => {
      emitter.emit('download-progress', { percent: 42.6 })
      emitter.emit('update-downloaded', { version: '2026.9.12' })
    }),
    quitAndInstall: vi.fn(),
  } as unknown as UpdaterLike & {
    checkForUpdates: ReturnType<typeof vi.fn>
    downloadUpdate: ReturnType<typeof vi.fn>
    quitAndInstall: ReturnType<typeof vi.fn>
  }
  return { updater, emitter }
}

describe('AppUpdateController', () => {
  it('is unsupported without an updater (dev build)', async () => {
    const ctl = new AppUpdateController({
      updater: null,
      version: '0.1.0',
      beforeInstall: async () => {},
    })
    expect(ctl.current()).toMatchObject({ phase: 'unsupported', current: '0.1.0' })
    expect((await ctl.check()).phase).toBe('unsupported')
    expect((await ctl.download()).phase).toBe('unsupported')
  })

  it('configures explicit downloads and install-on-quit', () => {
    const { updater } = fakeUpdater()
    new AppUpdateController({ updater, version: '1', beforeInstall: async () => {} })
    expect(updater.autoDownload).toBe(false)
    expect(updater.autoInstallOnAppQuit).toBe(true)
    expect(updater.allowPrerelease).toBe(false)
  })

  it('walks checking → available → downloading → downloaded, then installs', async () => {
    const { updater, emitter } = fakeUpdater()
    const beforeInstall = vi.fn(async () => {})
    const ctl = new AppUpdateController({
      updater,
      version: '2026.9.9',
      beforeInstall,
      now: () => 5,
    })
    const phases: string[] = []
    ctl.subscribe((s) => phases.push(s.phase))

    await ctl.check()
    emitter.emit('update-available', { version: '2026.9.12' })
    expect(ctl.current()).toMatchObject({ phase: 'available', latest: '2026.9.12', checkedAt: 5 })

    await ctl.download()
    expect(ctl.current()).toMatchObject({ phase: 'downloaded', latest: '2026.9.12', percent: 100 })
    expect(phases).toEqual(['checking', 'available', 'downloading', 'downloading', 'downloaded'])

    await ctl.install()
    expect(beforeInstall).toHaveBeenCalledTimes(1)
    expect(updater.quitAndInstall).toHaveBeenCalledWith(false, true)
  })

  it('reports up-to-date and errors', async () => {
    const { updater, emitter } = fakeUpdater()
    const ctl = new AppUpdateController({ updater, version: '1', beforeInstall: async () => {} })
    await ctl.check()
    emitter.emit('update-not-available', { version: '1' })
    expect(ctl.current().phase).toBe('up-to-date')
    emitter.emit('error', new Error('net down'))
    expect(ctl.current()).toMatchObject({ phase: 'error', error: 'net down' })
  })

  it('turns a rejected check into an error state', async () => {
    const { updater } = fakeUpdater()
    updater.checkForUpdates.mockRejectedValue(new Error('403'))
    const ctl = new AppUpdateController({ updater, version: '1', beforeInstall: async () => {} })
    expect((await ctl.check()).error).toBe('403')
  })

  it('download and install are no-ops outside their phase', async () => {
    const { updater } = fakeUpdater()
    const ctl = new AppUpdateController({ updater, version: '1', beforeInstall: async () => {} })
    await ctl.download()
    await ctl.install()
    expect(updater.downloadUpdate).not.toHaveBeenCalled()
    expect(updater.quitAndInstall).not.toHaveBeenCalled()
  })
})
