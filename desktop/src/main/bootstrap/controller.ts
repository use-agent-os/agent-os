import { spawn as nodeSpawn, type ChildProcess } from 'node:child_process'
import type { BootstrapState, EngineDiscovery } from '@shared/bootstrap'
import type { GatewayStatus } from '@shared/gateway'
import type { GatewaySettings, SettingsPatch } from '@shared/settings'
import { hardenedEnv } from '../updates/engine-updater'
import { discoverEngine, type ProbeResult } from './discovery'
import type { BootstrapRunner } from './runner'

export interface BootstrapControllerDeps {
  runner: BootstrapRunner
  gateway: {
    start(): Promise<GatewayStatus>
    stop(): Promise<GatewayStatus>
    current(): GatewayStatus
    /** Connect to a gateway already listening; null when none is. */
    adopt(): Promise<GatewayStatus | null>
  }
  getSettings: () => GatewaySettings
  updateSettings: (patch: SettingsPatch) => void
  appVersion: string
  locate?: (override: string | null) => string | null
  probe?: (cli: string) => Promise<ProbeResult>
  spawn?: typeof nodeSpawn
}

/**
 * The launch sequence: find the engine, install or update it if this app
 * needs to, then start the gateway. Every later launch takes the fast path
 * (probe says "same version" → start), so the installer only ever shows
 * when there is work to do.
 */
export class BootstrapController {
  constructor(private readonly deps: BootstrapControllerDeps) {}

  /** Runs once at app ready. Resolves when the gateway start has been issued. */
  async launch(): Promise<BootstrapState> {
    const settings = this.deps.getSettings()
    if (settings.mode === 'external') {
      // Nothing to install for a gateway that lives elsewhere.
      void this.deps.gateway.start()
      return this.deps.runner.markReady(await this.discover())
    }
    const discovery = await this.discover()
    if (!discovery.needsInstall) {
      void this.deps.gateway.start()
      return this.deps.runner.markReady(discovery)
    }
    // An engine we would install over may already be serving a gateway (a
    // terminal, a previous launch). That gateway is usable now; adopt it and
    // leave the update to Settings › About rather than blocking the window.
    if (await this.deps.gateway.adopt()) {
      return this.deps.runner.markReady(discovery)
    }
    return this.deps.runner.offer(discovery)
  }

  /** "Install here": run the stages, then start the gateway on success. */
  async install(): Promise<BootstrapState> {
    const state = await this.deps.runner.run()
    if (state.phase === 'succeeded') {
      // A freshly installed CLI lives in the default location; a stale
      // override pointing elsewhere would keep the app on the old binary.
      const settings = this.deps.getSettings()
      if (settings.cliPath && !this.locateFn()(settings.cliPath)) {
        this.deps.updateSettings({ gateway: { cliPath: null } })
      }
      void this.deps.gateway.start()
    }
    return state
  }

  /** "Connect to an existing gateway": external mode, no install. */
  async connectExisting(): Promise<BootstrapState> {
    this.deps.updateSettings({ gateway: { mode: 'external' } })
    void this.deps.gateway.start()
    return this.deps.runner.dismiss()
  }

  /** Settings › Advanced: install this app's engine again, over whatever is there. */
  async reinstall(): Promise<BootstrapState> {
    const discovery = await this.discover()
    this.deps.runner.offer({ ...discovery, needsInstall: true, reason: 'Reinstall requested.' })
    return this.install()
  }

  /** Settings › Advanced: `uv tool uninstall use-agent-os` after stopping the gateway. */
  async uninstallEngine(): Promise<{ ok: boolean; detail: string }> {
    await this.deps.gateway.stop()
    const spawn = this.deps.spawn ?? nodeSpawn
    return new Promise((resolve) => {
      let child: ChildProcess
      try {
        child = spawn('uv', ['tool', 'uninstall', 'use-agent-os'], {
          env: hardenedEnv(),
          stdio: ['ignore', 'pipe', 'pipe'],
        })
      } catch (err) {
        resolve({ ok: false, detail: String(err) })
        return
      }
      let output = ''
      child.stdout?.on('data', (c: Buffer) => (output += c.toString()))
      child.stderr?.on('data', (c: Buffer) => (output += c.toString()))
      child.once('error', (err) => resolve({ ok: false, detail: err.message }))
      child.once('exit', (code) => {
        resolve({ ok: code === 0, detail: output.trim().split('\n').slice(-3).join('\n') })
      })
    })
  }

  async discover(): Promise<EngineDiscovery> {
    return discoverEngine({
      settings: this.deps.getSettings(),
      appVersion: this.deps.appVersion,
      locate: this.deps.locate,
      probe: this.deps.probe,
    })
  }

  private locateFn(): (override: string | null) => string | null {
    return this.deps.locate ?? ((override) => override)
  }
}
