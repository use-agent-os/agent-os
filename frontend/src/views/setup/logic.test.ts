import { describe, expect, it } from 'vitest'
import {
  aggregateStepStatus,
  audioStatusText,
  buildAudioConfigureParams,
  buildImageConfigureParams,
  buildMemoryConfigureParams,
  buildMemorySettingsPatches,
  buildRouterConfigureParams,
  buildSearchConfigureParams,
  camel,
  capabilityBadge,
  capabilityIsPrimary,
  configCliArg,
  configuredProvider,
  credentialNeedList,
  detailStepStatus,
  effectiveProvider,
  envFixCommands,
  envRecoveryCommand,
  envReferenceSaveAdvisory,
  finishSummary,
  handoffCommands,
  hasSetupAction,
  imageGenerationStatusText,
  initialStepFromStatus,
  isProviderAdvancedField,
  isVisibleTier,
  memoryControlFlags,
  memoryEmbeddingStatusText,
  memoryNeedList,
  memorySettingsOverBudget,
  mergeTiers,
  onboardingReasons,
  providerAdvancedOpen,
  providerConfigFor,
  providerFieldValue,
  providerRouterSupportText,
  providerRouterSupportTone,
  readinessStatusLabel,
  readinessTone,
  readScopedFields,
  recipeCommands,
  resolveJudgeModelParam,
  routerMode,
  searchStatusText,
  setupHeadline,
  setupStepForSection,
  shellArg,
  stepForSection,
  stepStatus,
  tierLabel,
  validateScopedRequiredFields,
  type CapabilityField,
  type OnboardingStatus,
  type ProviderSpec,
  type ScopedField,
  type SectionDetail,
  type SetupConfig,
} from './logic'

const detail = (d: Partial<SectionDetail>): SectionDetail => d

describe('camel (setup.js:2010-2012)', () => {
  it('converts snake_case to camelCase', () => {
    expect(camel('api_key_env')).toBe('apiKeyEnv')
    expect(camel('max_results')).toBe('maxResults')
    expect(camel('model')).toBe('model')
    expect(camel('')).toBe('')
  })
})

describe('tierLabel (setup.js:660-662)', () => {
  it('maps known tiers and falls back to Route c1', () => {
    expect(tierLabel('c0')).toBe('Route c0')
    expect(tierLabel('c3')).toBe('Route c3')
    expect(tierLabel('weird')).toBe('weird')
    expect(tierLabel(undefined)).toBe('Route c1')
  })
})

describe('detailStepStatus (setup.js:158-173)', () => {
  it('reviews when detail absent', () => {
    expect(detailStepStatus(undefined)).toEqual({ label: 'Review', tone: 'is-muted' })
  })
  it('needs action on blocking/actionRequired/missing/degraded', () => {
    expect(detailStepStatus(detail({ blocking: true })).tone).toBe('is-warn')
    expect(detailStepStatus(detail({ actionRequired: true })).tone).toBe('is-warn')
    expect(detailStepStatus(detail({ status: 'missing' })).tone).toBe('is-warn')
    expect(detailStepStatus(detail({ status: 'degraded' })).tone).toBe('is-warn')
  })
  it('ready on ok', () => {
    expect(detailStepStatus(detail({ status: 'ok' }))).toEqual({ label: 'Ready', tone: 'is-ok' })
  })
  it('otherwise labels from READINESS_LABELS', () => {
    expect(detailStepStatus(detail({ status: 'optional' }))).toEqual({
      label: 'Optional',
      tone: 'is-muted',
    })
  })
})

