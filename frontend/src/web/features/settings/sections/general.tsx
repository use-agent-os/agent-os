import { useQuery } from '@tanstack/react-query'
import { BellRing, Loader2 } from 'lucide-react'
import { useId, useState } from 'react'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSetSystem'
import { blockedBecause, gate, requireConnection } from '@/lib/disabled-reason'
import {
  browserWebPushEnv,
  disableWebPush,
  enableWebPush,
  readWebPushState,
  WebPushError,
  type RpcCall,
  type WebPushEnv,
  type WebPushState,
} from '@/services/web-push'
import { usePreferences, type SendShortcut, type TextSize } from '@/stores/preferences'
import { useTheme, type ThemePreference } from '@/stores/theme'
import { SegmentedControl } from '@/web/components/ui/segmented-control'
import { Switch } from '@/web/components/ui/switch'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useRegisterFields } from '../fields'

// General › Appearance and Notifications. Both are browser-only: a change
// applies at once and never shows the save bar.

/** A segmented single choice built on native radios (keyboard for free). */
function ChoiceRow<V extends string>({
  id,
  label,
  help,
  value,
  options,
  onChange,
}: {
  id: string
  label: string
  help?: string
  value: V
  options: readonly { value: V; label: string }[]
  onChange: (value: V) => void
}) {
  const legendId = useId()
  return (
    // A rendered <legend> sits on the fieldset's top edge, above its padding, so the
    // row's spacing lives on a wrapper or the title touches the divider above it.
    <div className="py-3">
      <fieldset data-field-id={id} className="flex flex-col gap-1">
        <legend id={legendId} className="font-medium">
          {label}
        </legend>
        {help ? <p className="text-small text-text-muted">{help}</p> : null}
        <SegmentedControl
          aria-labelledby={legendId}
          className="mt-1"
          value={value}
          onChange={onChange}
          options={options}
        />
      </fieldset>
    </div>
  )
}

function SwitchRow({
  id,
  label,
  help,
  checked,
  onChange,
}: {
  id: string
  label: string
  help: string
  checked: boolean
  onChange: (checked: boolean) => void
}) {
  const inputId = useId()
  return (
    <div data-field-id={id} className="flex items-start justify-between gap-4 py-3">
      <div className="min-w-0">
        <label htmlFor={inputId} className="font-medium">
          {label}
        </label>
        <p id={`${inputId}-help`} className="text-small text-text-muted">
          {help}
        </p>
      </div>
      <Switch
        id={inputId}
        aria-describedby={`${inputId}-help`}
        checked={checked}
        onCheckedChange={onChange}
      />
    </div>
  )
}

export function AppearanceSection() {
  const theme = useTheme((s) => s.preference)
  const setTheme = useTheme((s) => s.setPreference)
  const prefs = usePreferences()

  useRegisterFields('appearance', [
    { id: 'theme', label: t('webSetSystem.theme') },
    { id: 'textSize', label: t('webSetSystem.textSize') },
    { id: 'sendShortcut', label: t('webSetSystem.sendShortcut') },
    { id: 'showThinking', label: t('webSetSystem.showThinking') },
    { id: 'visualEffects', label: t('webSetSystem.visualEffects') },
  ])

  return (
    <div className="flex flex-col divide-y divide-border">
      <p className="pb-3 text-small text-text-muted">{t('webSetSystem.appearanceLocal')}</p>
      <ChoiceRow<ThemePreference>
        id="theme"
        label={t('webSetSystem.theme')}
        value={theme}
        onChange={setTheme}
        options={[
          { value: 'system', label: t('webSetSystem.themeSystem') },
          { value: 'light', label: t('webSetSystem.themeLight') },
          { value: 'dark', label: t('webSetSystem.themeDark') },
        ]}
      />
      <ChoiceRow<TextSize>
        id="textSize"
        label={t('webSetSystem.textSize')}
        value={prefs.textSize}
        onChange={(v) => prefs.setPreference('textSize', v)}
        options={[
          { value: 'small', label: t('webSetSystem.textSmall') },
          { value: 'default', label: t('webSetSystem.textDefault') },
          { value: 'large', label: t('webSetSystem.textLarge') },
        ]}
      />
      <ChoiceRow<SendShortcut>
        id="sendShortcut"
        label={t('webSetSystem.sendShortcut')}
        help={t('webSetSystem.sendShortcutHelp')}
        value={prefs.sendShortcut}
        onChange={(v) => prefs.setPreference('sendShortcut', v)}
        options={[
          { value: 'enter', label: t('webSetSystem.sendEnter') },
          { value: 'mod-enter', label: t('webSetSystem.sendModEnter') },
        ]}
      />
      <SwitchRow
        id="showThinking"
        label={t('webSetSystem.showThinking')}
        help={t('webSetSystem.showThinkingHelp')}
        checked={prefs.showThinking}
        onChange={(v) => prefs.setPreference('showThinking', v)}
      />
      <SwitchRow
        id="visualEffects"
        label={t('webSetSystem.visualEffects')}
        help={t('webSetSystem.visualEffectsHelp')}
        checked={prefs.visualEffects}
        onChange={(v) => prefs.setPreference('visualEffects', v)}
      />
    </div>
  )
}

