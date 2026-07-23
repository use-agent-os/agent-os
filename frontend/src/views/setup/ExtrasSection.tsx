// Capabilities section (setup.js:716-957). Five capability cards — web search,
// memory embedding, memory settings, image generation, voice audio — each with
// its own draft state, conditional field enablement (from logic.ts), masked
// secrets, and a Save wired to the matching onboarding.*.configure RPC (memory
// settings uses config.patch). All decision-shaped derivation lives in logic.ts.
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  CapabilityBadge,
  EnvRecoveryCommand,
  NeedList,
  PanelHead,
  SetupCheckbox,
  SetupSelect,
} from './parts'
import {
  audioStatusText,
  buildAudioConfigureParams,
  buildImageConfigureParams,
  buildMemoryConfigureParams,
  buildMemorySettingsPatches,
  buildSearchConfigureParams,
  capabilityIsPrimary,
  credentialNeedList,
  envRecoveryCommand,
  imageGenerationStatusText,
  memoryControlFlags,
  memoryEmbeddingStatusText,
  memoryNeedList,
  memorySettingsOverBudget,
  searchStatusText,
  type CapabilityField,
  type Catalog,
  type OnboardingStatus,
  type ProviderSpec,
  type SetupConfig,
} from './logic'

type ExtrasResetTarget = 'search' | 'memoryEmbedding' | 'memorySettings' | 'image' | 'audio'

function saveVariant(status: OnboardingStatus, name: string): 'default' | 'outline' {
  return capabilityIsPrimary(status, name) ? 'default' : 'outline'
}

