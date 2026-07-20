// Pure config-view helpers ported 1:1 from the legacy view
// (src/agentos/gateway/static/js/views/config.js). Each function carries the
// legacy line range it mirrors so the parity matrix stays auditable. RPC calls,
// rendering, tooltips, mode/tab UI and the sticky save bar live in ConfigPage;
// this module owns the pure derivations that drive the write surface: tab
// filtering, object flattening, grouping, the dotted-key value lookup, the
// dirty/no-op diff (the core of the sticky bar + save gating), value parsing +
// JSON validation, YAML serialisation and the diff/summary formatting.

/** The raw config object returned by config.get (a nested JSON tree). */
export type ConfigData = Record<string, unknown>

/** A single flattened leaf entry: [dotted key, value]. */
export type Entry = [string, unknown]

/** One editor section (form tab), matched to top-level keys by prefix. */
export interface TabDef {
  id: string
  label: string
  prefixes: string[]
}

// config.js:22-29 — the six form tabs and the top-level prefixes each collects.
export const TABS: readonly TabDef[] = [
  {
    id: 'core',
    label: 'Core',
    prefixes: ['general', 'auth', 'host', 'port', 'version', 'debug', 'control_ui', 'diagnostics'],
  },
  {
    id: 'ai',
    label: 'AI & Agents',
    prefixes: [
      'provider',
      'model',
      'agent',
      'llm',
      'skills',
      'agentos_router',
      'prompt_cache',
      'thinking',
    ],
  },
  { id: 'memory', label: 'Memory', prefixes: ['memory'] },
  {
    id: 'communication',
    label: 'Communication',
    prefixes: ['channel', 'telegram', 'slack', 'discord', 'email', 'messaging'],
  },
  { id: 'automation', label: 'Automation', prefixes: ['cron', 'scheduler'] },
  {
    id: 'infrastructure',
    label: 'Infrastructure',
    prefixes: ['log', 'storage', 'db', 'cache', 'search'],
  },
]