describe('aggregateStepStatus (setup.js:146-156)', () => {
  const mk = (details: Record<string, SectionDetail>): OnboardingStatus => ({
    sectionDetails: details,
  })
  it('needs action if any section needs action', () => {
    const s = mk({ search: detail({ status: 'ok' }), audio: detail({ status: 'missing' }) })
    expect(aggregateStepStatus(s, ['search', 'audio']).tone).toBe('is-warn')
  })
  it('ready only when every present section is ok', () => {
    const s = mk({ search: detail({ status: 'ok' }), audio: detail({ status: 'ok' }) })
    expect(aggregateStepStatus(s, ['search', 'audio'])).toEqual({ label: 'Ready', tone: 'is-ok' })
  })
  it('optional otherwise (incl. empty)', () => {
    expect(aggregateStepStatus(mk({}), ['search']).label).toBe('Optional')
    const s = mk({ search: detail({ status: 'ok' }), audio: detail({ status: 'optional' }) })
    expect(aggregateStepStatus(s, ['search', 'audio']).label).toBe('Optional')
  })
})

describe('hasSetupAction (setup.js:261-270)', () => {
  it('true when needsOnboarding', () => {
    expect(hasSetupAction({ needsOnboarding: true })).toBe(true)
  })
  it('true when any section needs action', () => {
    expect(hasSetupAction({ sectionDetails: { llm: detail({ status: 'missing' }) } })).toBe(true)
  })
  it('false when nothing pending', () => {
    expect(hasSetupAction({ sectionDetails: { llm: detail({ status: 'ok' }) } })).toBe(false)
    expect(hasSetupAction({})).toBe(false)
  })
})

describe('stepStatus (setup.js:125-144)', () => {
  it('provider needs action when env missing', () => {
    expect(stepStatus('provider', { llmSource: 'missing_env' }, 'openai').tone).toBe('is-warn')
  })
  it('provider falls to llm/provider detail', () => {
    const s: OnboardingStatus = { sectionDetails: { llm: detail({ status: 'ok' }) } }
    expect(stepStatus('provider', s, 'openai')).toEqual({ label: 'Ready', tone: 'is-ok' })
  })
  it('router: provider-first when no provider', () => {
    expect(stepStatus('router', {}, '')).toEqual({ label: 'Provider first', tone: 'is-muted' })
  })
  it('router: detail when provider present', () => {
    const s: OnboardingStatus = { sectionDetails: { router: detail({ status: 'ok' }) } }
    expect(stepStatus('router', s, 'openai').tone).toBe('is-ok')
  })
  it('extras aggregates capabilities', () => {
    const s: OnboardingStatus = {
      sectionDetails: { search: detail({ status: 'missing' }) },
    }
    expect(stepStatus('extras', s, 'openai').tone).toBe('is-warn')
  })
  it('finish reviews when action pending else ready', () => {
    expect(stepStatus('finish', { needsOnboarding: true }, 'openai').label).toBe('Review')
    expect(stepStatus('finish', {}, 'openai').label).toBe('Ready')
  })
})

describe('stepForSection / initialStepFromStatus (setup.js:280-305)', () => {
  it('maps sections to steps', () => {
    expect(stepForSection('router')).toBe('router')
    expect(stepForSection('search')).toBe('extras')
    expect(stepForSection('unknown')).toBe('provider')
  })
  it('initial step: first needing action', () => {
    const s: OnboardingStatus = {
      sectionDetails: { llm: detail({ status: 'ok' }), channels: detail({ status: 'missing' }) },
    }
    expect(initialStepFromStatus(s)).toBe('finish')
  })
  it('initial step: finish when needsOnboarding===false and nothing pending', () => {
    expect(initialStepFromStatus({ needsOnboarding: false })).toBe('finish')
  })
  it('initial step: provider default', () => {
    expect(initialStepFromStatus({})).toBe('provider')
  })
})