// ── Web search (setup.js:716-851) ───────────────────────────────────────────
function SearchCard({
  catalog,
  status,
  config,
  onSave,
  saving,
}: {
  catalog: Catalog
  status: OnboardingStatus
  config: SetupConfig
  onSave: (params: Record<string, unknown>) => void
  saving: boolean
}) {
  const providers = (catalog.searchProviders || []).filter((p) => p.runtimeSupported)
  const initial =
    config.search_provider ||
    providers.find((p) => p.providerId === 'duckduckgo')?.providerId ||
    providers[0]?.providerId ||
    'duckduckgo'
  const [provider, setProvider] = useState(initial)
  const spec: ProviderSpec = providers.find((p) => p.providerId === provider) ||
    providers[0] || {
      providerId: provider,
    }
  const requiresKey = spec.requiresApiKey === true

  const [maxResults, setMaxResults] = useState(String(config.search_max_results || 5))
  const [apiKey, setApiKey] = useState('')
  const [apiKeyEnv, setApiKeyEnv] = useState(
    config.search_api_key_env || (requiresKey ? spec.envKey || '' : '') || '',
  )
  // Re-seed the env-var name to the new provider's envKey on a provider switch,
  // unless the user has typed their own — legacy _syncSearchProviderKeyControls
  // did `envInput.value = spec.envKey || ''` on every change (setup.js:1551-1555;
  // '' when the provider needs no key). Without a touch flag, switching to Brave
  // without typing would save api_key_env:'' instead of 'BRAVE_API_KEY'.
  const [envTouched, setEnvTouched] = useState(false)
  const [envProviderKey, setEnvProviderKey] = useState(provider)
  if (envProviderKey !== provider) {
    setEnvProviderKey(provider)
    if (!envTouched) setApiKeyEnv(requiresKey ? spec.envKey || '' : '')
  }
  const [proxy, setProxy] = useState(config.search_proxy || '')
  const [useEnvProxy, setUseEnvProxy] = useState(config.search_use_env_proxy === true)
  const [fallback, setFallback] = useState(config.search_fallback_policy || 'off')
  const [diagnostics, setDiagnostics] = useState(config.search_diagnostics === true)

  const collect = () => {
    const fields: CapabilityField[] = [
      {
        name: 'max_results',
        value: maxResults,
        checked: false,
        type: 'number',
        secret: false,
        disabled: false,
      },
      {
        name: 'api_key',
        value: apiKey,
        checked: false,
        type: 'password',
        secret: true,
        disabled: !requiresKey,
      },
      {
        name: 'api_key_env',
        value: apiKeyEnv,
        checked: false,
        type: 'text',
        secret: false,
        disabled: !requiresKey,
      },
      { name: 'proxy', value: proxy, checked: false, type: 'text', secret: false, disabled: false },
      {
        name: 'use_env_proxy',
        value: '',
        checked: useEnvProxy,
        type: 'checkbox',
        secret: false,
        disabled: false,
      },
      {
        name: 'fallback_policy',
        value: fallback,
        checked: false,
        type: 'text',
        secret: false,
        disabled: false,
      },
      {
        name: 'diagnostics',
        value: '',
        checked: diagnostics,
        type: 'checkbox',
        secret: false,
        disabled: false,
      },
    ].filter((f) => !f.disabled)
    onSave(buildSearchConfigureParams(provider, fields))
  }

  return (
    <div className="setup-mini panel">
      <div className="setup-mini__head">
        <h3 className="t-label">Web search</h3>
        <CapabilityBadge status={status} name="search" />
      </div>
      <p className="setup-muted">{searchStatusText(status, config)}</p>
      <EnvRecoveryCommand command={envRecoveryCommand(status, 'search')} />
      <NeedList
        items={credentialNeedList(spec.whatYouNeed, apiKeyEnv || spec.envKey)}
        label="Search needs"
      />
      <label>
        <span>Provider</span>
        <SetupSelect
          aria-label="Search provider"
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
        >
          {providers.map((p) => (
            <option key={p.providerId} value={p.providerId}>
              {p.label}
            </option>
          ))}
        </SetupSelect>
      </label>
      <label>
        <span>Max results</span>
        <input
          type="number"
          min={1}
          step={1}
          aria-label="Search max results"
          value={maxResults}
          onChange={(e) => setMaxResults(e.target.value)}
        />
      </label>
      {requiresKey ? (
        <div className="setup-advanced__body">
          <label>
            <span>API key</span>
            <input
              type="password"
              aria-label="Search API key"
              placeholder="leave blank to keep current"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </label>
          <label>
            <span>API key env</span>
            <input
              aria-label="Search API key env"
              value={apiKeyEnv}
              placeholder={spec.envKey || 'SEARCH_API_KEY'}
              onChange={(e) => {
                setEnvTouched(true)
                setApiKeyEnv(e.target.value)
              }}
            />
          </label>
        </div>
      ) : null}
      <details
        className="setup-advanced"
        open={Boolean(proxy || useEnvProxy || fallback !== 'off' || diagnostics)}
      >
        <summary>Advanced search options</summary>
        <div className="setup-advanced__body" aria-label="Search behavior">
          <label>
            <span>HTTP proxy</span>
            <input
              aria-label="Search proxy"
              placeholder="http://127.0.0.1:7890"
              value={proxy}
              onChange={(e) => setProxy(e.target.value)}
            />
          </label>
          <SetupCheckbox
            ariaLabel="Use environment proxy"
            checked={useEnvProxy}
            onChange={setUseEnvProxy}
          >
            Use environment proxy
          </SetupCheckbox>
          <label>
            <span>Fallback policy</span>
            <SetupSelect
              aria-label="Search fallback policy"
              value={fallback}
              onChange={(e) => setFallback(e.target.value)}
            >
              <option value="off">Off</option>
              <option value="network">Network retry</option>
            </SetupSelect>
          </label>
          <SetupCheckbox
            ariaLabel="Search diagnostics"
            checked={diagnostics}
            onChange={setDiagnostics}
          >
            Diagnostics
          </SetupCheckbox>
        </div>
      </details>
      <Button
        type="button"
        variant={saveVariant(status, 'search')}
        disabled={saving}
        onClick={collect}
      >
        Save web search
      </Button>
    </div>
  )
}

