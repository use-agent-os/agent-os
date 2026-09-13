import { Copy, Eye, EyeOff, ExternalLink } from 'lucide-react'
import { useId, useState } from 'react'
import { toast } from 'sonner'
import type { GatewayState } from '@shared/gateway'
import type { GatewaySettings } from '@shared/settings'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { desktopApi, isDesktop } from '~/lib/desktop-api'
import { useGateway } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import {
  draftFromGateway,
  gatewayDirty,
  gatewayDraftErrors,
  gatewayFromDraft,
  gatewayNeedsRestart,
  type GatewayDraft,
} from '../logic'
import { Card, Head, Notice, Pill, Row, Segmented, Value, type Tone } from '../parts'

const TONE: Record<GatewayState, Tone | undefined> = {
  stopped: undefined,
  starting: 'warn',
  running: 'ok',
  stopping: 'warn',
  error: 'danger',
}

export function GatewayPane() {
  const saved = useSettings((s) => s.settings.gateway)
  const loaded = useSettings((s) => s.loaded)
  const update = useSettings((s) => s.update)
  const { status, busy, start, stop, restart } = useGateway()

  const needsRestart = gatewayNeedsRestart(saved, status)
  const running = status.state === 'running' || status.state === 'starting'
  const pulsing = status.state === 'starting' || status.state === 'stopping'

  function copyUrl() {
    if (!status.url) return
    void navigator.clipboard?.writeText(status.url)
    toast.success(t('settings.copied'), { id: 'stg-copy' })
  }

  return (
    <>
      <Head title={t('settings.section.gateway')} blurb={t('settings.section.gateway.blurb')} />

      {status.error ? (
        <Notice tone="danger">
          <span className="stg-value" style={{ whiteSpace: 'pre-wrap' }}>
            {status.error}
          </span>
        </Notice>
      ) : needsRestart ? (
        <Notice
          action={
            <Button disabled={busy} onClick={() => void restart()}>
              {t('settings.gateway.restartNow')}
            </Button>
          }
        >
          {t('settings.gateway.restartNeeded')}
        </Notice>
      ) : null}

      <Card
        title={t('settings.gateway.status')}
        blurb={t('settings.gateway.status.blurb')}
        action={
          <Pill tone={TONE[status.state]} pulse={pulsing}>
            {t(`gateway.state.${status.state}`)}
          </Pill>
        }
      >
        <Row label={t('settings.gateway.endpoint')}>
          <Value>{status.url ?? `http://${saved.host}:${saved.port}`}</Value>
          <Button
            variant="ghost"
            size="icon"
            disabled={!status.url}
            aria-label={t('settings.copy')}
            title={t('settings.copy')}
            onClick={copyUrl}
          >
            <Copy className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            disabled={status.state !== 'running' || !status.url}
            aria-label={t('settings.gateway.openConsole')}
            title={t('settings.gateway.openConsole')}
            onClick={() => status.url && void desktopApi().app.openExternal(status.url)}
          >
            <ExternalLink
              className="size-3.5 text-muted-foreground"
              strokeWidth={1.75}
              aria-hidden
            />
          </Button>
        </Row>
        <Row label={t('settings.gateway.pid')}>
          <Value>
            {status.state !== 'running'
              ? '—'
              : status.pid !== null
                ? `pid ${status.pid}`
                : t('settings.gateway.pid.adopted')}
          </Value>
          {running ? (
            <>
              <Button disabled={busy} onClick={() => void restart()}>
                {t('settings.gateway.restart')}
              </Button>
              <Button disabled={busy} onClick={() => void stop()}>
                {t('settings.gateway.stop')}
              </Button>
            </>
          ) : (
            <Button variant="primary" disabled={busy || !isDesktop()} onClick={() => void start()}>
              {t('settings.gateway.start')}
            </Button>
          )}
        </Row>
      </Card>

      {/* Keyed on the saved value: a save or a reset re-seeds the form, while an
          unrelated settings change (a toolbar toggle) leaves an edit alone. */}
      <ConnectionForm
        key={loaded ? JSON.stringify(saved) : 'unloaded'}
        saved={saved}
        onSave={async (next) => {
          await update({ gateway: next })
          toast.success(t('settings.gateway.saved'), { id: 'stg-gateway' })
        }}
      />
    </>
  )
}