describe('onboardingReasons + setupHeadline (setup.js:177-259)', () => {
  it('empty when no action', () => {
    expect(onboardingReasons({}, {})).toEqual([])
    expect(setupHeadline([])).toEqual({ title: 'Ready to run', chip: 'Ready', tone: 'is-ok' })
  })
  it('env-missing → blocking provider reason', () => {
    const status: OnboardingStatus = { llmSource: 'missing_env', needsOnboarding: true }
    const config: SetupConfig = { llm: { api_key_env: 'OPENAI_API_KEY' } }
    const reasons = onboardingReasons(status, config)
    expect(reasons[0]).toEqual({
      text: 'OPENAI_API_KEY is not visible',
      tier: 'blocking',
      step: 'provider',
    })
    expect(setupHeadline(reasons)).toEqual({
      title: 'Action needed',
      chip: 'Action needed',
      tone: 'is-warn',
    })
  })
  it('no provider/model → connect-a-provider blocking', () => {
    const reasons = onboardingReasons({ needsOnboarding: true }, { llm: {} })
    expect(
      reasons.some((r) => r.text === 'Connect a model provider' && r.tier === 'blocking'),
    ).toBe(true)
  })
  it('optional-only headline downgrades', () => {
    const status: OnboardingStatus = {
      sectionDetails: {
        audio: detail({ status: 'optional', actionRequired: true, label: 'Audio' }),
      },
    }
    const config: SetupConfig = { llm: { provider: 'openai', model: 'gpt' } }
    const reasons = onboardingReasons(status, config)
    expect(reasons).toEqual([{ text: 'Audio setup needed', tier: 'optional', step: 'extras' }])
    expect(setupHeadline(reasons)).toEqual({
      title: 'Optional improvements',
      chip: 'Optional · 1 item',
      tone: 'is-optional',
    })
  })
  it('dedupes repeated reason text', () => {
    const status: OnboardingStatus = {
      needsOnboarding: true,
      sectionDetails: {
        llm: detail({ status: 'missing' }),
        provider: detail({ status: 'missing' }),
      },
    }
    const reasons = onboardingReasons(status, { llm: {} })
    expect(reasons.filter((r) => r.text === 'Connect a model provider')).toHaveLength(1)
  })
})

describe('envRecoveryCommand (setup.js:501-507)', () => {
  it('finds by section', () => {
    const status: OnboardingStatus = {
      envRecoveryCommands: [{ section: 'search', command: 'export X=1' }],
    }
    expect(envRecoveryCommand(status, 'search')).toBe('export X=1')
    expect(envRecoveryCommand(status, 'audio')).toBe('')
  })
})