// ── Memory embedding (setup.js:732-876) ─────────────────────────────────────
function MemoryEmbeddingCard({
  catalog,
  status,
  config,
  onSave,
  saving,
}: {
  catalog: Catalog
  status: OnboardingStatus
  config: SetupConfig
  onSave: (params: Record<string, unknown>) => void
  saving: boolean
}) {
  const providers = catalog.memoryEmbeddingProviders || []
  const current = (config.memory || {}).embedding || {}
  const initial = current.provider || current.mode || 'auto'
  const [provider, setProvider] = useState(initial)
  const spec: ProviderSpec = providers.find((p) => p.providerId === provider) ||
    providers[0] || {
      providerId: provider,
    }
  const flags = memoryControlFlags(provider, spec)

  const remote = (current.remote || {}) as Record<string, string>
  const local = (current.local || {}) as Record<string, string>
  const ollama = (current.ollama || {}) as Record<string, string>

  const [model, setModel] = useState(remote.model || ollama.model || '')
  const [apiKey, setApiKey] = useState('')
  const [apiKeyEnv, setApiKeyEnv] = useState(
    remote.api_key_env || (flags.apiKeyEnabled ? spec.envKey || '' : '') || '',
  )
  const [baseUrl, setBaseUrl] = useState(remote.base_url || ollama.base_url || '')
  const [onnxDir, setOnnxDir] = useState(local.onnx_dir || '')

  const collect = () => {
    const fields: CapabilityField[] = [
      {
        name: 'model',
        value: model,
        checked: false,
        type: 'text',
        secret: false,
        disabled: !flags.remoteControlEnabled,
      },
      {
        name: 'api_key',
        value: apiKey,
        checked: false,
        type: 'password',
        secret: true,
        disabled: !flags.apiKeyEnabled,
      },
      {
        name: 'api_key_env',
        value: apiKeyEnv,
        checked: false,
        type: 'text',
        secret: false,
        disabled: !flags.apiKeyEnabled,
      },
      {
        name: 'base_url',
        value: baseUrl,
        checked: false,
        type: 'text',
        secret: false,
        disabled: !flags.remoteControlEnabled,
      },
      {
        name: 'onnx_dir',
        value: onnxDir,
        checked: false,
        type: 'text',
        secret: false,
        disabled: !flags.localControlEnabled,
      },
    ]
    onSave(buildMemoryConfigureParams(provider, fields))
  }

  const apiKeyLabel = provider === 'auto' ? 'Fallback API key' : 'API key'
  const remoteSummary = provider === 'auto' ? 'Remote fallback options' : 'Connection options'

  return (
    <div className="setup-mini panel">
      <div className="setup-mini__head">
        <h3 className="t-label">Memory embedding</h3>
        <CapabilityBadge status={status} name="memory_embedding" />
      </div>
      <p className="setup-muted">{memoryEmbeddingStatusText(status, config, provider)}</p>
      <EnvRecoveryCommand command={envRecoveryCommand(status, 'memory_embedding')} />
      <NeedList
        items={memoryNeedList(spec, provider, apiKeyEnv || spec.envKey)}
        label="Memory needs"
      />
      <label>
        <span>Provider</span>
        <SetupSelect
          aria-label="Memory embedding provider"
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
        >
          {providers.map((p) => (
            <option key={p.providerId} value={p.providerId}>
              {p.label}
            </option>
          ))}
        </SetupSelect>
      </label>
      {flags.localControlEnabled ? (
        <label>
          <span>ONNX directory</span>
          <input
            aria-label="Memory ONNX directory"
            placeholder="models/bge-onnx"
            value={onnxDir}
            onChange={(e) => setOnnxDir(e.target.value)}
          />
        </label>
      ) : null}
      {flags.hasRemoteOptions ? (
        <details className="setup-advanced" open={provider !== 'auto'}>
          <summary>{remoteSummary}</summary>
          <div className="setup-advanced__body" aria-label="Memory embedding connection">
            {flags.remoteControlEnabled ? (
              <label>
                <span>Model</span>
                <input
                  aria-label="Memory embedding model"
                  value={model}
                  placeholder={
                    provider === 'ollama' ? 'nomic-embed-text' : 'text-embedding-3-small'
                  }
                  onChange={(e) => setModel(e.target.value)}
                />
              </label>
            ) : null}
            {flags.apiKeyEnabled ? (
              <>
                <label>
                  <span>{apiKeyLabel}</span>
                  <input
                    type="password"
                    aria-label="Memory embedding API key"
                    placeholder="leave blank to keep current"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                  />
                </label>
                <label>
                  <span>API key env</span>
                  <input
                    aria-label="Memory embedding API key env"
                    value={apiKeyEnv}
                    placeholder={spec.envKey || 'OPENAI_API_KEY'}
                    onChange={(e) => setApiKeyEnv(e.target.value)}
                  />
                </label>
              </>
            ) : null}
            {flags.remoteControlEnabled ? (
              <label>
                <span>Base URL</span>
                <input
                  aria-label="Memory embedding base URL"
                  value={baseUrl}
                  placeholder={
                    provider === 'ollama' ? 'http://localhost:11434' : 'https://api.openai.com/v1'
                  }
                  onChange={(e) => setBaseUrl(e.target.value)}
                />
              </label>
            ) : null}
          </div>
        </details>
      ) : null}
      <Button
        type="button"
        variant={saveVariant(status, 'memory_embedding')}
        disabled={saving}
        onClick={collect}
      >
        Save memory embedding
      </Button>
    </div>
  )
}