// config.js:32-125 — per-field help, keyed by config path. Falls back to a
// generic message (config.js:127-130).
const HELP: Record<string, string> = {
  host: 'Network interface the gateway binds to. Read-only here — set via agentos gateway run --bind (CLI only). Defaults to 127.0.0.1 (loopback); 0.0.0.0 exposes on all interfaces and requires auth.',
  port: 'TCP port for the ASGI gateway. Read-only here — set via agentos gateway run --port (CLI only). Default 18791; the WebSocket and REST endpoints share it.',
  debug:
    'Security-sensitive developer mode. Auth scope expansion can take effect immediately for new connections; Starlette debug, uvicorn log level, and some startup wiring need a gateway restart. Keep it off in shared deployments.',
  diagnostics_enabled:
    'Default standard diagnostics mode at gateway startup. Raw turn-call capture stays off unless AGENTOS_TURN_CALL_LOG=1 or the running gateway is switched with agentos diagnostics on --raw.',
  log_file_enabled:
    'Writes gateway debug.log records for operator troubleshooting. This is separate from raw turn-call capture, which requires AGENTOS_TURN_CALL_LOG=1 or agentos diagnostics on --raw.',
  log_level: 'Minimum gateway file log level. AGENTOS_LOG_LEVEL can override this at runtime.',
  log_file_max_bytes:
    'Maximum debug.log size before rotation. Set to 0 to disable rotation in the stdlib handler.',
  log_file_backup_count: 'Number of rotated debug.log backups to retain.',
  'agent_token_saving.tool_result_projection_max_inline_chars':
    'Maximum inline size for canonical tokenjuice tool-result projections. Raw tool output is transient and is not stored.',
  'agentos_router.enabled':
    'Turn the auto tier router on or off. When off, every request uses the default model regardless of complexity.',
  'agentos_router.rollout_phase':
    'Rollout stage for new router model versions. Higher phases enable more aggressive routing decisions.',
  'agentos_router.strategy':
    '"pilot-v1" (default) classifies each turn with the local Pilot ML router (MiniLM+ONNX bundle, no LLM call); "llm_judge" classifies via a small LLM call instead. The pilot bundle ships in the wheel and degrades to the default tier if absent.',
  'agentos_router.judge_model':
    'Explicit LLM-judge model. Leave unset for Auto: the judge follows the tier profile’s cheapest text tier (c0 first), so profile switches auto-update it.',
  'agentos_router.judge_provider':
    'Optional provider for judge_model. Must match llm.provider — tier entries carry no credentials, so a cross-provider judge has no credential source.',
  'agentos_router.judge_base_url':
    'Local OpenAI-compatible judge endpoint (Ollama / LM Studio / llama.cpp / vLLM). Only takes effect when judge_model is set; the judge client is then built against this base URL with judge_api_key, bypassing the provider-match constraint (a local endpoint needs no cloud credentials).',
  'agentos_router.judge_api_key':
    'API key for the local judge endpoint (judge_base_url). Optional — local endpoints usually accept any token; a placeholder is used when unset. Redacted in logs.',
  'agentos_router.judge_input_max_chars':
    'Character budget for the message body sent to the judge (head/tail truncation with an elision marker). Signals are computed before truncation.',
  'agentos_router.judge_short_circuit_enabled':
    'Skip the judge call for trivial short greetings/acknowledgements (exact allowlist match) and route them to the cheapest tier directly.',
  'agentos_router.judge_short_circuit_allowlist':
    'Extra exact greeting/ack phrases (case-insensitive) that skip the judge. These are ADDED to the built-in default allowlist (en/vi/zh), not a replacement — leave empty to use just the defaults.',
  'memory.embedding':
    'Long-term memory embedding provider. Auto mode prefers a downloaded EmbeddingGemma model, then the bundled BGE ONNX, then a configured remote key, then FTS-only. Run `agentos memory embedding-download` to fetch the EmbeddingGemma upgrade; switching the local model triggers a full reindex. Remote embeddings require explicit memory embedding configuration.',
  'memory.embedding.provider':
    'Canonical memory embedding provider: auto, none, local, openai/openai-compatible, or ollama. This is independent from the chat LLM provider.',
  'memory.embedding.remote.api_key':
    'API key for the memory embedding endpoint. This does not inherit the chat/OpenRouter key in auto mode.',
  'memory.embedding.remote.base_url':
    'OpenAI-compatible API root for memory indexing, for example https://api.openai.com/v1. The provider appends /embeddings.',
  'memory.embedding.local.model':
    'Optional local embedding model id to pin. Leave empty for auto (a downloaded EmbeddingGemma export when present, otherwise the bundled BGE-small). Set "google/embeddinggemma-300m" or "BAAI/bge-small-zh-v1.5" to force one. Changing this triggers a full reindex.',
  'memory.embedding.local.onnx_dir':
    'Optional ONNX directory for a custom local embedding model. Leave empty to use the resolved model’s export (downloaded EmbeddingGemma or bundled BGE-small).',
  'memory.retrieval_mode':
    'Memory retrieval mode. "hybrid" uses vectors when an embedding provider is available; "fts_only" disables vectors.',
  'memory.curated_memory_char_limit':
    'Character budget for MEMORY.md, the agent’s curated notes file. When full, the agent consolidates existing entries via the memory tool instead of growing the file further.',
  'memory.curated_user_char_limit': 'Character budget for USER.md, the curated user profile file.',
  'memory.inject_limit':
    'Cap on the combined curated MEMORY.md + USER.md blocks injected into every system prompt. Keep it above the sum of the two char-limit budgets plus roughly 310 chars of header/separator overhead, or the user-profile block is dropped whole to stay under budget.',
  'memory.provider.name':
    'Optional external memory provider layered on top of built-in memory. Empty (the default) keeps built-in memory only; "mem0" enables the mem0 provider (prompt recall block, fenced recall, per-turn sync, write mirror). The provider is built once at boot, so changing this requires a gateway restart. mem0 needs the extra: pip install "use-agent-os[mem0]".',
  'memory.provider.mem0.llm_provider':
    'Backend the mem0 provider uses for its extraction/summarization LLM. Defaults to "ollama" for a fully local stack. Requires a gateway restart.',
  'memory.provider.mem0.llm_model':
    'mem0 extraction/summarization model. Default "qwen3:4b" (a small local Ollama model). Requires a gateway restart.',
  'memory.provider.mem0.llm_base_url':
    'Base URL for the mem0 LLM backend. Defaults to the local Ollama endpoint http://localhost:11434. Requires a gateway restart.',
  'memory.provider.mem0.embedder_provider':
    'Backend for mem0 embeddings. Defaults to "ollama" so embeddings stay local. Requires a gateway restart.',
  'memory.provider.mem0.embedder_model':
    'mem0 embedding model. Default "embeddinggemma" (local via Ollama). Requires a gateway restart.',
  'memory.provider.mem0.embedder_base_url':
    'Base URL for the mem0 embedder backend. Defaults to the local Ollama endpoint http://localhost:11434. Requires a gateway restart.',
  'memory.provider.mem0.vector_store_path':
    'On-disk directory for the mem0 vector store. Empty resolves to <agent state dir>/mem0 at boot, keeping all data local. Requires a gateway restart.',
  'sandbox.sandbox':
    'Runtime sandbox switch. The out-of-box posture keeps this false; use agentos sandbox on|bypass|full to change sandbox and permission defaults together.',
  'sandbox.security_grading':
    'Risk grading and approval gate for tool actions. Keep this paired with sandbox.sandbox unless using the sandbox CLI posture commands.',
  'permissions.default_mode':
    'Default owner/operator permission mode: bypass is the out-of-box local posture, off keeps sandboxed execution, on uses host execution with approvals, and full bypasses sensitive-path gates too.',
  'prompt_cache.mode':
    'Anthropic prompt cache control. "auto" (default) lets the provider decide; "on" forces caching; "off" disables it entirely.',
  context_budget_tokens:
    'Soft cap on the assembled prompt size. When exceeded, the configured overflow policy kicks in (summarize, truncate, or refuse).',
  context_overflow_policy:
    '"auto_summarize" compacts older history via a small LLM; "hard_truncate" drops oldest turns; "refuse" rejects the turn with a stable error.',
  auth_mode:
    'Gateway auth scheme. "token" requires a static bearer token; "none" is open (loopback only); other modes per deployment.',
  'auth.allow_unauthenticated_public':
    'Break-glass opt-in. By default the gateway refuses to start with auth.mode "none" on a non-loopback bind; enabling this serves anyway, giving every peer that can reach the port full operator access. Only enable behind a reverse proxy with auth, VPN, or firewall.',
  'control_ui.allowed_origins':
    'Extra browser origins allowed to open the Control UI WebSocket, call the HTTP API, and send Host headers, beyond loopback (which is always allowed). Add your reverse-proxy origin here (e.g. https://agent.example.com) when serving the UI off another host; default ports 80/443 are normalized. Cross-origin requests are otherwise rejected to block cross-site WebSocket hijacking and DNS rebinding.',
}

