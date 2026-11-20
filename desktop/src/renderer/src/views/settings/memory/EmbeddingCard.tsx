import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Cloud,
  Cpu,
  Eye,
  EyeOff,
  KeyRound,
  Laptop,
  Server,
  Sparkles,
  TextSearch,
} from 'lucide-react'
import { useId, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import type { ProviderSpec } from '@/views/setup/logic'
import { Button } from '~/components/ui/button'
import { t, type MessageKey } from '~/i18n'
import { useSettings } from '~/stores/settings'
import { jumpTo } from '../overview/jumps'
import { Card, Choices, Disclosure, Notice, Row, type ChoiceOption, type Tone } from '../parts'
import { withRevision } from '../use-snapshot'
import { reportSaveError, useEnvSet } from './api'
import { useDraft } from './drafts'
import {
  EMBEDDING_DEFAULTS,
  EMBEDDING_PROVIDERS,
  embeddingDirty,
  embeddingFields,
  embeddingProblem,
  embeddingSpecs,
  embeddingWrite,
  hasStoredEmbeddingKey,
  isLoopbackHost,
  ollamaHasModel,
  retrievalIsKeywordOnly,
  savedEmbeddingKeyEnv,
  savedEmbeddingProvider,
  seedEmbedding,
  urlHost,
  type EmbeddingDraft,
  type EmbeddingField,
  type EmbeddingProblem,
  type EmbeddingProvider,
  type EmbeddingWrite,
} from './logic'
import { Spinner, fill } from './ui'

interface ConfigureResult {
  changed?: boolean
  restartRequired?: boolean
  warnings?: string[]
}

const CHOICE: Record<
  EmbeddingProvider,
  { title: MessageKey; blurb: MessageKey; tag?: MessageKey; tone?: Tone; icon: ReactNode }
> = {
  auto: {
    title: 'settings.memory.embed.auto',
    blurb: 'settings.memory.embed.auto.blurb',
    tag: 'settings.memory.embed.tag.recommended',
    tone: 'primary',
    icon: <Sparkles />,
  },
  local: {
    title: 'settings.memory.embed.local',
    blurb: 'settings.memory.embed.local.blurb',
    tag: 'settings.memory.embed.tag.private',
    tone: 'ok',
    icon: <Laptop />,
  },
  ollama: {
    title: 'settings.memory.embed.ollama',
    blurb: 'settings.memory.embed.ollama.blurb',
    tag: 'settings.memory.embed.tag.local',
    tone: 'ok',
    icon: <Cpu />,
  },
  openai: {
    title: 'settings.memory.embed.openai',
    blurb: 'settings.memory.embed.openai.blurb',
    tag: 'settings.memory.embed.tag.key',
    tone: 'warn',
    icon: <Cloud />,
  },
  'openai-compatible': {
    title: 'settings.memory.embed.compatible',
    blurb: 'settings.memory.embed.compatible.blurb',
    tag: 'settings.memory.embed.tag.key',
    tone: 'warn',
    icon: <Server />,
  },
  none: {
    title: 'settings.memory.embed.none',
    blurb: 'settings.memory.embed.none.blurb',
    icon: <TextSearch />,
  },
}

/** The engine's plain name, for the stats strip. */
export function embeddingTitle(provider: EmbeddingProvider): string {
  return t(CHOICE[provider].title)
}

const FIELD_LABEL: Record<EmbeddingField, MessageKey> = {
  model: 'settings.memory.embed.model',
  baseUrl: 'settings.memory.embed.baseUrl',
  apiKey: 'settings.memory.embed.apiKey',
  apiKeyEnv: 'settings.memory.embed.apiKeyEnv',
  onnxDir: 'settings.memory.embed.onnxDir',
}

const FIELD_HELP: Partial<Record<EmbeddingField, MessageKey>> = {
  apiKeyEnv: 'settings.memory.embed.apiKeyEnv.help',
  onnxDir: 'settings.memory.embed.onnxDir.help',
}

/**
 * "How memory is searched": one engine from the onboarding catalog, the
 * fields it needs, saved through `onboarding.memory_embedding.configure`
 * with the snapshot revision. The engine is loaded at boot, so a change
 * waits for a gateway restart.
 */
export function EmbeddingCard({
  snapshot,
  reload,
  onRebuild,
}: {
  snapshot: SettingsSnapshot
  reload: () => Promise<void>
  onRebuild: () => void
}) {
  const rpc = useRpc()
  const specs = embeddingSpecs(snapshot)
  const specFor = (id: EmbeddingProvider): ProviderSpec | undefined =>
    specs.find((s) => s.providerId === id)
  const savedProvider = savedEmbeddingProvider(snapshot)
  const saved = seedEmbedding(snapshot, savedProvider, specFor(savedProvider))
  const { draft, setDraft, revert, settle } = useDraft<EmbeddingDraft>('embedding', saved)
  const storedKey = hasStoredEmbeddingKey(snapshot)
  const envSet = useEnvSet()
  const [justSaved, setJustSaved] = useState(false)
  const blocked = Boolean(snapshot.writeBlocked)

  const ids = (specs.length > 0 ? specs.map((s) => s.providerId) : EMBEDDING_PROVIDERS).filter(
    (id): id is EmbeddingProvider => id in CHOICE,
  )
  const options: ChoiceOption<EmbeddingProvider>[] = ids.map((id) => ({
    value: id,
    title: t(CHOICE[id].title),
    blurb: t(CHOICE[id].blurb),
    tag: CHOICE[id].tag ? t(CHOICE[id].tag!) : undefined,
    tagTone: CHOICE[id].tone,
    icon: CHOICE[id].icon,
  }))

  const save = useMutation({
    mutationFn: (write: EmbeddingWrite) =>
      rpc.call<ConfigureResult>(write.method, withRevision(snapshot, write.params)),
    onSuccess: async (res) => {
      for (const w of res?.warnings ?? []) toast.warning(w)
      if (res?.changed === false) {
        toast.success(t('settings.memory.embed.unchanged'), { id: 'stg-memory-embed' })
        revert()
      } else {
        settle()
        toast.success(
          res?.restartRequired
            ? t('settings.memory.embed.saved.restart')
            : t('settings.memory.embed.saved'),
          { id: 'stg-memory-embed' },
        )
        setJustSaved(true)
      }
      await reload()
    },
    onError: (err) => reportSaveError(err, reload, 'stg-memory-embed-err'),
  })

  const dirty = embeddingDirty(saved, draft)
  const problem = embeddingProblem(draft, {
    storedKey,
    savedEnv: savedEmbeddingKeyEnv(snapshot),
    envSet,
  })
  const canSave = dirty && !problem && !save.isPending && !blocked
  const { main, advanced } = embeddingFields(draft.provider)
  const auto = draft.provider === 'auto'
  const fallbackUrl = draft.baseUrl.trim() || EMBEDDING_DEFAULTS.auto.baseUrl!
  const fallbackEnv = draft.apiKey.trim()
    ? ''
    : draft.apiKeyEnv.trim() || (storedKey ? '' : savedEmbeddingKeyEnv(snapshot))
  const fallbackHint = !draft.fallback
    ? t('settings.memory.embed.fallback.off')
    : fallbackEnv
      ? fill('settings.memory.embed.fallback.on', { name: fallbackEnv })
      : t('settings.memory.embed.fallback.onStored')

  function choose(provider: EmbeddingProvider) {
    setJustSaved(false)
    setDraft(
      provider === savedProvider ? saved : seedEmbedding(snapshot, provider, specFor(provider)),
    )
  }

  const field = (name: EmbeddingField) => (
    <EmbeddingFieldRow
      key={name}
      name={name}
      draft={draft}
      storedKey={storedKey}
      disabled={blocked || save.isPending}
      onChange={(value) => setDraft((d) => ({ ...d, [name]: value }))}
    />
  )

  return (
    <Card
      title={t('settings.memory.embed.title')}
      blurb={t('settings.memory.embed.blurb')}
      footNote={
        problem ? (
          <span className="mem-problem">{problemText(problem)}</span>
        ) : dirty ? (
          t('settings.memory.embed.foot.restart')
        ) : undefined
      }
      foot={
        <>
          <Button disabled={!dirty || save.isPending} onClick={revert}>
            {t('settings.revert')}
          </Button>
          <Button
            variant="primary"
            disabled={!canSave}
            onClick={() => save.mutate(embeddingWrite(draft))}
          >
            {save.isPending ? t('settings.memory.saving') : t('settings.save')}
          </Button>
        </>
      }
    >
      {retrievalIsKeywordOnly(snapshot) ? (
        <div className="mem-card-notice">
          <KeywordOnlyNotice snapshot={snapshot} reload={reload} />
        </div>
      ) : null}
      <Choices
        label={t('settings.memory.embed.choices')}
        value={draft.provider}
        options={options}
        columns={2}
        disabled={blocked || save.isPending}
        onChange={choose}
      />
      {draft.provider === 'ollama' ? (
        <OllamaCheck baseUrl={draft.baseUrl} model={draft.model} />
      ) : null}
      {main.map(field)}
      {auto ? (
        <Disclosure title={t('settings.memory.embed.fallback')} hint={fallbackHint}>
          <div className="mem-card-notice">
            {draft.fallback ? (
              <Notice
                tone="info"
                action={
                  <Button
                    disabled={blocked || save.isPending}
                    onClick={() =>
                      setDraft((d) => ({
                        ...d,
                        model: '',
                        baseUrl: '',
                        apiKey: '',
                        apiKeyEnv: '',
                        fallback: false,
                      }))
                    }
                  >
                    {t('settings.memory.embed.fallback.remove')}
                  </Button>
                }
              >
                {fill('settings.memory.embed.fallback.onBody', { url: fallbackUrl })}
              </Notice>
            ) : (
              <Notice
                tone="info"
                action={
                  <Button
                    disabled={blocked || save.isPending}
                    onClick={() => setDraft((d) => ({ ...d, fallback: true }))}
                  >
                    {t('settings.memory.embed.fallback.add')}
                  </Button>
                }
              >
                {saved.fallback
                  ? t('settings.memory.embed.fallback.removed')
                  : t('settings.memory.embed.fallback.offBody')}
              </Notice>
            )}
          </div>
          {draft.fallback ? advanced.map(field) : null}
        </Disclosure>
      ) : advanced.length > 0 ? (
        <Disclosure
          title={t('settings.memory.advanced')}
          hint={advanced.map((f) => t(FIELD_LABEL[f])).join(', ')}
        >
          {advanced.map(field)}
        </Disclosure>
      ) : null}
      {problem?.kind === 'envUnset' ? (
        <div className="mem-card-notice">
          <Notice
            tone="warn"
            action={
              <Button onClick={() => jumpTo('environment')}>
                {t('settings.memory.embed.openEnv')}
              </Button>
            }
          >
            {fill('settings.memory.embed.envUnset', { name: problem.name })}
          </Notice>
        </div>
      ) : null}
      {justSaved && !dirty ? (
        <div className="mem-card-notice">
          <Notice
            tone="ok"
            action={
              <Button onClick={onRebuild}>{t('settings.memory.maint.rebuild.button')}</Button>
            }
          >
            {t('settings.memory.embed.afterSave')}
          </Notice>
        </div>
      ) : null}
    </Card>
  )
}

function problemText(problem: NonNullable<EmbeddingProblem>): string {
  switch (problem.kind) {
    case 'needsKey':
      return t('settings.memory.embed.needsKey')
    case 'needsUrl':
      return t('settings.memory.embed.needsUrl')
    case 'envUnset':
      return t('settings.memory.embed.envUnset.short')
  }
}

function EmbeddingFieldRow({
  name,
  draft,
  storedKey,
  disabled,
  onChange,
}: {
  name: EmbeddingField
  draft: EmbeddingDraft
  storedKey: boolean
  disabled: boolean
  onChange: (value: string) => void
}) {
  const id = useId()
  const [show, setShow] = useState(false)
  const placeholder =
    name === 'apiKey'
      ? storedKey
        ? t('settings.memory.embed.apiKey.saved')
        : t('settings.memory.embed.apiKey.placeholder')
      : name === 'onnxDir'
        ? t('settings.memory.embed.onnxDir.placeholder')
        : (EMBEDDING_DEFAULTS[draft.provider][name] ?? '')
  const help =
    name === 'model' && EMBEDDING_DEFAULTS[draft.provider].model
      ? fill('settings.memory.embed.model.help', {
          model: EMBEDDING_DEFAULTS[draft.provider].model!,
        })
      : name === 'baseUrl' && draft.provider === 'ollama'
        ? t('settings.memory.embed.baseUrl.ollama')
        : name === 'baseUrl' && draft.provider === 'openai-compatible'
          ? t('settings.memory.embed.baseUrl.compatible')
          : name === 'apiKey'
            ? t('settings.memory.embed.apiKey.help')
            : FIELD_HELP[name]
              ? t(FIELD_HELP[name]!)
              : undefined

  if (name === 'apiKey') {
    return (
      <Row label={t(FIELD_LABEL[name])} help={help} htmlFor={id}>
        <span className="stg-input-wrap">
          <input
            id={id}
            className="mac-input"
            data-mono="true"
            type={show ? 'text' : 'password'}
            autoComplete="off"
            spellCheck={false}
            placeholder={placeholder}
            value={draft.apiKey}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value)}
          />
          <Button
            variant="ghost"
            size="icon"
            aria-label={
              show ? t('settings.memory.embed.hideKey') : t('settings.memory.embed.showKey')
            }
            aria-pressed={show}
            onClick={() => setShow((v) => !v)}
          >
            {show ? (
              <EyeOff className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            ) : (
              <Eye className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            )}
          </Button>
        </span>
      </Row>
    )
  }
  return (
    <Row label={t(FIELD_LABEL[name])} help={help} htmlFor={id}>
      <span className="mem-field">
        {name === 'apiKeyEnv' ? (
          <KeyRound className="mem-field__icon" strokeWidth={1.75} aria-hidden />
        ) : null}
        <input
          id={id}
          className="mac-input"
          data-mono={name === 'model' ? undefined : 'true'}
          autoComplete="off"
          spellCheck={false}
          placeholder={placeholder}
          value={draft[name]}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        />
      </span>
    </Row>
  )
}