// ── Memory settings (setup.js:877-904) ──────────────────────────────────────
function MemorySettingsCard({
  config,
  onSave,
  saving,
}: {
  config: SetupConfig
  onSave: (patches: Record<string, unknown>) => void
  saving: boolean
}) {
  const memory = config.memory || {}
  const [providerName, setProviderName] = useState(String(memory.provider?.name || ''))
  const [memoryLimit, setMemoryLimit] = useState(String(memory.curated_memory_char_limit ?? 4000))
  const [userLimit, setUserLimit] = useState(String(memory.curated_user_char_limit ?? 2000))
  const [injectLimit, setInjectLimit] = useState(String(memory.inject_limit ?? 6400))

  const overBudget = memorySettingsOverBudget(
    Number.parseInt(memoryLimit || '0', 10),
    Number.parseInt(userLimit || '0', 10),
    Number.parseInt(injectLimit || '0', 10),
  )

  const collect = () =>
    onSave(buildMemorySettingsPatches({ providerName, memoryLimit, userLimit, injectLimit }))

  return (
    <div className="setup-mini panel">
      <div className="setup-mini__head">
        <h3 className="t-label">Memory</h3>
      </div>
      <p className="setup-muted">
        Bounded long-term memory and profile notes carried into every conversation.
      </p>
      <label>
        <span>Memory provider</span>
        <SetupSelect
          aria-label="Memory provider"
          value={providerName}
          onChange={(e) => setProviderName(e.target.value)}
        >
          <option value="">None - built-in memory only</option>
          <option value="mem0">mem0</option>
        </SetupSelect>
      </label>
      <label>
        <span>Long-term memory budget (MEMORY.md)</span>
        <input
          type="number"
          min={0}
          step={1}
          aria-label="Long-term memory budget"
          value={memoryLimit}
          onChange={(e) => setMemoryLimit(e.target.value)}
        />
      </label>
      <label>
        <span>User profile budget (USER.md)</span>
        <input
          type="number"
          min={0}
          step={1}
          aria-label="User profile budget"
          value={userLimit}
          onChange={(e) => setUserLimit(e.target.value)}
        />
      </label>
      <label>
        <span>Prompt injection limit</span>
        <input
          type="number"
          min={0}
          step={1}
          aria-label="Prompt injection limit"
          value={injectLimit}
          onChange={(e) => setInjectLimit(e.target.value)}
        />
      </label>
      {overBudget ? (
        <div className="setup-warning panel tone-warn tone-rail">
          Injection limit too small. The user profile block may be dropped.
        </div>
      ) : null}
      <Button type="button" variant="outline" disabled={saving} onClick={collect}>
        Save memory settings
      </Button>
    </div>
  )
}

