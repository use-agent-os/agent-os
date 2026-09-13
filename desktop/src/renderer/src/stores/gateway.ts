import { create } from 'zustand'
import { STOPPED_GATEWAY, type GatewayStatus } from '@shared/gateway'
import { desktopApi } from '~/lib/desktop-api'

interface GatewayStore {
  status: GatewayStatus
  busy: boolean
  refresh(): Promise<void>
  start(): Promise<void>
  stop(): Promise<void>
  restart(): Promise<void>
}

/** Mirror of the main-process GatewaySupervisor, kept fresh by IPC pushes. */
export const useGateway = create<GatewayStore>((set) => ({
  status: { ...STOPPED_GATEWAY },
  busy: false,
  async refresh() {
    set({ status: await desktopApi().gateway.status() })
  },
  async start() {
    set({ busy: true })
    try {
      set({ status: await desktopApi().gateway.start() })
    } finally {
      set({ busy: false })
    }
  },
  async stop() {
    set({ busy: true })
    try {
      set({ status: await desktopApi().gateway.stop() })
    } finally {
      set({ busy: false })
    }
  },
  async restart() {
    set({ busy: true })
    try {
      set({ status: await desktopApi().gateway.restart() })
    } finally {
      set({ busy: false })
    }
  },
}))

/** Subscribe once from the app root; returns a disposer. */
export function bindGatewayEvents(): () => void {
  void useGateway.getState().refresh()
  return desktopApi().gateway.onChanged((status) => useGateway.setState({ status }))
}