/** config.js:127-130 — per-field help text; generic fallback when unknown. */
export function helpFor(key: string): string {
  if (key in HELP) return HELP[key]!
  return 'No description yet — see the docs.'
}

// config.js:422 — max object levels to descend before falling back to a
// JSON-blob field. Top-level entries are depth 0, so three descents expose the
// depth-3 leaf memory.embedding.local.model; a 4th object level blobs out.
const FLATTEN_MAX_DEPTH = 3

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

/**
 * config.js:430-450 — flatten object-valued entries into dotted-key leaf
 * fields. Recurse while the value is a plain object AND depth < the limit.
 * Arrays, null, and scalars are leaves; an empty object or an object still
 * nested at the depth limit is emitted whole (the JSON-blob field renders it).
 */
export function flattenEntries(entries: Entry[]): Entry[] {
  const out: Entry[] = []
  const walk = (key: string, value: unknown, depth: number): void => {
    if (isPlainObject(value) && depth < FLATTEN_MAX_DEPTH) {
      const keys = Object.keys(value)
      if (keys.length === 0) {
        out.push([key, value]) // empty object: keep as a JSON-blob leaf
        return
      }
      keys.forEach((childKey) => walk(`${key}.${childKey}`, value[childKey], depth + 1))
      return
    }
    out.push([key, value])
  }
  entries.forEach(([k, v]) => walk(k, v, 0))
  return out
}