describe('provider derivation (setup.js:406-483)', () => {
  const spec = (p: Partial<ProviderSpec>): ProviderSpec => ({ providerId: 'openai', ...p })
  it('router-support text/tone', () => {
    expect(providerRouterSupportText(spec({ routerSupported: true }))).toBe('Pilot Router ready')
    expect(providerRouterSupportText(spec({ routerSupported: false }))).toBe('Direct only')
    expect(providerRouterSupportText(null)).toBe('choose provider')
    expect(providerRouterSupportTone(spec({ routerSupported: true }))).toBe('is-ready')
    expect(providerRouterSupportTone(spec({ routerSupported: false }))).toBe('is-direct')
    expect(providerRouterSupportTone(null)).toBe('is-neutral')
  })
  it('providerConfigFor only returns matching provider config', () => {
    const config: SetupConfig = { llm: { provider: 'openai', model: 'gpt' } }
    expect(providerConfigFor(config, 'openai')).toEqual({ provider: 'openai', model: 'gpt' })
    expect(providerConfigFor(config, 'anthropic')).toEqual({})
  })
  it('configuredProvider gates on status', () => {
    const config: SetupConfig = { llm: { provider: 'openai' } }
    expect(configuredProvider({}, config)).toBe('openai')
    expect(configuredProvider({ hasConfig: false }, config)).toBe('')
    expect(configuredProvider({ hasConfig: false, llmConfigured: true }, config)).toBe('openai')
    expect(configuredProvider({ hasConfig: false, llmSource: 'env' }, config)).toBe('openai')
    expect(configuredProvider({}, {})).toBe('')
  })
  it('effectiveProvider prefers draft', () => {
    const config: SetupConfig = { llm: { provider: 'openai' } }
    expect(effectiveProvider({}, config, 'anthropic')).toBe('anthropic')
    expect(effectiveProvider({}, config, '')).toBe('openai')
  })
  it('isProviderAdvancedField', () => {
    const s = spec({ routerSupported: true })
    expect(isProviderAdvancedField({ name: 'base_url' }, s)).toBe(true)
    expect(isProviderAdvancedField({ name: 'proxy' }, s)).toBe(true)
    expect(isProviderAdvancedField({ name: 'model', required: false }, s)).toBe(true)
    expect(isProviderAdvancedField({ name: 'model', required: true }, s)).toBe(false)
    expect(isProviderAdvancedField({ name: 'api_key' }, s)).toBe(false)
  })
  it('providerFieldValue seeds from config', () => {
    const cur = { model: 'gpt', base_url: 'http://x', proxy: 'p', api_key: 'secret' }
    expect(providerFieldValue({ name: 'model' }, cur)).toBe('gpt')
    expect(providerFieldValue({ name: 'base_url' }, cur)).toBe('http://x')
    expect(providerFieldValue({ name: 'proxy' }, cur)).toBe('p')
    // api_key present with no env → blank env value
    expect(providerFieldValue({ name: 'api_key_env' }, cur)).toBe('')
    // no api_key → default used
    expect(providerFieldValue({ name: 'api_key_env', default: 'OPENAI_API_KEY' }, {})).toBe(
      'OPENAI_API_KEY',
    )
  })
  it('providerAdvancedOpen when a value diverges from default', () => {
    expect(providerAdvancedOpen([{ name: 'base_url', default: 'd' }], { base_url: 'other' })).toBe(
      true,
    )
    expect(providerAdvancedOpen([{ name: 'base_url', default: 'd' }], { base_url: 'd' })).toBe(
      false,
    )
    expect(providerAdvancedOpen([{ name: 'proxy', required: true }], {})).toBe(true)
  })
})

describe('credentialNeedList / memoryNeedList (setup.js:333-353)', () => {
  it('rewrites credential lines with the env key', () => {
    const items = ['API key via OLD_KEY or a one-time paste.', 'other']
    expect(credentialNeedList(items, 'NEW_KEY')).toEqual([
      'API key via NEW_KEY or a one-time paste.',
      'other',
    ])
  })
  it('returns items unchanged when no key', () => {
    expect(credentialNeedList(['a'], '')).toEqual(['a'])
  })
  it('memoryNeedList: auto with no key drops remote fallback', () => {
    const spec: ProviderSpec = {
      providerId: 'auto',
      whatYouNeed: ['keeps', 'remote fallback credentials optional'],
    }
    expect(memoryNeedList(spec, 'auto', '')).toEqual(['keeps'])
  })
  it('memoryNeedList: requiresApiKey rewrites credentials', () => {
    const spec: ProviderSpec = {
      providerId: 'openai',
      requiresApiKey: true,
      envKey: 'OPENAI_API_KEY',
      whatYouNeed: ['API key via X or a one-time paste.'],
    }
    expect(memoryNeedList(spec, 'openai', undefined)).toEqual([
      'API key via OPENAI_API_KEY or a one-time paste.',
    ])
  })
})