type OllamaProbe = { state: 'up'; models: string[] | null } | { state: 'down' }

async function probeOllama(url: string): Promise<OllamaProbe> {
  try {
    const res = await fetch(`${url}/api/tags`, { signal: AbortSignal.timeout(2500) })
    if (!res.ok) return { state: 'up', models: null }
    const body = (await res.json()) as { models?: { name?: string }[] }
    return { state: 'up', models: (body.models ?? []).map((m) => String(m.name ?? '')) }
  } catch {
    // A refused CORS read still means something answered: ask again without reading.
    try {
      await fetch(`${url}/api/tags`, { mode: 'no-cors', signal: AbortSignal.timeout(2500) })
      return { state: 'up', models: null }
    } catch {
      return { state: 'down' }
    }
  }
}

/**
 * Is Ollama there? Only answerable from the app when the gateway runs on this
 * Mac; otherwise it has to be reachable from the gateway's machine.
 */
function OllamaCheck({ baseUrl, model }: { baseUrl: string; model: string }) {
  const gatewayHost = useSettings((s) => s.settings.gateway.host)
  const url = (baseUrl.trim() || EMBEDDING_DEFAULTS.ollama.baseUrl!).replace(/\/+$/, '')
  const wanted = model.trim() || EMBEDDING_DEFAULTS.ollama.model!
  const canProbe = isLoopbackHost(gatewayHost) && isLoopbackHost(urlHost(url))
  const probe = useQuery({
    queryKey: ['settings', 'memory', 'ollama', url],
    enabled: canProbe,
    retry: false,
    staleTime: 0,
    queryFn: () => probeOllama(url),
  })
  const again = (
    <Button disabled={probe.isFetching} onClick={() => void probe.refetch()}>
      {t('settings.memory.ollama.again')}
    </Button>
  )
  let body: ReactNode
  if (!canProbe) {
    body = <Notice tone="info">{fill('settings.memory.ollama.remote', { url })}</Notice>
  } else if (probe.isPending || (probe.isFetching && !probe.data)) {
    body = <Spinner label={fill('settings.memory.ollama.checking', { url })} />
  } else if (!probe.data || probe.data.state === 'down') {
    body = (
      <Notice tone="warn" action={again}>
        {fill('settings.memory.ollama.down', { url })}
      </Notice>
    )
  } else if (probe.data.models && !ollamaHasModel(probe.data.models, wanted)) {
    body = (
      <Notice tone="warn" action={again}>
        {fill('settings.memory.ollama.noModel', { model: wanted })}
      </Notice>
    )
  } else {
    body = (
      <Notice tone="ok">
        {probe.data.models
          ? fill('settings.memory.ollama.ready', { model: wanted })
          : t('settings.memory.ollama.up')}
      </Notice>
    )
  }
  return (
    <div className="mem-card-notice" data-testid="ollama-check">
      {body}
    </div>
  )
}

/** `memory.retrieval_mode = "fts_only"` overrides every engine: say so, offer the way back. */
function KeywordOnlyNotice({
  snapshot,
  reload,
}: {
  snapshot: SettingsSnapshot
  reload: () => Promise<void>
}) {
  const rpc = useRpc()
  const fix = useMutation({
    mutationFn: () =>
      rpc.call<ConfigureResult>(
        'config.patch',
        withRevision(snapshot, { patches: { 'memory.retrieval_mode': 'hybrid' } }),
      ),
    onSuccess: async () => {
      toast.success(t('settings.memory.embed.ftsOnly.done'), { id: 'stg-memory-embed' })
      await reload()
    },
    onError: (err) => reportSaveError(err, reload, 'stg-memory-embed-err'),
  })
  return (
    <Notice
      tone="warn"
      action={
        <Button
          disabled={fix.isPending || Boolean(snapshot.writeBlocked)}
          onClick={() => fix.mutate()}
        >
          {t('settings.memory.embed.ftsOnly.fix')}
        </Button>
      }
    >
      {t('settings.memory.embed.ftsOnly')} <code className="mem-key">memory.retrieval_mode</code>
    </Notice>
  )
}
