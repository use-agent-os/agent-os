import type { BootstrapController } from '../bootstrap/controller'
import type { BootstrapRunner } from '../bootstrap/runner'
import type { GatewaySupervisor } from '../gateway/supervisor'
import type { PetStore } from '../pets/store'
import type { SettingsStore } from '../settings/store'
import type { AppUpdateController } from '../updates/app-updater'
import type { EngineUpdater } from '../updates/engine-updater'
import { registerAppIpc } from './app'
import { registerBootstrapIpc } from './bootstrap'
import { registerGatewayIpc } from './gateway'
import { registerNotifyIpc } from './notify'
import { registerPetsIpc } from './pets'
import { registerSettingsIpc } from './settings'
import { registerThemeIpc } from './theme'
import { registerUpdatesIpc } from './updates'

export interface MainServices {
  settings: SettingsStore
  gateway: GatewaySupervisor
  pets: PetStore
  engineUpdater: EngineUpdater
  appUpdater: AppUpdateController
  bootstrapRunner: BootstrapRunner
  bootstrap: BootstrapController
}

/** Register every IPC handler exactly once, before the first window opens. */
export function registerIpc(services: MainServices): void {
  registerSettingsIpc(services.settings)
  registerAppIpc(services.settings)
  registerThemeIpc(services.settings)
  registerGatewayIpc(services.gateway)
  registerPetsIpc(services.pets)
  registerNotifyIpc()
  registerUpdatesIpc(services.engineUpdater, services.appUpdater)
  registerBootstrapIpc(services.bootstrapRunner, services.bootstrap)
}