// ── Image generation (setup.js:905-926) ─────────────────────────────────────
function ImageCard({
  catalog,
  status,
  config,
  onSave,
  saving,
}: {
  catalog: Catalog
  status: OnboardingStatus
  config: SetupConfig
  onSave: (params: Record<string, unknown>) => void
  saving: boolean
}) {
  const providers = (catalog.imageGenerationProviders || []).filter((p) => p.runtimeSupported)
  const initial =
    status.imageGenerationProvider ||
    (status.imageGenerationPrimary || '').split('/')[0] ||
    providers[0]?.providerId ||
    'openrouter'
  const [provider, setProvider] = useState(initial)
  const spec: ProviderSpec = providers.find((p) => p.providerId === provider) ||
    providers[0] || {
      providerId: provider,
    }
  const imageConfig = config.image_generation || {}
  const providerConfig = (imageConfig.providers || {})[provider] || {}
  const enabledInitial = status.imageGenerationEnabled === false ? false : true

  const [enabled, setEnabled] = useState(enabledInitial)
  const [primary, setPrimary] = useState(
    String(status.imageGenerationPrimary || spec.defaultModel || ''),
  )
  const [apiKey, setApiKey] = useState('')
  const [apiKeyEnv, setApiKeyEnv] = useState(
    String(providerConfig.api_key_env || (spec.requiresApiKey ? spec.envKey : '') || ''),
  )
  const [baseUrl, setBaseUrl] = useState(
    String(providerConfig.base_url || spec.defaultBaseUrl || ''),
  )
  const statusText =
    enabled === enabledInitial
      ? imageGenerationStatusText(status)
      : enabled
        ? 'Save to make image generation available to agents.'
        : 'Save to hide image generation from agents.'

  const needs = enabled
    ? credentialNeedList(spec.whatYouNeed, apiKeyEnv || spec.envKey)
    : ['No key required while image generation is disabled.']

  const collect = () => {
    const fields: CapabilityField[] = [
      {
        name: 'primary',
        value: primary,
        checked: false,
        type: 'text',
        secret: false,
        disabled: false,
      },
      {
        name: 'api_key',
        value: apiKey,
        checked: false,
        type: 'password',
        secret: true,
        disabled: false,
      },
      {
        name: 'api_key_env',
        value: apiKeyEnv,
        checked: false,
        type: 'text',
        secret: false,
        disabled: false,
      },
      {
        name: 'base_url',
        value: baseUrl,
        checked: false,
        type: 'text',
        secret: false,
        disabled: false,
      },
    ]
    onSave(buildImageConfigureParams(provider, enabled, fields))
  }

  return (
    <div className="setup-mini panel">
      <div className="setup-mini__head">
        <h3 className="t-label">Image generation</h3>
        <CapabilityBadge status={status} name="image_generation" />
      </div>
      <p className="setup-muted">{statusText}</p>
      <EnvRecoveryCommand command={envRecoveryCommand(status, 'image_generation')} />
      <NeedList items={needs} label="Image needs" />
      <SetupCheckbox
        ariaLabel="Image generation enabled"
        checked={enabled}
        className="setup-capability-toggle"
        onChange={setEnabled}
      >
        Enable image generation
      </SetupCheckbox>
      {enabled ? (
        <div className="setup-advanced__body">
          <label>
            <span>Provider</span>
            <SetupSelect
              aria-label="Image provider"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
            >
              {providers.map((p) => (
                <option key={p.providerId} value={p.providerId}>
                  {p.label}
                </option>
              ))}
            </SetupSelect>
          </label>
          <label>
            <span>Primary model</span>
            <input
              aria-label="Image primary model"
              value={primary}
              onChange={(e) => setPrimary(e.target.value)}
            />
          </label>
          <label>
            <span>API key</span>
            <input
              type="password"
              aria-label="Image API key"
              placeholder="leave blank to keep current"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </label>
          <label>
            <span>API key env</span>
            <input
              aria-label="Image API key env"
              value={apiKeyEnv}
              placeholder={spec.envKey || 'OPENROUTER_API_KEY'}
              onChange={(e) => setApiKeyEnv(e.target.value)}
            />
          </label>
          <label>
            <span>Base URL</span>
            <input
              aria-label="Image base URL"
              value={baseUrl}
              placeholder={spec.defaultBaseUrl || 'https://api.openai.com/v1'}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </label>
        </div>
      ) : null}
      <Button
        type="button"
        variant={saveVariant(status, 'image_generation')}
        disabled={saving}
        onClick={collect}
      >
        Save image generation
      </Button>
    </div>
  )
}

