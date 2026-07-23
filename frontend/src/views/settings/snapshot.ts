import type { Catalog, OnboardingStatus, SectionDetail, SetupConfig } from '@/views/setup/logic'
import type { WsRpcClient } from '@/lib/ws-rpc'

export const SETTINGS_SNAPSHOT_QUERY_KEY = ['settings', 'snapshot'] as const

export function isMethodNotFoundRpcError(error: unknown): boolean {
  return Boolean(
    error &&
    typeof error === 'object' &&
    'code' in error &&
    (error as { code?: unknown }).code === 'METHOD_NOT_FOUND',
  )
}

export interface SettingsReadiness {
  state?: 'ready' | 'action_required' | 'restart_required'
  coreReady?: boolean
  runtimeReady?: boolean
  needsOnboarding?: boolean
  sections?: Record<string, string>
  sectionDetails?: Record<string, SectionDetail>
  total?: number
  ready?: number
  actionRequired?: number
  required?: number
  optional?: number
}

export interface SettingsSnapshot {
  catalog?: Catalog
  status?: OnboardingStatus
  config?: SetupConfig
  readiness?: SettingsReadiness
  revision?: string | null
  configPath?: string
  pendingRestart?: boolean
  restartReasons?: string[]
  diskDiverged?: boolean
  writeBlocked?: boolean
}

export async function loadSettingsSnapshot(
  rpc: Pick<WsRpcClient, 'call' | 'waitForConnection'>,
): Promise<SettingsSnapshot> {
  await rpc.waitForConnection()
  try {
    const snapshot = await rpc.call<SettingsSnapshot>('config.snapshot')
    if (!snapshot || !Object.prototype.hasOwnProperty.call(snapshot, 'config')) {
      throw new Error('config.snapshot returned an invalid response')
    }
    return snapshot
  } catch (error) {
    if (!isMethodNotFoundRpcError(error)) throw error
    // Compatibility path for a UI served while an older gateway process is
    // still running. The next reload uses the atomic snapshot after restart.
    const [catalog, status, config] = await Promise.all([
      rpc.call<SettingsSnapshot['catalog']>('onboarding.catalog'),
      rpc.call<SettingsSnapshot['status']>('onboarding.status'),
      rpc.call<SettingsSnapshot['config']>('config.get'),
    ])
    return { catalog: catalog ?? {}, status: status ?? {}, config: config ?? {} }
  }
}

export function readinessFromSnapshot(
  snapshot: SettingsSnapshot | undefined,
): Required<
  Pick<SettingsReadiness, 'total' | 'ready' | 'actionRequired' | 'required' | 'optional'>
> {
  const explicit = snapshot?.readiness
  const runtimeNeedsAction = Boolean(
    explicit?.state === 'action_required' ||
    explicit?.coreReady === false ||
    explicit?.runtimeReady === false ||
    explicit?.needsOnboarding === true ||
    snapshot?.status?.needsOnboarding === true,
  )
  if (
    explicit?.total !== undefined &&
    explicit.ready !== undefined &&
    explicit.actionRequired !== undefined
  ) {
    const actionRequired = Math.max(explicit.actionRequired, runtimeNeedsAction ? 1 : 0)
    return {
      total: explicit.total,
      ready: Math.min(explicit.ready, Math.max(explicit.total - actionRequired, 0)),
      actionRequired,
      required: explicit.required ?? 0,
      optional: explicit.optional ?? 0,
    }
  }

  const sectionDetails = explicit?.sectionDetails ?? snapshot?.status?.sectionDetails
  const derivedDetails =
    sectionDetails ??
    Object.fromEntries(
      Object.entries(explicit?.sections ?? {}).map(([section, sectionStatus]) => [
        section,
        { status: sectionStatus },
      ]),
    )
  const details = Object.values(derivedDetails)
  const needsAction = (entry: SectionDetail) =>
    Boolean(
      entry.blocking ||
      entry.actionRequired ||
      entry.status === 'missing' ||
      entry.status === 'degraded' ||
      entry.status === 'unknown',
    )
  const actionEntries = details.filter(needsAction)
  const actionRequired = Math.max(actionEntries.length, runtimeNeedsAction ? 1 : 0)
  const total = Math.max(details.length, runtimeNeedsAction && details.length === 0 ? 1 : 0)
  return {
    total,
    ready: details.filter((entry) => entry.status === 'ok').length,
    actionRequired,
    required: Math.max(
      actionEntries.filter((entry) => entry.required).length,
      runtimeNeedsAction && actionEntries.length === 0 ? 1 : 0,
    ),
    optional: actionEntries.filter((entry) => !entry.required).length,
  }
}