/** config.js:876-883 — lowercased, JSON-stringified search haystack for a value. */
export function searchBlob(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value).toLowerCase()
    } catch {
      return ''
    }
  }
  return String(value).toLowerCase()
}

/**
 * config.js:452-461 — the flattened leaf entries a tab collects: top-level keys
 * whose lowercased name matches any of the tab's prefixes (exact / prefix+dot /
 * prefix+underscore), flattened, then filtered by the (already-lowercased)
 * search text over key OR value.
 */
export function entriesForTab(config: ConfigData, tab: TabDef, searchText: string): Entry[] {
  const topLevel = Object.entries(config).filter(([k]) => {
    const lk = k.toLowerCase()
    return tab.prefixes.some((p) => lk.startsWith(p + '.') || lk === p || lk.startsWith(p + '_'))
  })
  return flattenEntries(topLevel).filter(([k, v]) => {
    if (!searchText) return true
    return k.toLowerCase().includes(searchText) || searchBlob(v).includes(searchText)
  })
}

/** A grouped bundle of leaf entries under a titled sub-section. */
export interface FieldGroup {
  id: string
  title: string
  entries: Entry[]
}

/** config.js:491-495 — the group id for a leaf: its top dotted segment, or the
 * bare key when the value is an object, else 'general'. */
export function groupIdForKey(k: string, v: unknown): string {
  if (k.includes('.')) return k.split('.')[0]!
  if (v && typeof v === 'object') return k
  return 'general'
}

/** config.js:497-500 — a group title: de-cased separators, title-cased words. */
export function groupTitle(id: string): string {
  if (id === 'general') return 'General'
  return id.replace(/[_-]/g, ' ').replace(/\b\w/g, (ch) => ch.toUpperCase())
}

/** config.js:481-489 — bundle leaf entries into ordered groups (insertion order). */
export function groupEntries(entries: Entry[]): FieldGroup[] {
  const groups = new Map<string, FieldGroup>()
  entries.forEach(([k, v]) => {
    const id = groupIdForKey(k, v)
    if (!groups.has(id)) groups.set(id, { id, title: groupTitle(id), entries: [] })
    groups.get(id)!.entries.push([k, v])
  })
  return Array.from(groups.values())
}

/** config.js:505-510 — the field label: the dotted key with its group prefix
 * stripped (a flattened leaf reads `provider.name` under Memory). The full key
 * stays as the data key for save + masking. */
export function fieldLabel(k: string, groupId: string): string {
  if (groupId && groupId !== 'general' && k.startsWith(groupId + '.')) {
    return k.slice(groupId.length + 1)
  }
  return k
}

// config.js:514 — bind posture is CLI-only: host/port render display-only and
// the RPC rejects writes anyway.
const READONLY_KEYS = new Set(['host', 'port'])

/** config.js:514,531 — is this key display-only (host/port)? */
export function isReadonlyKey(k: string): boolean {
  return READONLY_KEYS.has(k)
}

// config.js:519 — sensitive-key masking tests the FULL dotted key, so a nested
// leaf like memory.embedding.remote.api_key is still masked after flattening.
const SENSITIVE_KEY_RE = /key|token|secret|password|api_key/i

/** config.js:519 — does this key hold a secret (masked input + redacted preview)? */
export function isSensitiveKey(k: string): boolean {
  return SENSITIVE_KEY_RE.test(k)
}