// ── Voice audio (setup.js:927-950) ──────────────────────────────────────────
function AudioCard({
  catalog,
  status,
  config,
  onSave,
  saving,
}: {
  catalog: Catalog
  status: OnboardingStatus
  config: SetupConfig
  onSave: (params: Record<string, unknown>) => void
  saving: boolean
}) {
  const providers = (catalog.audioProviders || []).filter((p) => p.runtimeSupported)
  const initial = status.audioProvider || providers[0]?.providerId || 'elevenlabs'
  const [provider, setProvider] = useState(initial)
  const spec: ProviderSpec = providers.find((p) => p.providerId === provider) ||
    providers[0] || {
      providerId: provider,
    }
  const audioConfig = config.audio || {}
  const providerConfig = (audioConfig.providers || {})[provider] || {}
  const tts = (audioConfig.tts || {}) as Record<string, string>
  const enabledInitial = status.audioEnabled === true || audioConfig.enabled === true

  const [enabled, setEnabled] = useState(enabledInitial)
  const [apiKey, setApiKey] = useState('')
  const [apiKeyEnv, setApiKeyEnv] = useState(
    String(providerConfig.api_key_env || (spec.requiresApiKey ? spec.envKey : '') || ''),
  )
  const [baseUrl, setBaseUrl] = useState(
    String(providerConfig.base_url || spec.defaultBaseUrl || ''),
  )
  const [ttsVoice, setTtsVoice] = useState(String(tts.voice || spec.defaultTtsVoice || ''))
  const [ttsModel, setTtsModel] = useState(String(tts.model || spec.defaultTtsModel || ''))
  const [languageCode, setLanguageCode] = useState(
    String(tts.language_code || spec.defaultLanguageCode || ''),
  )
  const statusText =
    enabled === enabledInitial
      ? audioStatusText(status)
      : enabled
        ? 'Save to make voice audio available to agents.'
        : 'Save to hide voice audio from agents.'

  const needs = enabled
    ? credentialNeedList(spec.whatYouNeed, apiKeyEnv || spec.envKey)
    : ['No key required while voice audio is disabled.']

  const collect = () => {
    const fields: CapabilityField[] = [
      {
        name: 'api_key',
        value: apiKey,
        checked: false,
        type: 'password',
        secret: true,
        disabled: false,
      },
      {
        name: 'api_key_env',
        value: apiKeyEnv,
        checked: false,
        type: 'text',
        secret: false,
        disabled: false,
      },
      {
        name: 'base_url',
        value: baseUrl,
        checked: false,
        type: 'text',
        secret: false,
        disabled: false,
      },
      {
        name: 'tts_voice',
        value: ttsVoice,
        checked: false,
        type: 'text',
        secret: false,
        disabled: false,
      },
      {
        name: 'tts_model',
        value: ttsModel,
        checked: false,
        type: 'text',
        secret: false,
        disabled: false,
      },
      {
        name: 'language_code',
        value: languageCode,
        checked: false,
        type: 'text',
        secret: false,
        disabled: false,
      },
    ]
    onSave(buildAudioConfigureParams(provider, enabled, fields))
  }

  return (
    <div className="setup-mini panel">
      <div className="setup-mini__head">
        <h3 className="t-label">Voice audio</h3>
        <CapabilityBadge status={status} name="audio" />
      </div>
      <p className="setup-muted">{statusText}</p>
      <EnvRecoveryCommand command={envRecoveryCommand(status, 'audio')} />
      <NeedList items={needs} label="Audio needs" />
      <SetupCheckbox
        ariaLabel="Voice audio enabled"
        checked={enabled}
        className="setup-capability-toggle"
        onChange={setEnabled}
      >
        Enable voice audio
      </SetupCheckbox>
      {enabled ? (
        <div className="setup-advanced__body">
          <label>
            <span>Provider</span>
            <SetupSelect
              aria-label="Audio provider"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
            >
              {providers.map((p) => (
                <option key={p.providerId} value={p.providerId}>
                  {p.label}
                </option>
              ))}
            </SetupSelect>
          </label>
          <label>
            <span>API key</span>
            <input
              type="password"
              aria-label="Audio API key"
              placeholder="leave blank to keep current"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </label>
          <label>
            <span>API key env</span>
            <input
              aria-label="Audio API key env"
              value={apiKeyEnv}
              placeholder={spec.envKey || 'ELEVENLABS_API_KEY'}
              onChange={(e) => setApiKeyEnv(e.target.value)}
            />
          </label>
          <label>
            <span>Base URL</span>
            <input
              aria-label="Audio base URL"
              value={baseUrl}
              placeholder={spec.defaultBaseUrl || 'https://api.elevenlabs.io'}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </label>
          <label>
            <span>TTS voice</span>
            <input
              aria-label="Audio TTS voice"
              value={ttsVoice}
              placeholder={spec.defaultTtsVoice || 'voice id'}
              onChange={(e) => setTtsVoice(e.target.value)}
            />
          </label>
          <label>
            <span>TTS model</span>
            <input
              aria-label="Audio TTS model"
              value={ttsModel}
              placeholder={spec.defaultTtsModel || 'eleven_multilingual_v2'}
              onChange={(e) => setTtsModel(e.target.value)}
            />
          </label>
          <label>
            <span>Language code</span>
            <input
              aria-label="Audio language code"
              value={languageCode}
              placeholder="zh-CN, en-US, en-GB"
              onChange={(e) => setLanguageCode(e.target.value)}
            />
          </label>
        </div>
      ) : null}
      <Button
        type="button"
        variant={saveVariant(status, 'audio')}
        disabled={saving}
        onClick={collect}
      >
        Save voice audio
      </Button>
    </div>
  )
}

