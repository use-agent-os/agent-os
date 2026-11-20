export const capabilities = {
  'settings.capabilities.stats': 'Capabilities at a glance',
  'settings.capabilities.stat.search': 'Web search',
  'settings.capabilities.stat.image': 'Images',
  'settings.capabilities.stat.voice': 'Voice',
  'settings.capabilities.stat.x': 'Search on X',
  'settings.capabilities.stat.x.value': 'Grok',
  'settings.capabilities.stat.login': 'SuperGrok login',
  'settings.capabilities.stat.off': 'Off',
  'settings.capabilities.stat.noKey': 'No key needed',
  'settings.capabilities.stat.keyReady': 'Key in place',
  'settings.capabilities.stat.keyMissing': 'Key missing',
  'settings.capabilities.stat.offHint': 'Not in use',

  'settings.capabilities.state.on': 'On',
  'settings.capabilities.state.off': 'Off',
  'settings.capabilities.state.key': 'Needs a key',

  'settings.capabilities.search.title': 'Web search',
  'settings.capabilities.search.blurb':
    'Lets the agent look things up on the web and read what it finds.',
  'settings.capabilities.x.title': 'Search on X',
  'settings.capabilities.x.blurb':
    'Lets the agent search posts on X through Grok, with citations. Needs an xAI key or a SuperGrok login.',
  'settings.capabilities.image.title': 'Image generation',
  'settings.capabilities.image.blurb': 'Lets the agent draw images from a description.',
  'settings.capabilities.voice.title': 'Voice',
  'settings.capabilities.voice.blurb':
    'Lets the agent speak its replies, transcribe audio and work with voices.',

  'settings.capabilities.enable': 'Turn on',
  'settings.capabilities.image.off':
    'Off. Turn it on to pick a service; OpenAI and OpenRouter both work with a key you may already have.',
  'settings.capabilities.voice.off':
    'Off. Turn it on to connect a voice service and give the agent a voice.',
  'settings.capabilities.x.off': 'Off. The agent will not search X.',

  'settings.capabilities.provider': 'Service',
  'settings.capabilities.tag.free': 'Free · no key',
  'settings.capabilities.tag.keyReady': 'Key ready',
  'settings.capabilities.tag.needsKey': 'Needs a key',

  'settings.capabilities.blurb.search.brave':
    'Brave’s own independent index. Fast and private; a free tier with a key.',
  'settings.capabilities.blurb.search.duckduckgo': 'Works right away. No account or key.',
  'settings.capabilities.blurb.search.tavily':
    'Search made for AI agents. Returns clean extracts of each page.',
  'settings.capabilities.blurb.search.exa': 'Finds pages by meaning rather than keywords.',
  'settings.capabilities.blurb.search.perplexity': 'Answers with sources, from Perplexity.',
  'settings.capabilities.blurb.image.openai': 'GPT Image models, straight from OpenAI.',
  'settings.capabilities.blurb.image.openrouter':
    'Gemini and other image models through one OpenRouter key.',
  'settings.capabilities.blurb.voice.elevenlabs':
    'Natural speech, transcription, voice changing and music.',

  'settings.capabilities.field.api_key': 'API key',
  'settings.capabilities.field.api_key_env': 'Read the key from',
  'settings.capabilities.field.api_key_env.help':
    'The environment variable that holds the key. Ignored when you paste a key above.',
  'settings.capabilities.field.max_results': 'Results per search',
  'settings.capabilities.field.max_results.help': 'How many results the agent reads each time.',
  'settings.capabilities.field.primary': 'Model',
  'settings.capabilities.field.primary.help': 'The image model, written as service/model.',
  'settings.capabilities.field.model': 'Grok model',
  'settings.capabilities.field.base_url': 'Server URL',
  'settings.capabilities.field.base_url.help': 'Change only for a proxy or a compatible server.',
  'settings.capabilities.field.proxy': 'HTTP proxy',
  'settings.capabilities.field.use_env_proxy': 'Use the proxy from the environment',
  'settings.capabilities.field.fallback_policy': 'When the service fails',
  'settings.capabilities.field.fallback_policy.off': 'Show the error',
  'settings.capabilities.field.fallback_policy.network': 'Retry with DuckDuckGo',
  'settings.capabilities.field.diagnostics': 'Include error details in results',
  'settings.capabilities.field.tts_voice': 'Voice ID',
  'settings.capabilities.field.tts_model': 'Speech model',
  'settings.capabilities.field.language_code': 'Language',
  'settings.capabilities.field.reasoning_effort': 'Reasoning effort',
  'settings.capabilities.field.reasoning_effort.default': 'Model default',
  'settings.capabilities.field.timeout_seconds': 'Time per attempt (seconds)',
  'settings.capabilities.field.total_timeout_seconds': 'Total time limit (seconds)',
  'settings.capabilities.field.retries': 'Retries',
  'settings.capabilities.optionDefault': 'Default',

  'settings.capabilities.key.env': '{key} is already set, so you can leave this empty.',
  'settings.capabilities.key.stored': 'A key is saved. Paste a new one to replace it.',
  'settings.capabilities.key.login': 'Signed in with SuperGrok, so no key is needed.',
  'settings.capabilities.key.provider': 'Uses the key of your model provider.',
  'settings.capabilities.key.missing': 'Paste a key, or set {key} in Environment.',
  'settings.capabilities.key.placeholder.keep': 'Leave empty to keep it',
  'settings.capabilities.key.placeholder.new': 'Paste your key',
  'settings.capabilities.key.show': 'Show key',
  'settings.capabilities.key.hide': 'Hide key',
  'settings.capabilities.key.get': 'Get a key',
  'settings.capabilities.key.openEnv': 'Open Environment',
  'settings.capabilities.x.loginHint': 'Or sign in with SuperGrok from a terminal:',

  'settings.capabilities.advanced': 'Advanced',
  'settings.capabilities.advanced.hint': '{n} settings',

  'settings.capabilities.problem.key': 'Add a key above before saving.',
  'settings.capabilities.problem.range': '{field} must be between {min} and {max}.',
  'settings.capabilities.problem.rangeSeconds': '{field} must be between {min} and {max} seconds.',
  'settings.capabilities.problem.modelPrefix': 'The model must start with {prefix}',
  'settings.capabilities.problem.number': '{field} must be a number.',
  'settings.capabilities.problem.whole': '{field} must be a whole number.',
  'settings.capabilities.problem.required': '{field} is required.',

  'settings.capabilities.saved.search': 'Web search saved',
  'settings.capabilities.saved.x': 'Search on X saved',
  'settings.capabilities.saved.image': 'Image generation saved',
  'settings.capabilities.saved.voice': 'Voice saved',
  'settings.capabilities.unknownProvider':
    'The saved service ({id}) is not one this app can set up. Saving switches to the one you pick.',
  'settings.capabilities.noProviders': 'The gateway offers no service for this yet.',
  'settings.capabilities.retry': 'Retry',
} as const
