import { useQuery } from '@tanstack/react-query'
import { useEffect, useId, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { ModalShell } from '@/components/ModalShell'
import type { AppInfo } from '@shared/app'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { desktopApi, isDesktop } from '~/lib/desktop-api'
import { useBootstrap } from '~/stores/bootstrap'
import { useGateway } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import { syncThemeFromSettings } from '~/theme/theme-store'
import { diagnosticsReport } from '../logic'
import { Card, Head, Row } from '../parts'
import { useConfigSnapshot } from '../use-snapshot'

interface StatusResult {
  version?: string
}

export function AdvancedPane() {
  const rpc = useRpc()
  const { connected, snapshot } = useConfigSnapshot()
  const settings = useSettings((s) => s.settings)
  const reset = useSettings((s) => s.reset)
  const gateway = useGateway((s) => s.status)
  const [info, setInfo] = useState<AppInfo | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [confirmingUninstall, setConfirmingUninstall] = useState(false)
  const reinstallEngine = useBootstrap((s) => s.reinstall)
  const uninstallEngine = useBootstrap((s) => s.uninstallEngine)
  const bootstrapPhase = useBootstrap((s) => s.state.phase)

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
    queryFn: () => rpc.call<StatusResult>('status'),
  })
  const configPath = snapshot?.configPath ?? null
  const desktop = isDesktop()

  async function copyDiagnostics() {
    const text = diagnosticsReport({
      info,
      gateway,
      settings,
      gatewayVersion: status.data?.version ?? null,
      configPath,
    })
    await navigator.clipboard?.writeText(text)
    toast.success(t('settings.copied'), { id: 'stg-copy' })
  }

  async function doUninstall() {
    setConfirmingUninstall(false)
    const result = await uninstallEngine()
    if (result.ok) toast.success(t('settings.advanced.uninstallDone'), { id: 'stg-engine' })
    else
      toast.error(`${t('settings.advanced.uninstallFailed')}: ${result.detail}`, {
        id: 'stg-engine',
      })
  }

  async function doReset() {
    await reset()
    await syncThemeFromSettings(useSettings.getState().settings.theme)
    setConfirming(false)
    toast.success(t('settings.advanced.resetDone'), { id: 'stg-reset' })
  }

  return (
    <>
      <Head title={t('settings.section.advanced')} blurb={t('settings.section.advanced.blurb')} />

      <Card title={t('settings.advanced.files')}>
        <Row
          label={t('settings.advanced.settingsFile')}
          help={<code className="stg-path">{info?.paths.settings ?? '…'}</code>}
        >
          <Button
            disabled={!desktop || !info}
            onClick={() => info && void desktopApi().app.showItemInFolder(info.paths.settings)}
          >
            {t('settings.reveal')}
          </Button>
        </Row>
        <Row
          label={t('settings.advanced.logs')}
          help={<code className="stg-path">{info?.paths.logs ?? '…'}</code>}
        >
          <Button
            disabled={!desktop || !info}
            onClick={() => info && void desktopApi().app.openPath(info.paths.logs)}
          >
            {t('settings.open')}
          </Button>
        </Row>
        <Row
          label={t('settings.advanced.gatewayConfig')}
          help={
            <>
              <code className="stg-path">
                {configPath ?? t('settings.advanced.gatewayConfig.unknown')}
              </code>
              <span>{t('settings.advanced.gatewayConfig.help')}</span>
            </>
          }
        >
          <Button
            disabled={!desktop || !configPath}
            onClick={() => configPath && void desktopApi().app.showItemInFolder(configPath)}
          >
            {t('settings.reveal')}
          </Button>
          <Button
            disabled={!desktop || !configPath}
            onClick={() => configPath && void desktopApi().app.openPath(configPath)}
          >
            {t('settings.open')}
          </Button>
        </Row>
      </Card>

      <Card title={t('settings.advanced.diagnostics')}>
        <Row
          label={t('settings.advanced.copyDiagnostics')}
          help={t('settings.advanced.copyDiagnostics.help')}
        >
          <Button onClick={() => void copyDiagnostics()}>{t('settings.copy')}</Button>
        </Row>
      </Card>

      <Card title={t('settings.advanced.engine')} blurb={t('settings.advanced.engine.help')}>
        <Row label={t('settings.advanced.reinstall')} help={t('settings.advanced.reinstall.help')}>
          <Button
            disabled={!desktop || bootstrapPhase === 'running'}
            onClick={() => void reinstallEngine()}
          >
            {t('settings.advanced.reinstall')}
          </Button>
        </Row>
        <Row label={t('settings.advanced.uninstall')} help={t('settings.advanced.uninstall.help')}>
          <Button
            variant="danger"
            disabled={!desktop || bootstrapPhase === 'running'}
            onClick={() => setConfirmingUninstall(true)}
          >
            {t('settings.advanced.uninstallConfirm.confirm')}
          </Button>
        </Row>
      </Card>

      <Card title={t('settings.advanced.reset')}>
        <Row label={t('settings.advanced.resetAll')} help={t('settings.advanced.resetAll.help')}>
          <Button variant="danger" onClick={() => setConfirming(true)}>
            {t('settings.advanced.resetConfirm.confirm')}
          </Button>
        </Row>
      </Card>

      {confirming ? (
        <ResetConfirm onCancel={() => setConfirming(false)} onConfirm={() => void doReset()} />
      ) : null}
      {confirmingUninstall ? (
        <UninstallConfirm
          onCancel={() => setConfirmingUninstall(false)}
          onConfirm={() => void doUninstall()}
        />
      ) : null}
    </>
  )
}

function ResetConfirm({ onCancel, onConfirm }: { onCancel: () => void; onConfirm: () => void }) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={onCancel}
      overlayClassName="stg-confirm__overlay"
      className="stg-confirm"
    >
      <h2 id={titleId}>{t('settings.advanced.resetConfirm.title')}</h2>
      <p id={bodyId}>{t('settings.advanced.resetConfirm.body')}</p>
      <div className="stg-confirm__actions">
        <Button onClick={onCancel}>{t('settings.advanced.resetConfirm.cancel')}</Button>
        <Button variant="danger" onClick={onConfirm}>
          {t('settings.advanced.resetConfirm.confirm')}
        </Button>
      </div>
    </ModalShell>
  )
}

function UninstallConfirm({
  onCancel,
  onConfirm,
}: {
  onCancel: () => void
  onConfirm: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={onCancel}
      overlayClassName="stg-confirm__overlay"
      className="stg-confirm"
    >
      <h2 id={titleId}>{t('settings.advanced.uninstallConfirm.title')}</h2>
      <p id={bodyId}>{t('settings.advanced.uninstallConfirm.body')}</p>
      <div className="stg-confirm__actions">
        <Button onClick={onCancel}>{t('settings.advanced.resetConfirm.cancel')}</Button>
        <Button variant="danger" onClick={onConfirm}>
          {t('settings.advanced.uninstallConfirm.confirm')}
        </Button>
      </div>
    </ModalShell>
  )
}
