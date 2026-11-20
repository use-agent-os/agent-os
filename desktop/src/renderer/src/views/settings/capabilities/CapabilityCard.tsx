import { useMutation } from '@tanstack/react-query'
import { ExternalLink, LoaderCircle } from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import type { ProviderSpec } from '@/views/setup/logic'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { Button } from '~/components/ui/button'
import { Switch } from '~/components/ui/switch'
import { t, type MessageKey } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'
import { jumpTo } from '../overview/jumps'
import { Card, Choices, Disclosure, Notice, Pill, type ChoiceOption, type Tone } from '../parts'
import { ProviderLogo } from '../ProviderLogo'
import { withRevision } from '../use-snapshot'
import { FieldRow, fieldLabel } from './FieldRow'
import {
  buildParams,
  draftDirty,
  enableDraft,
  fieldProblem,
  initialProvider,
  keyReady,
  keySource,
  keyUrl,
  saveProblem,
  seedDraft,
  shortLabel,
  switchProvider,
  visibleFields,
  type CapabilityDef,
  type CapDraft,
  type CapStatus,
  type KeyContext,
  type SaveProblem,
} from './logic'

export function fill(key: MessageKey, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce(
    (text, [name, value]) => text.replace(`{${name}}`, String(value)),
    t(key),
  )
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

interface ConfigureResult {
  restartRequired?: boolean
  warnings?: string[]
}

export const STATE_TONE: Record<CapStatus['state'], Tone | undefined> = {
  on: 'ok',
  off: undefined,
  key: 'warn',
}

/** One sentence per service; the catalog's own "what you need" when we have none. */
const PROVIDER_BLURB: Record<string, MessageKey> = {
  'search.brave': 'settings.capabilities.blurb.search.brave',
  'search.duckduckgo': 'settings.capabilities.blurb.search.duckduckgo',
  'search.tavily': 'settings.capabilities.blurb.search.tavily',
  'search.exa': 'settings.capabilities.blurb.search.exa',
  'search.perplexity': 'settings.capabilities.blurb.search.perplexity',
  'image.openai': 'settings.capabilities.blurb.image.openai',
  'image.openrouter': 'settings.capabilities.blurb.image.openrouter',
  'voice.elevenlabs': 'settings.capabilities.blurb.voice.elevenlabs',
}

function providerBlurb(def: CapabilityDef, spec: ProviderSpec): string {
  const key = PROVIDER_BLURB[`${def.id}.${spec.providerId}`]
  return key ? t(key) : (spec.whatYouNeed?.[0] ?? '')
}

function problemText(problem: SaveProblem, def: CapabilityDef, spec: ProviderSpec): string {
  if (problem.kind === 'key') return t('settings.capabilities.problem.key')
  if (problem.kind === 'model-prefix') {
    return fill('settings.capabilities.problem.modelPrefix', { prefix: problem.prefix })
  }
  const field = def.fields(spec).find((f) => f.name === problem.field)
  const label = field ? fieldLabel(field) : problem.field
  if (problem.kind === 'range') {
    // "Time per attempt must be between 30 and 300 seconds", not "… (seconds) must …".
    const seconds = problem.field.endsWith('_seconds')
    return fill(
      seconds
        ? 'settings.capabilities.problem.rangeSeconds'
        : 'settings.capabilities.problem.range',
      {
        field: seconds ? label.replace(/\s*\(seconds\)$/, '') : label,
        min: problem.min,
        max: problem.max,
      },
    )
  }
  return fill(
    problem.kind === 'required'
      ? 'settings.capabilities.problem.required'
      : field?.type === 'int'
        ? 'settings.capabilities.problem.whole'
        : 'settings.capabilities.problem.number',
    { field: label },
  )
}

/**
 * One capability as a card: what it does, whether it is on, which service
 * answers (choice cards, each saying whether it needs a key and whether
 * that key is already here), the service's fields with the rare ones under
 * Advanced, and Save / Revert in the foot. Saved through the capability's
 * `onboarding.*.configure` call with the snapshot revision.
 */
export function CapabilityCard({
  def,
  snapshot,
  ctx,
  envUrl,
  icon,
  title,
  blurb,
  offText,
  status,
  disabled,
  onSaved,
  aside,
}: {
  def: CapabilityDef
  snapshot: SettingsSnapshot
  ctx: KeyContext
  envUrl: (name: string) => string | undefined
  icon: ReactNode
  title: string
  blurb: string
  /** Shown instead of the form while the switch is off. */
  offText?: string
  status: CapStatus
  disabled: boolean
  onSaved: () => Promise<void>
  /** Extra lines under the key row (the X login hint). */
  aside?: ReactNode
}) {
  const rpc = useRpc()
  const saved = useMemo(() => seedDraft(def, snapshot), [def, snapshot])
  const [draft, setDraft] = useState<CapDraft>(saved)
  const [error, setError] = useState<string | null>(null)
  const specs = def.specs(snapshot)
  const spec = specs.find((s) => s.providerId === draft.providerId)
  const current = def.current(snapshot)
  const unknownSaved = Boolean(current) && !specs.some((s) => s.providerId === current)
  const on = !def.toggles || draft.enabled
  const fields = spec ? visibleFields(def.fields(spec), draft.values) : []
  const advanced = fields.filter((f) => def.advanced(f))
  const core = fields.filter((f) => !def.advanced(f))
  const problem = spec ? saveProblem(def, spec, draft, snapshot, ctx) : null
  const dirty = draftDirty(saved, draft)

  const save = useMutation({
    mutationFn: (params: Record<string, unknown>) =>
      rpc.call<ConfigureResult>(def.method, withRevision(snapshot, params)),
    onSuccess: async (res) => {
      setError(null)
      for (const w of res?.warnings ?? []) toast.warning(w)
      toast.success(
        res?.restartRequired
          ? t('settings.restartRequired')
          : t(`settings.capabilities.saved.${def.id}`),
        { id: `stg-cap-${def.id}` },
      )
      // A typed key is sent once; the form forgets it as soon as it landed.
      setDraft((d) => ({ ...d, values: { ...d.values, api_key: '' } }))
      await onSaved()
    },
    onError: (err) => {
      const message = errorText(err)
      setError(message)
      toast.error(`${t('settings.saveFailed')}: ${message}`, { id: `stg-cap-${def.id}-err` })
    },
  })

  // An edit answers the last refusal: the old message would describe a draft that is gone.
  const setValue = (name: string, value: unknown) => {
    setError(null)
    setDraft((d) => ({ ...d, values: { ...d.values, [name]: value } }))
  }

  const choiceOptions: ChoiceOption<string>[] = specs.map((s) => {
    const label = s.label || s.providerId
    const free = s.requiresApiKey !== true
    const ready = !free && keyReady(def, s, snapshot, ctx)
    return {
      value: s.providerId,
      title: label,
      blurb: providerBlurb(def, s),
      icon: <ProviderLogo id={s.providerId} label={shortLabel(label)} size={28} />,
      tag: free
        ? t('settings.capabilities.tag.free')
        : ready
          ? t('settings.capabilities.tag.keyReady')
          : t('settings.capabilities.tag.needsKey'),
      tagTone: free || ready ? 'ok' : undefined,
    }
  })

  function renderKeyRow(field: (typeof fields)[number]) {
    if (!spec) return null
    const source = keySource(def, spec, draft, snapshot, ctx)
    const envKey = String(draft.values.api_key_env || spec.envKey || '')
    const url = keyUrl(spec, envUrl)
    const help =
      source === 'env' ? (
        fill('settings.capabilities.key.env', { key: envKey })
      ) : source === 'stored' ? (
        t('settings.capabilities.key.stored')
      ) : source === 'login' ? (
        t('settings.capabilities.key.login')
      ) : source === 'provider' ? (
        t('settings.capabilities.key.provider')
      ) : source === 'missing' ? (
        <>
          <span className="cap-missing">
            {fill('settings.capabilities.key.missing', { key: envKey })}
          </span>
          <button
            type="button"
            className="prov-keylink"
            onClick={() => jumpTo('environment', envKey || undefined)}
          >
            {t('settings.capabilities.key.openEnv')}
          </button>
        </>
      ) : undefined
    const keep =
      source === 'env' || source === 'stored' || source === 'login' || source === 'provider'
    return (
      <FieldRow
        key={field.name}
        field={field}
        value={draft.values[field.name]}
        disabled={disabled || save.isPending}
        onChange={(v) => setValue(field.name, v)}
        help={
          <>
            {help ? <span>{help}</span> : null}
            {aside}
          </>
        }
        placeholder={
          keep
            ? t('settings.capabilities.key.placeholder.keep')
            : t('settings.capabilities.key.placeholder.new')
        }
        after={
          url ? (
            <button
              type="button"
              className="prov-keylink cap-getkey"
              onClick={() => void desktopApi().app.openExternal(url)}
            >
              {t('settings.capabilities.key.get')}
              <ExternalLink className="size-3" strokeWidth={2} aria-hidden />
            </button>
          ) : null
        }
      />
    )
  }

  function renderField(field: (typeof fields)[number]) {
    if (field.name === 'api_key') return renderKeyRow(field)
    const wrong = spec ? fieldProblem(field, draft.values[field.name]) : null
    return (
      <FieldRow
        key={field.name}
        field={field}
        value={draft.values[field.name]}
        disabled={disabled || save.isPending}
        onChange={(v) => setValue(field.name, v)}
        // Said at the field as soon as the value is wrong; "required" waits
        // for an edit, so an untouched card does not nag.
        error={
          wrong && spec && (wrong.kind !== 'required' || dirty)
            ? problemText(wrong, def, spec)
            : undefined
        }
        suggestions={
          field.name === 'primary' && Array.isArray(spec?.suggestedModels)
            ? (spec.suggestedModels as string[])
            : undefined
        }
      />
    )
  }

  // A field's own problem is said at the field; the foot repeats it only when
  // the field may be folded away under Advanced.
  const problemField =
    problem && 'field' in problem ? fields.find((f) => f.name === problem.field) : undefined
  const footProblem = Boolean(problem) && (!problemField || def.advanced(problemField))
  const footNote = error ? (
    <span className="stg-error">{error}</span>
  ) : dirty && problem && spec && footProblem ? (
    <span className="cap-problem">{problemText(problem, def, spec)}</span>
  ) : undefined

  return (
    <Card
      title={title}
      blurb={blurb}
      icon={<span className="cap-icon">{icon}</span>}
      action={
        <span className="cap-head-action">
          <Pill tone={STATE_TONE[status.state]}>
            {t(`settings.capabilities.state.${status.state}`)}
          </Pill>
          {def.toggles ? (
            <Switch
              checked={draft.enabled}
              disabled={disabled || save.isPending || specs.length === 0}
              aria-label={`${t('settings.capabilities.enable')}: ${title}`}
              onCheckedChange={(enabled) =>
                setDraft((d) =>
                  enabled ? enableDraft(def, snapshot, d, ctx) : { ...d, enabled: false },
                )
              }
            />
          ) : null}
        </span>
      }
      footNote={footNote}
      foot={
        // The save bar appears with something to save, not on every card at rest.
        !dirty && !save.isPending && !error ? undefined : (
          <>
            <Button
              disabled={!dirty || save.isPending}
              onClick={() => {
                setDraft(saved)
                setError(null)
              }}
            >
              {t('settings.revert')}
            </Button>
            <Button
              variant="primary"
              disabled={!dirty || !spec || Boolean(problem) || save.isPending || disabled}
              onClick={() => spec && save.mutate(buildParams(def, spec, draft))}
            >
              {save.isPending ? (
                <LoaderCircle className="stg-spin size-3.5" strokeWidth={1.75} aria-hidden />
              ) : null}
              {t('settings.save')}
            </Button>
          </>
        )
      }
    >
      {specs.length === 0 ? (
        <p className="cap-off">{t('settings.capabilities.noProviders')}</p>
      ) : !on ? (
        <p className="cap-off">{offText}</p>
      ) : (
        <>
          {unknownSaved && draft.providerId === initialProvider(def, snapshot) ? (
            <div className="cap-notice">
              <Notice tone="info">
                {fill('settings.capabilities.unknownProvider', { id: current })}
              </Notice>
            </div>
          ) : null}
          {!def.single ? (
            <div className="cap-choices">
              <Choices
                label={`${title}: ${t('settings.capabilities.provider')}`}
                value={draft.providerId}
                columns={specs.length >= 3 ? 3 : specs.length === 2 ? 2 : 1}
                disabled={disabled || save.isPending}
                options={choiceOptions}
                onChange={(next) => {
                  if (next === draft.providerId) return
                  setDraft((d) => switchProvider(def, snapshot, d, next))
                }}
              />
            </div>
          ) : null}
          {core.map(renderField)}
          {advanced.length > 0 ? (
            <Disclosure
              title={t('settings.capabilities.advanced')}
              hint={fill('settings.capabilities.advanced.hint', { n: advanced.length })}
            >
              {advanced.map(renderField)}
            </Disclosure>
          ) : null}
        </>
      )}
    </Card>
  )
}
