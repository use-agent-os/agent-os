import { useQuery } from '@tanstack/react-query'
import { ExternalLink, LoaderCircle } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useRpc } from '@/app/providers'
import { tPlural } from '@/i18n'
import { useUpdatePreflight, type PreflightState } from '@/lib/update-preflight'
import { useConnection } from '@/stores/connection'
import type { AppInfo } from '@shared/app'
import {
  gatewayBehind,
  gatewaySupported,
  isNewer,
  latestRelease,
  MIN_GATEWAY_VERSION,
  type AppUpdateState,
  type EngineUpdateState,
} from '@shared/updates'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'
import { useAttentionPanel } from '~/stores/attention'
import { useSettings } from '~/stores/settings'
import { useUi } from '~/stores/ui'
import { useUpdates } from '~/stores/updates'
import { formatUptime } from '../logic'
import { Card, Head, Notice, Pill, Row, Value, type Tone } from '../parts'
import agentosMark from '@/assets/agentos-mark.png'

const REPO = 'https://github.com/use-agent-os/agent-os'
const LINKS = [
  { label: 'settings.about.github', url: REPO },
  { label: 'settings.about.releases', url: `${REPO}/releases` },
  { label: 'settings.about.issues', url: `${REPO}/issues/new` },
] as const

interface StatusResult {
  version?: string
  uptime_ms?: number
  provider?: string | null
  active_sessions?: number
}
interface DataCheck {
  ok: boolean
  checked: string[]
  problems: { path: string; result: string }[]
  snapshot: string | null
}

