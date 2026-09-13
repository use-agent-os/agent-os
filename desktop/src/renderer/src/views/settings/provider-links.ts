/**
 * Where a person gets an API key, per provider. Only the pages we are sure
 * of; a provider without an entry simply shows no link. Kept out of the
 * catalog on purpose: these are product pages that move, not gateway facts.
 */
export const PROVIDER_KEY_URLS: Readonly<Record<string, string>> = {
  openai: 'https://platform.openai.com/api-keys',
  anthropic: 'https://console.anthropic.com/settings/keys',
  openrouter: 'https://openrouter.ai/keys',
  deepseek: 'https://platform.deepseek.com/api_keys',
  gemini: 'https://aistudio.google.com/apikey',
  moonshot: 'https://platform.moonshot.cn/console/api-keys',
  zhipu: 'https://open.bigmodel.cn/usercenter/apikeys',
  dashscope: 'https://bailian.console.aliyun.com/?apiKey=1',
  volcengine: 'https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey',
  qianfan: 'https://console.bce.baidu.com/qianfan/ais/console/apiKey',
}

export function providerKeyUrl(providerId: string): string | null {
  return PROVIDER_KEY_URLS[providerId] ?? null
}
