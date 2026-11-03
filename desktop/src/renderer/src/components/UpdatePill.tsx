import './update-pill.css'
import { AlertTriangle, ArrowDownToLine, LoaderCircle, RotateCw } from 'lucide-react'
import { releaseUpdate } from '@shared/updates'
import { t } from '~/i18n'
import { useGatewayStatus } from '~/lib/use-gateway-status'
import { useSettings } from '~/stores/settings'
import { useUi } from '~/stores/ui'
import { useUpdates } from '~/stores/updates'

/**
 * The toolbar's standing notice that a newer AgentOS is waiting, for the
 * engine and the app together. Unlike the toast, it stays until the release
 * is fully applied: "Update" while something can be fetched, the running
 * step while it is, "Restart" once the app has landed, "Restart gateway"
 * when only the running gateway lags the engine on disk, "Gateway outdated"
 * when an external gateway runs an older release than the latest (it is
 * updated on its own host). Clicking it opens Settings › About, where the
 * full cards and every button live.
 */
export function UpdatePill() {
  const engine = useUpdates((s) => s.engine)
  const app = useUpdates((s) => s.app)
  const external = useSettings((s) => s.settings.gateway.mode === 'external')
  const status = useGatewayStatus()
  const openSettings = useUi((s) => s.openSettings)
  const release = releaseUpdate(engine, app, status.data?.version, { external })
  if (release.kind === 'none') return null

  const Icon =
    release.kind === 'restart' || release.kind === 'gateway-restart'
      ? RotateCw
      : release.kind === 'working'
        ? LoaderCircle
        : release.kind === 'failed' || release.kind === 'gateway-outdated'
          ? AlertTriangle
          : ArrowDownToLine
  const label =
    release.kind === 'available'
      ? t('updates.pill.available')
      : release.kind === 'working'
        ? release.step === 'engine'
          ? t('updates.pill.engine')
          : t('updates.pill.app')
        : release.kind === 'restart'
          ? t('updates.pill.restart')
          : release.kind === 'failed'
            ? t('updates.pill.failed')
            : release.kind === 'gateway-outdated'
              ? t('updates.pill.gatewayOutdated')
              : t('updates.pill.gateway')
  const title = `${label} · AgentOS ${release.version}`.trim()
  return (
    <button
      type="button"
      className="update-pill"
      data-kind={release.kind}
      data-ready={release.kind === 'restart' ? 'true' : undefined}
      data-busy={release.kind === 'working' ? 'true' : undefined}
      aria-label={title}
      title={title}
      onClick={() => openSettings('about')}
    >
      <Icon className="size-3.5" strokeWidth={2} aria-hidden />
      <span>{label}</span>
      {release.kind === 'working' && release.step === 'app' ? (
        <span className="update-pill__pct">{release.percent ?? 0}%</span>
      ) : null}
    </button>
  )
}