export function ExtrasSection({
  catalog,
  status,
  config,
  onSaveSearch,
  onSaveMemory,
  onSaveMemorySettings,
  onSaveImage,
  onSaveAudio,
  onBack,
  onNext,
  saving,
  resetVersions,
  conflicts,
  onDirtyChange,
}: {
  catalog: Catalog
  status: OnboardingStatus
  config: SetupConfig
  onSaveSearch: (params: Record<string, unknown>) => void
  onSaveMemory: (params: Record<string, unknown>) => void
  onSaveMemorySettings: (patches: Record<string, unknown>) => void
  onSaveImage: (params: Record<string, unknown>) => void
  onSaveAudio: (params: Record<string, unknown>) => void
  onBack: () => void
  onNext: () => void
  saving: boolean
  resetVersions: {
    search: number
    memoryEmbedding: number
    memorySettings: number
    image: number
    audio: number
  }
  conflicts: Record<ExtrasResetTarget, boolean>
  onDirtyChange: (target: ExtrasResetTarget) => void
}) {
  return (
    <section className="setup-panel panel">
      <PanelHead
        title="Capability Center"
        subtitle="Web search · Memory recall · Image generation · Voice audio"
      />
      <div className="setup-extras">
        <div className="setup-capability-slot" onChangeCapture={() => onDirtyChange('search')}>
          <SearchCard
            key={`search:${resetVersions.search}`}
            catalog={catalog}
            status={status}
            config={config}
            onSave={onSaveSearch}
            saving={saving || conflicts.search}
          />
        </div>
        <div
          className="setup-capability-slot"
          onChangeCapture={() => onDirtyChange('memoryEmbedding')}
        >
          <MemoryEmbeddingCard
            key={`memory-embedding:${resetVersions.memoryEmbedding}`}
            catalog={catalog}
            status={status}
            config={config}
            onSave={onSaveMemory}
            saving={saving || conflicts.memoryEmbedding}
          />
        </div>
        <div
          className="setup-capability-slot"
          onChangeCapture={() => onDirtyChange('memorySettings')}
        >
          <MemorySettingsCard
            key={`memory-settings:${resetVersions.memorySettings}`}
            config={config}
            onSave={onSaveMemorySettings}
            saving={saving || conflicts.memorySettings}
          />
        </div>
        <div className="setup-capability-slot" onChangeCapture={() => onDirtyChange('image')}>
          <ImageCard
            key={`image:${resetVersions.image}`}
            catalog={catalog}
            status={status}
            config={config}
            onSave={onSaveImage}
            saving={saving || conflicts.image}
          />
        </div>
        <div className="setup-capability-slot" onChangeCapture={() => onDirtyChange('audio')}>
          <AudioCard
            key={`audio:${resetVersions.audio}`}
            catalog={catalog}
            status={status}
            config={config}
            onSave={onSaveAudio}
            saving={saving || conflicts.audio}
          />
        </div>
      </div>
      <div className="setup-actions">
        <Button type="button" variant="outline" onClick={onBack}>
          Back
        </Button>
        <Button type="button" variant="outline" onClick={onNext}>
          Next
        </Button>
      </div>
    </section>
  )
}