describe('capability status text (setup.js:977-1058)', () => {
  it('searchStatusText branches', () => {
    expect(searchStatusText({}, {})).toBe('Web search is off until a provider is selected.')
    expect(searchStatusText({ searchConfigured: true }, { search_provider: 'brave' })).toBe(
      'Web search is ready for new turns.',
    )
    expect(
      searchStatusText(
        { searchSource: 'missing_env', searchEnvKey: 'BRAVE_KEY' },
        { search_provider: 'brave' },
      ),
    ).toBe('Web search is selected, but $BRAVE_KEY is not visible to the gateway.')
  })
  it('imageGenerationStatusText branches', () => {
    expect(imageGenerationStatusText({ imageGenerationEnabled: false })).toContain('hidden')
    expect(
      imageGenerationStatusText({
        imageGenerationConfigured: true,
        imageGenerationSource: 'llm_fallback',
      }),
    ).toContain('same provider key')
    expect(
      imageGenerationStatusText({
        imageGenerationSource: 'missing_env',
        imageGenerationEnvKey: 'K',
      }),
    ).toContain('$K is not visible')
  })
  it('audioStatusText branches', () => {
    expect(audioStatusText({ audioEnabled: false })).toContain('stay hidden')
    expect(audioStatusText({ audioConfigured: true })).toContain('ready for TTS')
    expect(audioStatusText({ audioSource: 'missing_env', audioEnvKey: 'E' })).toContain(
      '$E is not visible',
    )
  })
  it('memoryEmbeddingStatusText branches', () => {
    expect(memoryEmbeddingStatusText({}, {}, 'none')).toContain('embeddings are disabled')
    expect(memoryEmbeddingStatusText({}, {}, 'local')).toContain('local BGE')
    expect(memoryEmbeddingStatusText({}, {}, 'ollama')).toContain('Ollama')
    expect(memoryEmbeddingStatusText({}, {}, 'auto')).toContain('Local-first')
    const cfg: SetupConfig = { memory: { embedding: { provider: 'openai' } } }
    expect(memoryEmbeddingStatusText({ memoryEmbeddingConfigured: true }, cfg, 'openai')).toContain(
      'configured for new turns',
    )
  })
})

describe('memoryControlFlags (setup.js:741-749)', () => {
  it('auto enables remote + apiKey, not local', () => {
    expect(memoryControlFlags('auto', undefined)).toEqual({
      remoteControlEnabled: true,
      apiKeyEnabled: true,
      localControlEnabled: false,
      hasRemoteOptions: true,
    })
  })
  it('local enables only local', () => {
    const f = memoryControlFlags('local', undefined)
    expect(f.localControlEnabled).toBe(true)
    expect(f.remoteControlEnabled).toBe(false)
    expect(f.hasRemoteOptions).toBe(false)
  })
  it('openai apiKey follows requiresApiKey', () => {
    expect(
      memoryControlFlags('openai', { providerId: 'openai', requiresApiKey: true }).apiKeyEnabled,
    ).toBe(true)
  })
})

describe('router derivation (setup.js:550-635,1767-1846)', () => {
  it('routerMode force-migrates v4_phase3 / unknown to pilot-v1', () => {
    expect(routerMode({ enabled: false })).toBe('disabled')
    expect(routerMode({ strategy: 'pilot-v1' })).toBe('pilot-v1')
    expect(routerMode({ strategy: 'llm_judge' })).toBe('llm_judge')
    expect(routerMode({ strategy: 'v4_phase3' })).toBe('pilot-v1')
    expect(routerMode({})).toBe('pilot-v1')
  })
  it('mergeTiers overlays config over profile', () => {
    expect(mergeTiers({ c0: { model: 'a' } }, { c0: { model: 'b' } })).toEqual({
      c0: { model: 'b' },
    })
  })
  it('isVisibleTier keeps text tiers + image_model', () => {
    expect(isVisibleTier('c0')).toBe(true)
    expect(isVisibleTier('image_model')).toBe(true)
    expect(isVisibleTier('c9')).toBe(false)
  })
  it('resolveJudgeModelParam: no local endpoint', () => {
    // unchanged selection preserves (null)
    expect(resolveJudgeModelParam('m', 'm', false)).toBeNull()
    // changed to a model pins it
    expect(resolveJudgeModelParam('n', 'm', false)).toBe('n')
    // changed to empty clears to AUTO
    expect(resolveJudgeModelParam('', 'm', false)).toBe('')
  })
  it('resolveJudgeModelParam: local endpoint preserves unless deliberate pick', () => {
    expect(resolveJudgeModelParam('', '', true)).toBeNull()
    expect(resolveJudgeModelParam('cloud-model', '', true)).toBe('cloud-model')
  })
  it('buildRouterConfigureParams: pilot forwards threshold + stamps image_model', () => {
    const params = buildRouterConfigureParams({
      sel: 'pilot-v1',
      defaultTier: 'c2',
      judgeModel: null,
      pilotThresholdRaw: '0.7',
      tiers: [
        { tier: 'c0', provider: 'openai', model: 'a', thinkingLevel: 'low', supportsImage: false },
        {
          tier: 'image_model',
          provider: 'openai',
          model: 'img',
          thinkingLevel: '',
          supportsImage: false,
        },
      ],
    })
    expect(params.mode).toBe('recommended')
    expect(params.strategy).toBe('pilot-v1')
    expect(params.defaultTier).toBe('c2')
    expect(params.safetyNetThreshold).toBe(0.7)
    expect(params.tiers.image_model).toMatchObject({ supportsImage: true, image_only: true })
  })
  it('buildRouterConfigureParams: disabled drops strategy + threshold', () => {
    const params = buildRouterConfigureParams({
      sel: 'disabled',
      defaultTier: 'c1',
      judgeModel: null,
      pilotThresholdRaw: '0.5',
      tiers: [],
    })
    expect(params.mode).toBe('disabled')
    expect(params.strategy).toBeUndefined()
    expect(params.safetyNetThreshold).toBeUndefined()
  })
  it('buildRouterConfigureParams: judge mode never forwards threshold', () => {
    const params = buildRouterConfigureParams({
      sel: 'llm_judge',
      defaultTier: 'c1',
      judgeModel: 'j',
      pilotThresholdRaw: '0.9',
      tiers: [],
    })
    expect(params.strategy).toBe('llm_judge')
    expect(params.safetyNetThreshold).toBeUndefined()
    expect(params.judgeModel).toBe('j')
  })
})