/** The editor control kind for a leaf value. */
export type FieldKind = 'readonly' | 'boolean' | 'number' | 'object' | 'string'

/** config.js:516-565 — pick the editor control for a leaf: readonly (host/port)
 * wins, then boolean / number / object(JSON) by value type, else string. */
export function fieldKind(k: string, v: unknown): FieldKind {
  if (isReadonlyKey(k)) return 'readonly'
  if (typeof v === 'boolean') return 'boolean'
  if (typeof v === 'number') return 'number'
  if (typeof v === 'object' && v !== null) return 'object'
  return 'string'
}

/** config.js:629-637 — read the loaded config value at a (possibly dotted) leaf
 * key: a literal top-level key wins, else descend the dotted path. */
export function configValueAt(config: ConfigData, key: string): unknown {
  if (key in config) return config[key]
  let cur: unknown = config
  for (const part of key.split('.')) {
    if (cur !== null && typeof cur === 'object' && part in (cur as Record<string, unknown>)) {
      cur = (cur as Record<string, unknown>)[part]
    } else {
      return undefined
    }
  }
  return cur
}

/** The result of parsing a raw input value into its typed config value. */
export type ParseResult = { ok: true; value: unknown } | { ok: false }

/**
 * config.js:585-616 — coerce a raw editor value to its typed config value.
 * boolean = the checkbox state; number = Number(text); json = JSON.parse (which
 * can fail → {ok:false}, the inline "Invalid JSON" gate); string passes through.
 */
export function parseFieldValue(
  type: 'boolean' | 'number' | 'json' | 'string',
  raw: string,
): ParseResult {
  if (type === 'boolean') return { ok: true, value: raw === 'true' || raw === 'on' || raw === '1' }
  if (type === 'number') return { ok: true, value: Number(raw) }
  if (type === 'json') {
    try {
      return { ok: true, value: JSON.parse(raw) }
    } catch {
      return { ok: false }
    }
  }
  return { ok: true, value: raw }
}

/** One dirty leaf: its loaded value and the pending new value. */
export interface DirtyEntry {
  old: unknown
  new: unknown
}

/** Form-mode dirty state: dotted key → { old, new }. */
export type DirtyMap = Record<string, DirtyEntry>

/** Invalid-JSON state: dotted key → true (blocks save while non-empty). */
export type InvalidJsonMap = Record<string, true>

/** The outcome of diffing a pending value against the loaded config. */
export type DirtyResult = { dirty: false } | { dirty: true; old: unknown; new: unknown }

/**
 * config.js:606-612 — THE dirty/no-op derivation. Diff a pending leaf value
 * against the loaded config value: equal by reference OR by structural JSON is
 * a no-op (clears the dirty entry); anything else is dirty and carries the
 * old + new for the diff view. This is what gates the sticky bar and the save.
 */
export function computeDirty(config: ConfigData, key: string, newVal: unknown): DirtyResult {
  const oldVal = configValueAt(config, key)
  if (newVal === oldVal || JSON.stringify(newVal) === JSON.stringify(oldVal)) {
    return { dirty: false }
  }
  return { dirty: true, old: oldVal, new: newVal }
}

/** config.js:671 — the number of pending dirty leaves. */
export function dirtyCount(dirty: DirtyMap): number {
  return Object.keys(dirty).length
}

/** config.js:735 — is there any invalid-JSON leaf? (blocks form-mode save). */
export function hasInvalidJson(invalid: InvalidJsonMap): boolean {
  return Object.keys(invalid).length > 0
}

/** config.js:739 — the config.patch payload: { patches: { dottedKey: newValue } }. */
export function buildPatchPayload(dirty: DirtyMap): { patches: Record<string, unknown> } {
  const patches: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(dirty)) patches[k] = v.new
  return { patches }
}

/** config.js:731 — the config.apply payload: the edited YAML + the loaded
 * baseline the operator was shown (so the server diffs & persists only edits). */