function ConnectionForm({
  saved,
  onSave,
}: {
  saved: GatewaySettings
  onSave: (next: GatewaySettings) => Promise<void>
}) {
  const [draft, setDraft] = useState<GatewayDraft>(() => draftFromGateway(saved))
  const [showToken, setShowToken] = useState(false)
  const ids = { host: useId(), port: useId(), token: useId(), cli: useId() }

  const errors = gatewayDraftErrors(draft)
  const dirty = gatewayDirty(saved, draft)
  const canSave = dirty && !errors.host && !errors.port

  async function chooseCli() {
    const picked = await desktopApi().app.chooseFile({
      title: t('settings.gateway.cli.dialogTitle'),
      defaultPath: draft.cliPath || undefined,
    })
    if (picked) setDraft((d) => ({ ...d, cliPath: picked }))
  }

  return (
    <Card
      title={t('settings.gateway.connection')}
      blurb={t('settings.gateway.connection.blurb')}
      foot={
        <>
          <Button disabled={!dirty} onClick={() => setDraft(draftFromGateway(saved))}>
            {t('settings.revert')}
          </Button>
          <Button
            variant="primary"
            disabled={!canSave}
            onClick={() => void onSave(gatewayFromDraft(draft))}
          >
            {t('settings.save')}
          </Button>
        </>
      }
    >
      <Row label={t('settings.gateway.mode')} help={t('settings.gateway.mode.help')}>
        <Segmented
          label={t('settings.gateway.mode')}
          value={draft.mode}
          options={[
            { value: 'managed', label: t('settings.gateway.mode.managed') },
            { value: 'external', label: t('settings.gateway.mode.external') },
          ]}
          onChange={(mode) => setDraft((d) => ({ ...d, mode }))}
        />
      </Row>
      <Row
        label={t('settings.gateway.host')}
        htmlFor={ids.host}
        help={
          errors.host ? (
            <span className="stg-error">{t('settings.gateway.invalid.host')}</span>
          ) : undefined
        }
      >
        <input
          id={ids.host}
          className="mac-input"
          data-mono="true"
          data-invalid={errors.host ? 'true' : undefined}
          aria-invalid={!!errors.host}
          autoComplete="off"
          spellCheck={false}
          value={draft.host}
          onChange={(e) => setDraft((d) => ({ ...d, host: e.target.value }))}
        />
      </Row>
      <Row
        label={t('settings.gateway.port')}
        htmlFor={ids.port}
        help={
          errors.port ? (
            <span className="stg-error">{t('settings.gateway.invalid.port')}</span>
          ) : undefined
        }
      >
        <input
          id={ids.port}
          className="mac-input"
          data-mono="true"
          data-short="true"
          data-invalid={errors.port ? 'true' : undefined}
          aria-invalid={!!errors.port}
          inputMode="numeric"
          autoComplete="off"
          value={draft.port}
          onChange={(e) => setDraft((d) => ({ ...d, port: e.target.value }))}
        />
      </Row>
      <Row
        label={t('settings.gateway.token')}
        htmlFor={ids.token}
        help={t('settings.gateway.token.help')}
        align="start"
      >
        <span className="stg-input-wrap">
          <input
            id={ids.token}
            className="mac-input"
            data-mono="true"
            type={showToken ? 'text' : 'password'}
            autoComplete="off"
            spellCheck={false}
            placeholder={t('settings.gateway.token.placeholder')}
            value={draft.token}
            onChange={(e) => setDraft((d) => ({ ...d, token: e.target.value }))}
          />
          <Button
            variant="ghost"
            size="icon"
            aria-label={
              showToken ? t('settings.gateway.token.hide') : t('settings.gateway.token.show')
            }
            aria-pressed={showToken}
            onClick={() => setShowToken((v) => !v)}
          >
            {showToken ? (
              <EyeOff className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            ) : (
              <Eye className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            )}
          </Button>
        </span>
      </Row>
      <Row
        label={t('settings.gateway.cli')}
        htmlFor={ids.cli}
        help={t('settings.gateway.cli.help')}
        align="start"
      >
        <input
          id={ids.cli}
          className="mac-input"
          data-mono="true"
          autoComplete="off"
          spellCheck={false}
          placeholder={t('settings.gateway.cli.auto')}
          value={draft.cliPath}
          disabled={draft.mode === 'external'}
          onChange={(e) => setDraft((d) => ({ ...d, cliPath: e.target.value }))}
        />
        <Button
          disabled={draft.mode === 'external' || !isDesktop()}
          onClick={() => void chooseCli()}
        >
          {t('settings.gateway.cli.choose')}
        </Button>
      </Row>
    </Card>
  )
}
