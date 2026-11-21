import { useEffect, useState } from 'react'
import type { AuthResult } from '@shared/app'
import type { TouchIdMode } from '@shared/settings'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'
import { useSettings } from '~/stores/settings'
import { Fingerprint, Hand, ShieldAlert } from 'lucide-react'
import { Card, Choices, Head, Notice, Pill, Row } from '../parts'

const TEST_TONE = {
  ok: 'ok',
  cancelled: 'info',
  unavailable: 'warn',
  failed: 'danger',
} as const

/**
 * Settings › Security: when the desk asks for a fingerprint. The mode is
 * always editable without Touch ID — turning it off must never need the
 * sensor it is about — and only switching it on needs a sensor present.
 */
export function SecurityPane() {
  const mode = useSettings((s) => s.settings.security.touchId)
  const update = useSettings((s) => s.update)
  // null while asking; re-asked when the window comes back to the front,
  // since closing or opening the lid changes the answer.
  const [available, setAvailable] = useState<boolean | null>(null)
  const [testing, setTesting] = useState(false)
  const [outcome, setOutcome] = useState<AuthResult | null>(null)

  useEffect(() => {
    let cancelled = false
    const check = () =>
      void desktopApi()
        .app.biometrics()
        .then(
          (info) => {
            if (!cancelled) setAvailable(info.available)
          },
          () => {
            if (!cancelled) setAvailable(false)
          },
        )
    check()
    window.addEventListener('focus', check)
    return () => {
      cancelled = true
      window.removeEventListener('focus', check)
    }
  }, [])

  async function runTest() {
    setTesting(true)
    setOutcome(null)
    try {
      setOutcome(await desktopApi().app.authenticate(t('trading.touchId.reason.test')))
    } catch {
      setOutcome({ ok: false, reason: 'failed' })
    } finally {
      setTesting(false)
    }
  }

  const unavailable = available === false
  // Without a sensor the control is locked only while it is Off: a mode
  // already on stays editable, so nobody is locked out by a closed lid.
  const locked = unavailable && mode === 'off'
  const outcomeKey = outcome ? (outcome.ok ? 'ok' : outcome.reason) : null

  return (
    <>
      <Head title={t('settings.section.security')} blurb={t('settings.section.security.blurb')} />

      <Card
        title={t('settings.security.touchId')}
        blurb={t('settings.security.touchId.blurb')}
        action={
          available === null ? null : available ? (
            <Pill tone="ok">{t('settings.security.ready')}</Pill>
          ) : (
            <Pill>{t('settings.security.noSensor')}</Pill>
          )
        }
      >
        <Choices<TouchIdMode>
          label={t('settings.security.mode')}
          value={mode}
          disabled={locked}
          columns={1}
          options={[
            {
              value: 'off',
              title: t('settings.security.mode.off'),
              blurb: t('settings.security.mode.short.off'),
              icon: <Hand strokeWidth={1.75} aria-hidden />,
            },
            {
              value: 'high',
              title: t('settings.security.mode.high'),
              blurb: t('settings.security.mode.short.high'),
              icon: <ShieldAlert strokeWidth={1.75} aria-hidden />,
            },
            {
              value: 'all',
              title: t('settings.security.mode.all'),
              blurb: t('settings.security.mode.short.all'),
              icon: <Fingerprint strokeWidth={1.75} aria-hidden />,
            },
          ]}
          onChange={(touchId) => void update({ security: { touchId } })}
        />
        <p className="stg-card__note">{t(`settings.security.mode.help.${mode}`)}</p>
        {unavailable ? (
          <Notice tone="warn">
            {mode === 'off'
              ? t('settings.security.unavailable')
              : t('settings.security.unavailable.on')}
          </Notice>
        ) : null}
        <Row label={t('settings.security.test.label')} help={t('settings.security.test.help')}>
          <Button
            onClick={() => void runTest()}
            disabled={available !== true || testing}
            aria-busy={testing || undefined}
            data-testid="touch-id-test"
          >
            {testing ? t('trading.touchId.prompting') : t('settings.security.test')}
          </Button>
        </Row>
        {outcomeKey ? (
          <Notice tone={TEST_TONE[outcomeKey]}>{t(`settings.security.test.${outcomeKey}`)}</Notice>
        ) : null}
      </Card>
    </>
  )
}