describe('readScopedFields / validateScopedRequiredFields (setup.js:1705-1741)', () => {
  const f = (o: Partial<ScopedField>): ScopedField => ({
    name: 'x',
    value: '',
    checked: false,
    type: 'text',
    secret: false,
    required: false,
    hidden: false,
    ...o,
  })
  it('camelCases non-channel scope, keeps snake for channel', () => {
    const fields = [f({ name: 'api_key_env', value: 'K' })]
    expect(readScopedFields(fields, 'provider')).toEqual({ apiKeyEnv: 'K' })
    expect(readScopedFields(fields, 'channel')).toEqual({ api_key_env: 'K' })
  })
  it('checkboxes → bool; hidden skipped; blank secret omitted', () => {
    const fields = [
      f({ name: 'enabled', type: 'checkbox', checked: true }),
      f({ name: 'hidden_one', value: 'v', hidden: true }),
      f({ name: 'api_key', value: '', secret: true }),
      f({ name: 'plain', value: '' }),
    ]
    expect(readScopedFields(fields, 'provider')).toEqual({ enabled: true, plain: '' })
  })
  it('validate returns first missing required label', () => {
    const fields = [
      f({ name: 'token', required: true, value: '', label: 'Bot token *' }),
      f({ name: 'other', required: true, value: '' }),
    ]
    expect(validateScopedRequiredFields(fields, false)).toBe('Bot token')
  })
  it('validate passes when required filled', () => {
    expect(
      validateScopedRequiredFields([f({ required: true, value: 'v', label: 'X' })], false),
    ).toBe('')
  })
  it('validate allows blank secret when keep-existing', () => {
    const fields = [f({ name: 'api_key', required: true, secret: true, value: '', label: 'Key' })]
    expect(validateScopedRequiredFields(fields, true)).toBe('')
    expect(validateScopedRequiredFields(fields, false)).toBe('Key')
  })
})