const WEB_PUSH_QUERY_KEY = ['web-push', 'state'] as const

function pushFailure(error: unknown): string {
  if (error instanceof WebPushError) {
    if (error.reason === 'denied') return t('webSetSystem.pushDenied')
    if (error.reason === 'disabled') return t('webSetSystem.pushGatewayOff')
    return t('webSetSystem.pushUnsupported')
  }
  return t('webSetSystem.pushFailed', {
    message: error instanceof Error ? error.message : String(error),
  })
}

/** Why the switch cannot be flipped, or null when it can. */
function pushBlocked(state: WebPushState): string | null {
  if (state.support === 'insecure') return t('webSetSystem.pushInsecure')
  if (state.support === 'unsupported') return t('webSetSystem.pushUnsupported')
  // An existing subscription can always be turned off.
  if (state.subscribed) return null
  if (state.gatewayEnabled === null) return t('webSetSystem.pushGatewayMissing')
  if (!state.gatewayEnabled) return t('webSetSystem.pushGatewayOff')
  if (state.permission === 'denied') return t('webSetSystem.pushDenied')
  return null
}

/** Web Push for approval prompts in this browser (`services/web-push`). */
export function NotificationsSection({ env: envOverride }: { env?: WebPushEnv } = {}) {
  const rpc = useRpc()
  const connection = useDisplayedConnection()
  const [env] = useState<WebPushEnv>(() => envOverride ?? browserWebPushEnv())
  const [call] = useState(() => {
    const callRpc: RpcCall = async (method, params) => {
      await rpc.waitForConnection()
      return rpc.call(method, params)
    }
    return callRpc
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const inputId = useId()

  useRegisterFields('notifications', [{ id: 'webPush', label: t('webSetSystem.pushTitle') }])

  const query = useQuery<WebPushState>({
    queryKey: WEB_PUSH_QUERY_KEY,
    queryFn: () => readWebPushState(env, call),
    retry: false,
    // Permission can change in the browser's own settings meanwhile.
    refetchOnWindowFocus: true,
  })
  const state = query.data
  const subscribed = state?.subscribed === true
  const blocked = state ? pushBlocked(state) : null
  const toggle = gate(requireConnection(connection), blocked ? blockedBecause(blocked) : null)

  const note = !state
    ? query.isError
      ? pushFailure(query.error)
      : t('webSetSystem.pushChecking')
    : subscribed
      ? t('webSetSystem.pushOn')
      : t('webSetSystem.pushOff')

  const onToggle = async () => {
    if (!state || busy) return
    setBusy(true)
    setError('')
    try {
      // Straight from the click: the permission prompt needs the gesture.
      if (subscribed) await disableWebPush(env, call)
      else await enableWebPush(env, call)
    } catch (err) {
      setError(pushFailure(err))
    } finally {
      setBusy(false)
      void query.refetch()
    }
  }

  return (
    <div data-field-id="webPush" className="flex flex-col gap-2 py-3">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 gap-3">
          <BellRing className="mt-0.5 size-5 shrink-0 text-text-muted" aria-hidden />
          <div className="min-w-0">
            <label htmlFor={inputId} className="font-medium">
              {t('webSetSystem.pushTitle')}
            </label>
            <p id={`${inputId}-note`} className="text-small text-text-muted">
              {note}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {busy ? <Loader2 className="size-4 animate-spin text-text-muted" aria-hidden /> : null}
          <Switch
            id={inputId}
            aria-describedby={`${inputId}-note`}
            aria-busy={busy || undefined}
            checked={subscribed}
            disabled={busy || !state || toggle.disabled}
            onCheckedChange={() => void onToggle()}
          />
        </div>
      </div>
      {!busy && toggle.reason ? (
        <p className="text-small text-text-muted">{toggle.reason}</p>
      ) : null}
      {error ? (
        <p role="alert" className="text-small text-danger">
          {error}
        </p>
      ) : null}
    </div>
  )
}