export function AboutPane() {
  const connected = useConnection((s) => s.state === 'connected')
  const rpc = useRpc()
  const [info, setInfo] = useState<AppInfo | null>(null)
  const engine = useUpdates((s) => s.engine)
  const app = useUpdates((s) => s.app)
  const loaded = useUpdates((s) => s.loaded)
  const checkAll = useUpdates((s) => s.checkAll)
  const updateAll = useUpdates((s) => s.updateAll)
  const applyEngine = useUpdates((s) => s.applyEngine)
  // External run mode: the engine package on this machine is not the one
  // running, so the Engine card gives way to a read-only Gateway card and
  // "Update all" (which would install that engine) is not offered.
  const external = useSettings((s) => s.settings.gateway.mode === 'external')
  // Every engine update restarts the gateway, so it goes through the
  // preflight first: what would be interrupted, blocked while an approval
  // waits, applied only once confirmed. Which update is waiting on it:
  const [pendingUpdate, setPendingUpdate] = useState<'engine' | 'all' | null>(null)
  const preflight = useUpdatePreflight()
  const askToUpdate = (which: 'engine' | 'all') => {
    setPendingUpdate(which)
    preflight.run()
  }

  useEffect(() => {
    let cancelled = false
    void desktopApi()
      .app.info()
      .then((i) => {
        if (!cancelled) setInfo(i)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const status = useQuery({
    queryKey: ['settings', 'status'],
    enabled: connected,
    refetchInterval: 30_000,
    queryFn: () => rpc.call<StatusResult>('status'),
  })

  const open = (url: string) => void desktopApi().app.openExternal(url)
  const busy =
    engine.phase === 'checking' ||
    engine.phase === 'installing' ||
    engine.phase === 'restarting' ||
    app.phase === 'checking' ||
    app.phase === 'downloading'
  const engineOutdated = !external && engine.availability === 'outdated'
  const appOutdated = app.phase === 'available'
  const gatewayVersion = connected ? (status.data?.version ?? null) : null

  return (
    <>
      <Head title={t('settings.section.about')} />
      <div className="stg-hero">
        <div className="stg-hero__mark" aria-hidden>
          <img src={agentosMark} alt="" draggable={false} />
        </div>
        <div className="stg-hero__text">
          <div className="stg-hero__name">{t('settings.about.app')}</div>
          <div className="stg-hero__meta">
            {t('settings.about.version')} {info?.version ?? '…'}
            {info && !info.packaged ? ` · ${t('settings.about.dev')}` : ''}
            {' · '}
            {t('settings.about.license')}
          </div>
        </div>
        <div className="stg-hero__actions">
          {engineOutdated && appOutdated ? (
            <Button
              variant="primary"
              disabled={busy || pendingUpdate !== null}
              onClick={() => askToUpdate('all')}
            >
              {t('settings.about.updateAll')}
            </Button>
          ) : null}
          <Button disabled={busy || !loaded} onClick={() => void checkAll()}>
            {engine.phase === 'checking' || app.phase === 'checking' ? (
              <>
                <LoaderCircle className="stg-spin size-3.5" strokeWidth={1.75} aria-hidden />
                {t('settings.about.checking')}
              </>
            ) : (
              t('settings.about.check')
            )}
          </Button>
        </div>
      </div>

      {engineOutdated && appOutdated ? <Notice>{t('settings.about.bothOutdated')}</Notice> : null}
      {!gatewaySupported(gatewayVersion) ? (
        <Notice tone="danger">
          {t('settings.about.engine.tooOld')} <b>{MIN_GATEWAY_VERSION}</b>{' '}
          {t('settings.about.engine.tooOld.rest')} <b>{gatewayVersion}</b>.
        </Notice>
      ) : null}

      {external ? (
        <ExternalGatewayCard
          engine={engine}
          app={app}
          gatewayVersion={gatewayVersion}
          uptimeMs={connected ? status.data?.uptime_ms : undefined}
          connected={connected}
        />
      ) : (
        <EngineCard
          engine={engine}
          gatewayVersion={gatewayVersion}
          uptimeMs={connected ? status.data?.uptime_ms : undefined}
          connected={connected}
          onRestarted={() => void status.refetch()}
          confirming={pendingUpdate !== null}
          preflight={preflight}
          onRequestUpdate={() => askToUpdate('engine')}
          onCancelUpdate={() => setPendingUpdate(null)}
          onConfirmUpdate={() => {
            const which = pendingUpdate
            setPendingUpdate(null)
            void (which === 'all' ? updateAll() : applyEngine())
          }}
        />
      )}

      <AppCard />

      <Card title={t('settings.about.runtime')}>
        <Row label={t('settings.about.electron')}>
          <Value>{info?.electron || '—'}</Value>
        </Row>
        <Row label={t('settings.about.chrome')}>
          <Value>{info?.chrome || '—'}</Value>
        </Row>
      </Card>

      <Card title={t('settings.about.links')}>
        {LINKS.map((link) => (
          <Row key={link.url} label={t(link.label)}>
            <Button
              variant="ghost"
              size="icon"
              aria-label={t(link.label)}
              title={link.url}
              onClick={() => open(link.url)}
            >
              <ExternalLink
                className="size-3.5 text-muted-foreground"
                strokeWidth={1.75}
                aria-hidden
              />
            </Button>
          </Row>
        ))}
      </Card>
    </>
  )
}

/**
 * External run mode's stand-in for the Engine card: what the connected
 * gateway runs and the latest release, nothing more. The local engine's
 * "Installed" version is left out (it is not what runs) and there is no
 * update button: the gateway is updated on its own host.
 */
function ExternalGatewayCard({
  engine,
  app,
  gatewayVersion,
  uptimeMs,
  connected,
}: {
  engine: EngineUpdateState
  app: AppUpdateState
  gatewayVersion: string | null
  uptimeMs: number | undefined
  connected: boolean
}) {
  const latest = latestRelease(engine, app)
  const behind = gatewayBehind(engine, app, gatewayVersion)
  const checked = !!engine.checkedAt || !!app.checkedAt
  const pill = !connected ? (
    <Pill>{t('settings.about.remote.offline')}</Pill>
  ) : behind ? (
    <Pill tone="warn">{t('settings.about.outdated')}</Pill>
  ) : gatewayVersion && latest ? (
    <Pill tone="ok">{t('settings.about.upToDate')}</Pill>
  ) : null
  return (
    <Card title={t('settings.about.remote')} blurb={t('settings.about.remote.blurb')} action={pill}>
      <Row label={t('settings.about.remote.running')}>
        <Value>{gatewayVersion ?? '—'}</Value>
        {connected && uptimeMs !== undefined ? (
          <span className="stg-row__help">
            {t('settings.about.uptime').toLowerCase()} {formatUptime(uptimeMs)}
          </span>
        ) : null}
      </Row>
      <Row label={t('settings.about.remote.latest')}>
        <Value tone={behind ? 'warn' : undefined}>
          {latest ?? (checked ? '—' : t('settings.about.remote.unknown'))}
        </Value>
      </Row>
      {behind ? <Notice tone="info">{t('settings.about.remote.behind')}</Notice> : null}
    </Card>
  )
}

const ENGINE_TONE: Record<string, Tone | undefined> = {
  'up-to-date': 'ok',
  outdated: 'warn',
  offline: undefined,
}

function EngineCard({
  engine,
  gatewayVersion,
  uptimeMs,
  connected,
  onRestarted,
  confirming,
  preflight,
  onRequestUpdate,
  onCancelUpdate,
  onConfirmUpdate,
}: {
  engine: EngineUpdateState
  gatewayVersion: string | null
  uptimeMs: number | undefined
  connected: boolean
  onRestarted: () => void
  confirming: boolean
  preflight: PreflightState
  onRequestUpdate: () => void
  onCancelUpdate: () => void
  onConfirmUpdate: () => void
}) {
  const rpc = useRpc()
  const [dataCheck, setDataCheck] = useState<DataCheck | null>(null)
  const verifiedFor = useRef<string | null>(null)

  const running = engine.phase === 'installing' || engine.phase === 'restarting'
  const done = engine.phase === 'done' && engine.result
  const installed = engine.current ?? gatewayVersion
  const verified = !!done && !!gatewayVersion && gatewayVersion.split('+')[0] === engine.result?.new

  // The gateway came back on the new package: confirm with the gateway
  // itself (its handshake version, refetched here) and run the data check
  // that only the migrated databases can answer.
  useEffect(() => {
    if (!done || !engine.result?.gatewayRestarted || !connected) return
    if (verifiedFor.current === engine.result.new) return
    verifiedFor.current = engine.result.new
    onRestarted()
    rpc
      .call<DataCheck>('updates.verifyData')
      .then((check) => setDataCheck(check))
      .catch(() => setDataCheck(null))
  }, [done, engine.result, connected, rpc, onRestarted])

  const start = () => {
    setDataCheck(null)
    verifiedFor.current = null
    onConfirmUpdate()
  }

  const pill =
    engine.availability && !running ? (
      <Pill tone={ENGINE_TONE[engine.availability]}>
        {engine.availability === 'outdated'
          ? t('settings.about.outdated')
          : engine.availability === 'up-to-date'
            ? t('settings.about.upToDate')
            : t('settings.about.offline')}
      </Pill>
    ) : running ? (
      <Pill tone="warn" pulse>
        {t('settings.about.engine.updating')}
      </Pill>
    ) : null

  return (
    <Card title={t('settings.about.engine')} blurb={t('settings.about.engine.blurb')} action={pill}>
      <Row label={t('settings.about.engine.installed')}>
        <Value>{installed ?? '—'}</Value>
      </Row>
      <Row label={t('settings.about.engine.running')}>
        <Value>{gatewayVersion ?? '—'}</Value>
        {connected && uptimeMs !== undefined ? (
          <span className="stg-row__help">
            {t('settings.about.uptime').toLowerCase()} {formatUptime(uptimeMs)}
          </span>
        ) : null}
      </Row>
      <Row label={t('settings.about.engine.latest')}>
        <Value tone={engine.availability === 'outdated' ? 'warn' : undefined}>
          {engine.latest ?? (engine.checkedAt ? '—' : t('settings.about.engine.unknown'))}
        </Value>
        {engine.availability === 'outdated' && !running && !confirming ? (
          <Button variant="primary" onClick={onRequestUpdate}>
            {t('settings.about.engine.update')}
          </Button>
        ) : null}
      </Row>

      {confirming ? (
        <PreflightNotice preflight={preflight} onCancel={onCancelUpdate} onConfirm={start} />
      ) : null}

      {engine.interrupted && engine.phase !== 'error' ? (
        <Notice tone="info">{t('settings.about.engine.interrupted')}</Notice>
      ) : null}

      {running ? (
        <Notice tone="info">
          <LoaderCircle className="stg-spin size-3.5" strokeWidth={2} aria-hidden />
          {engine.phase === 'installing'
            ? t('settings.about.engine.installing')
            : t('settings.about.engine.restarting')}
        </Notice>
      ) : null}

      {engine.phase === 'error' && engine.error ? (
        <Notice tone="danger">
          <span style={{ whiteSpace: 'pre-wrap' }}>{engine.error}</span>
        </Notice>
      ) : null}
      {engine.manualCommand ? (
        <div className="stg-cmd" data-testid="engine-manual-command">
          <span className="stg-cmd__label">{t('settings.about.engine.manual')}</span>
          <code>{engine.manualCommand}</code>
        </div>
      ) : null}

      {done ? (
        <Notice tone={verified || !engine.result?.gatewayRestarted ? 'ok' : 'warn'}>
          {t('settings.about.engine.done')} <b>{engine.result?.new}</b>.{' '}
          {engine.result?.gatewayRestarted
            ? verified
              ? t('settings.about.engine.verified')
              : t('settings.about.engine.unverified')
            : t('settings.about.engine.notRestarted')}
        </Notice>
      ) : null}
      {done && dataCheck ? (
        dataCheck.ok ? (
          <Notice tone="ok">{t('settings.about.engine.dataOk')}</Notice>
        ) : (
          <Notice tone="danger">
            <span style={{ whiteSpace: 'pre-wrap' }}>
              {t('settings.about.engine.dataBad')}
              {'\n'}
              {dataCheck.problems.map((p) => `${p.path}: ${p.result}`).join('\n')}
              {dataCheck.snapshot
                ? `\n${t('settings.about.engine.restoreHint')} ${dataCheck.snapshot}`
                : ''}
            </span>
          </Notice>
        )
      ) : null}
      {done && engine.result?.snapshot ? (
        <Row label={t('settings.about.engine.snapshot')}>
          <Value title={engine.result.snapshot}>{engine.result.snapshot}</Value>
        </Row>
      ) : null}

      {engine.log.length > 0 && (running || engine.phase === 'error' || done) ? (
        <ProgressLog lines={engine.log} />
      ) : null}
    </Card>
  )
}

/**
 * The engine update's preflight, in the About card's own notice style: what
 * the gateway restart would interrupt (shared copy from the console), the
 * reason it is blocked while an approval waits, and the confirm.
 */
function PreflightNotice({
  preflight,
  onCancel,
  onConfirm,
}: {
  preflight: PreflightState
  onCancel: () => void
  onConfirm: () => void
}) {
  const openAttention = useAttentionPanel((s) => s.setOpen)
  const closeSettings = useUi((s) => s.closeSettings)
  const { preflight: result, loading, error, gate } = preflight
  const sessions = result?.runningSessions.length ?? 0
  const jobs = result?.dueJobs ?? []
  const approvals = result?.pendingApprovals ?? 0
  const warn = !!result?.warn || !!error
  const tone = gate.code === 'approvals_pending' ? 'danger' : warn ? 'warn' : 'info'
  return (
    <Notice
      tone={tone}
      action={
        <>
          {gate.code === 'approvals_pending' ? (
            <Button
              onClick={() => {
                closeSettings()
                openAttention(true)
              }}
            >
              {t('preflight.openAttention')}
            </Button>
          ) : null}
          <Button onClick={onCancel}>{t('preflight.cancel')}</Button>
          <Button
            variant={warn ? 'danger' : 'primary'}
            disabled={gate.disabled}
            title={gate.reason ?? undefined}
            data-testid="engine-preflight-confirm"
            onClick={onConfirm}
          >
            {warn ? t('preflight.updateAnyway') : t('preflight.update')}
          </Button>
        </>
      }
    >
      <span className="stg-preflight" data-testid="engine-preflight">
        <b>{t('preflight.title')}</b>
        {loading && !result ? <span>{t('settings.about.checking')}</span> : null}
        {error ? <span>{error}</span> : null}
        {result ? (
          <>
            <span>{tPlural('shell.preflightSessions', sessions)}</span>
            <span>
              {tPlural('shell.preflightJobs', jobs.length)}
              {jobs.length ? ` (${jobs.map((j) => j.name).join(', ')})` : ''}
            </span>
            <span>{tPlural('shell.preflightApprovals', approvals)}</span>
          </>
        ) : null}
        {gate.disabled && gate.reason && !loading ? (
          <span className="stg-preflight__reason">{gate.reason}</span>
        ) : null}
      </span>
    </Notice>
  )
}

function ProgressLog({ lines }: { lines: string[] }) {
  const ref = useRef<HTMLPreElement>(null)
  useEffect(() => {
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
  }, [lines.length])
  return (
    <details className="stg-log" open>
      <summary>{t('settings.about.engine.log')}</summary>
      <pre ref={ref} data-testid="engine-log">
        {lines.join('\n')}
      </pre>
    </details>
  )
}

function AppCard() {
  const app = useUpdates((s) => s.app)
  const downloadApp = useUpdates((s) => s.downloadApp)
  const installApp = useUpdates((s) => s.installApp)

  // Flag the Latest row by version, not phase, so a newer build stays flagged
  // through a re-check or a failed download, as the Engine card's does.
  const newer = !!app.latest && isNewer(app.latest, app.current)
  const tone: Tone | undefined =
    app.phase === 'up-to-date'
      ? 'ok'
      : app.phase === 'available' || app.phase === 'downloaded'
        ? 'warn'
        : app.phase === 'error'
          ? 'danger'
          : undefined
  const label =
    app.phase === 'up-to-date'
      ? t('settings.about.upToDate')
      : app.phase === 'available'
        ? t('settings.about.outdated')
        : app.phase === 'downloading'
          ? t('settings.about.app.downloading')
          : app.phase === 'downloaded'
            ? t('settings.about.app.downloaded')
            : app.phase === 'checking'
              ? t('settings.about.checking')
              : app.phase === 'error'
                ? t('settings.about.offline')
                : null

  return (
    <Card
      title={t('settings.about.appCard')}
      blurb={t('settings.about.appCard.blurb')}
      action={
        label ? (
          <Pill tone={tone} pulse={app.phase === 'downloading' || app.phase === 'checking'}>
            {label}
          </Pill>
        ) : null
      }
    >
      <Row label={t('settings.about.version')}>
        <Value>{app.current || '—'}</Value>
      </Row>
      {app.phase === 'unsupported' ? null : (
        <Row label={t('settings.about.app.latest')}>
          <Value tone={newer ? 'warn' : undefined}>
            {app.latest ?? (app.checkedAt ? '—' : t('settings.about.app.unknown'))}
          </Value>
          {app.phase === 'downloading' ? (
            <span className="stg-row__help" data-testid="app-download-percent">
              {app.percent ?? 0}%
            </span>
          ) : null}
        </Row>
      )}
      {app.phase === 'unsupported' ? (
        <Notice tone="info">{t('settings.about.app.unsupported')}</Notice>
      ) : null}
      {app.phase === 'available' ? (
        <Notice
          action={
            <Button variant="primary" onClick={() => void downloadApp()}>
              {t('settings.about.app.download')}
            </Button>
          }
        >
          <b>{app.latest}</b> {t('settings.about.app.available')}
        </Notice>
      ) : null}
      {app.phase === 'downloading' ? (
        <div className="stg-progress" role="progressbar" aria-valuenow={app.percent ?? 0}>
          <span style={{ width: `${app.percent ?? 0}%` }} />
        </div>
      ) : null}
      {app.phase === 'downloaded' ? (
        <Notice
          tone="ok"
          action={
            <Button variant="primary" onClick={() => void installApp()}>
              {t('settings.about.app.install')}
            </Button>
          }
        >
          <b>{app.latest}</b> {t('settings.about.app.downloaded')}{' '}
          {t('settings.about.app.installHint')}
        </Notice>
      ) : null}
      {app.phase === 'downloaded' && app.blocked ? (
        <Notice tone="warn">{t(`settings.about.app.blocked.${app.blocked}`)}</Notice>
      ) : null}
      {app.phase === 'error' && app.error ? (
        <Notice tone="danger">
          <span style={{ whiteSpace: 'pre-wrap' }}>{app.error}</span>
        </Notice>
      ) : null}
    </Card>
  )
}