describe('capability payload builders (setup.js:1877-1994)', () => {
  const cf = (o: Partial<CapabilityField>): CapabilityField => ({
    name: 'x',
    value: '',
    checked: false,
    type: 'text',
    secret: false,
    disabled: false,
    ...o,
  })
  it('buildMemoryConfigureParams skips disabled + blank secrets', () => {
    const params = buildMemoryConfigureParams('openai', [
      cf({ name: 'model', value: 'm' }),
      cf({ name: 'api_key', value: '', secret: true }),
      cf({ name: 'base_url', value: 'b', disabled: true }),
    ])
    expect(params).toEqual({ providerId: 'openai', model: 'm' })
  })
  it('buildMemoryConfigureParams defaults providerId to auto', () => {
    expect(buildMemoryConfigureParams('', [])).toEqual({ providerId: 'auto' })
  })
  it('buildSearchConfigureParams parses numbers, bools, skips blank secret', () => {
    const params = buildSearchConfigureParams('brave', [
      cf({ name: 'max_results', value: '5', type: 'number' }),
      cf({ name: 'diagnostics', type: 'checkbox', checked: true }),
      cf({ name: 'api_key', value: '', secret: true }),
      cf({ name: 'api_key_env', value: 'BRAVE' }),
    ])
    expect(params).toEqual({
      providerId: 'brave',
      maxResults: 5,
      diagnostics: true,
      apiKeyEnv: 'BRAVE',
    })
  })
  it('buildImageConfigureParams carries enabled + skips blank secret', () => {
    const params = buildImageConfigureParams('openrouter', true, [
      cf({ name: 'primary', value: 'p' }),
      cf({ name: 'api_key', value: '', secret: true }),
    ])
    expect(params).toEqual({ providerId: 'openrouter', enabled: true, primary: 'p' })
  })
  it('buildAudioConfigureParams carries enabled', () => {
    const params = buildAudioConfigureParams('elevenlabs', false, [
      cf({ name: 'tts_voice', value: 'v' }),
    ])
    expect(params).toEqual({ providerId: 'elevenlabs', enabled: false, ttsVoice: 'v' })
  })
  it('buildMemorySettingsPatches null-coalesces provider name + parses limits', () => {
    expect(
      buildMemorySettingsPatches({
        providerName: '',
        memoryLimit: '4000',
        userLimit: '2000',
        injectLimit: '6400',
      }),
    ).toEqual({
      'memory.provider.name': null,
      'memory.curated_memory_char_limit': 4000,
      'memory.curated_user_char_limit': 2000,
      'memory.inject_limit': 6400,
    })
  })
  it('memorySettingsOverBudget uses the 310-char overhead heuristic', () => {
    expect(memorySettingsOverBudget(4000, 2000, 6400)).toBe(false) // 6310 <= 6400
    expect(memorySettingsOverBudget(4000, 2000, 6000)).toBe(true) // 6310 > 6000
  })
})

describe('envReferenceSaveAdvisory (setup.js:1060-1075)', () => {
  it('none when no key or inline key pasted', () => {
    expect(envReferenceSaveAdvisory({ surface: 'X', envKey: '' }).kind).toBe('none')
    expect(envReferenceSaveAdvisory({ surface: 'X', envKey: 'K', hasInlineKey: 'sk-1' }).kind).toBe(
      'none',
    )
  })
  it('warn on missing_env or restart', () => {
    expect(
      envReferenceSaveAdvisory({
        surface: 'Image generation',
        envKey: 'K',
        keySource: 'missing_env',
      }),
    ).toEqual({
      kind: 'warn',
      message: 'Image generation saved $K. Start or restart the gateway with that variable set.',
    })
    expect(
      envReferenceSaveAdvisory({ surface: 'X', envKey: 'K', restartRequired: true }).kind,
    ).toBe('warn')
  })
  it('info otherwise', () => {
    expect(envReferenceSaveAdvisory({ surface: 'X', envKey: 'K' })).toEqual({
      kind: 'info',
      message: 'X saved $K reference. Keep it set for gateway restarts.',
    })
  })
})