export function buildApplyPayload(
  configYaml: string,
  baselineYaml: string,
): { config_yaml: string; baseline_yaml: string } {
  return { config_yaml: configYaml, baseline_yaml: baselineYaml }
}

const SECRET_KEY_RE = /token|key|secret|password/i
const STR_TRUNC = 40

/** config.js:843-855 — a compact, secret-redacted preview of a value for the
 * object-field summary line. */
export function formatPreviewValue(key: string, value: unknown): string {
  if (SECRET_KEY_RE.test(key)) return '"***"'
  if (value === null) return 'null'
  if (value === undefined) return 'undefined'
  if (typeof value === 'boolean' || typeof value === 'number') return String(value)
  if (typeof value === 'string') {
    const trimmed = value.length > STR_TRUNC ? value.slice(0, STR_TRUNC - 1) + '…' : value
    return JSON.stringify(trimmed)
  }
  if (Array.isArray(value)) return `[${value.length}]`
  if (typeof value === 'object') return `{${Object.keys(value).length}}`
  return JSON.stringify(value)
}

/** config.js:857-874 — the one-line "JSON · …" summary shown on a collapsed
 * object/array field (with a redacted key/value preview). */
export function objectSummary(value: unknown): string {
  if (Array.isArray(value)) {
    const len = value.length
    if (len === 0) return 'JSON · empty list'
    const preview = value
      .slice(0, 2)
      .map((v) => formatPreviewValue('item', v))
      .join(', ')
    const more = len > 2 ? ', …' : ''
    return `JSON · ${len} ${len === 1 ? 'item' : 'items'} · [${preview}${more}]`
  }
  if (value && typeof value === 'object') {
    const keys = Object.keys(value as Record<string, unknown>)
    if (keys.length === 0) return 'JSON · empty object'
    const previewKeys = keys.slice(0, 2)
    const parts = previewKeys.map(
      (k) => `${k}: ${formatPreviewValue(k, (value as Record<string, unknown>)[k])}`,
    )
    const more = keys.length > previewKeys.length ? ', …' : ''
    return `JSON · ${keys.length} ${keys.length === 1 ? 'key' : 'keys'} · {${parts.join(', ')}${more}}`
  }
  return 'JSON · value'
}

/** config.js:707-711 — a JSON-encoded, 120-char-truncated diff value for the
 * sticky-bar diff rows. */
export function summariseDiffValue(v: unknown): string {
  const s = JSON.stringify(v)
  if (s === undefined) return String(v)
  return s.length > 120 ? s.slice(0, 117) + '…' : s
}

/** config.js:886-912 — a minimal, dependency-free object→YAML serialiser (the
 * YAML-mode baseline text). Scalars inline; structural strings get JSON-quoted;
 * objects and lists render as indented blocks; empty collections inline. */
export function objToYaml(obj: unknown, indent = 0): string {
  const pad = '  '.repeat(indent)
  if (obj === null || obj === undefined) return 'null'
  if (typeof obj === 'boolean') return String(obj)
  if (typeof obj === 'number') return String(obj)
  if (typeof obj === 'string') {
    if (/[\n:#[\]{}&*!|>'"%@`]/.test(obj) || obj.trim() !== obj) {
      return JSON.stringify(obj)
    }
    return obj
  }
  if (Array.isArray(obj)) {
    if (obj.length === 0) return '[]'
    return '\n' + obj.map((item) => pad + '- ' + objToYaml(item, indent + 1)).join('\n')
  }
  if (typeof obj === 'object') {
    const keys = Object.keys(obj as Record<string, unknown>)
    if (keys.length === 0) return '{}'
    return (
      '\n' +
      keys
        .map((k) => {
          const val = (obj as Record<string, unknown>)[k]
          const rendered = objToYaml(val, indent + 1)
          const inline = typeof val !== 'object' || val === null
          return pad + k + ': ' + (inline ? rendered : rendered.trimStart())
        })
        .join('\n')
    )
  }
  return String(obj)
}