describe('Finish CLI assembly (setup.js:1077-1208,2014-2022)', () => {
  it('shellArg quotes only when needed', () => {
    expect(shellArg('/tmp/agentos.toml')).toBe('/tmp/agentos.toml')
    expect(shellArg('/has space/x.toml')).toBe("'/has space/x.toml'")
    expect(shellArg("it's")).toBe("'it'\\''s'")
  })
  it('configCliArg empty without a path', () => {
    expect(configCliArg('')).toBe('')
    expect(configCliArg('/tmp/x.toml')).toBe(' --config /tmp/x.toml')
  })
  it('envFixCommands appends a restart when there are recovery commands', () => {
    expect(envFixCommands([], ' --config /x')).toEqual([])
    const cmds = envFixCommands([{ label: 'set', command: 'export K=1' }], ' --config /x')
    expect(cmds).toHaveLength(2)
    expect(cmds[1]!.command).toBe('agentos gateway restart --config /x')
  })
  it('handoffCommands + recipeCommands include the config arg', () => {
    expect(handoffCommands(' --config /x')[0]!.command).toBe(
      'agentos onboard --if-needed --config /x',
    )
    expect(recipeCommands(' --config /x')).toHaveLength(6)
  })
})

describe('finishSummary (setup.js:1078-1150)', () => {
  it('summarizes configured provider', () => {
    const status: OnboardingStatus = { channelCount: 2 }
    const config: SetupConfig = {
      llm: { provider: 'openai', model: 'gpt-4', proxy: 'http://p' },
      agentos_router: { enabled: true },
    }
    expect(finishSummary(status, config)).toEqual({
      provider: 'openai',
      model: 'gpt-4',
      proxy: 'http://p',
      router: 'Pilot Router',
      channels: '2',
    })
  })
  it('not configured when no provider', () => {
    expect(finishSummary({}, {})).toEqual({
      provider: 'not configured',
      model: 'not configured',
      proxy: '',
      router: 'choose a provider first',
      channels: '0',
    })
  })
})

describe('readiness helpers (setup.js:1241-1288)', () => {
  it('readinessTone', () => {
    expect(readinessTone(detail({ blocking: true }), 'search')).toBe('is-warn')
    expect(readinessTone(detail({ status: 'ok' }), 'search')).toBe('is-ok')
    expect(readinessTone(detail({ status: 'optional' }), 'search')).toBe('is-muted')
  })
  it('router-needs-provider special case', () => {
    const d = detail({ status: 'ok', detail: 'uses Pilot Router after provider setup' })
    expect(readinessTone(d, 'router')).toBe('is-warn')
    expect(readinessStatusLabel(d, 'router')).toBe('Provider first')
    expect(setupStepForSection('router', d)).toBe('provider')
  })
  it('setupStepForSection maps sections (audio omitted → "" per legacy setup.js:1246)', () => {
    expect(setupStepForSection('llm', detail({}))).toBe('provider')
    expect(setupStepForSection('channels', detail({}))).toBe('channels')
    expect(setupStepForSection('search', detail({}))).toBe('extras')
    expect(setupStepForSection('memory_embedding', detail({}))).toBe('extras')
    // Legacy _setupStepForSection lists only search/image_generation/memory_embedding
    // for extras — audio falls through to '' (a legacy quirk kept for 1:1 fidelity;
    // the Finish readiness action button is simply omitted for the audio row).
    expect(setupStepForSection('audio', detail({}))).toBe('')
    expect(setupStepForSection('unknown', detail({}))).toBe('')
  })
})

describe('capability badge / primary (setup.js:959-969)', () => {
  it('capabilityIsPrimary when detail needs action', () => {
    const status: OnboardingStatus = { sectionDetails: { search: detail({ blocking: true }) } }
    expect(capabilityIsPrimary(status, 'search')).toBe(true)
    expect(capabilityIsPrimary({}, 'search')).toBe(false)
  })
  it('capabilityBadge tone + label', () => {
    const status: OnboardingStatus = { sectionDetails: { audio: detail({ status: 'ok' }) } }
    expect(capabilityBadge(status, 'audio')).toEqual({ tone: 'is-ok', label: 'Ready' })
  })
})
