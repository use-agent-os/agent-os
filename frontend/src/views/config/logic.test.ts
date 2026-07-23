import { describe, expect, it } from 'vitest'
import {
  buildApplyPayload,
  buildPatchPayload,
  computeDirty,
  configValueAt,
  dirtyCount,
  entriesForTab,
  fieldKind,
  fieldLabel,
  flattenEntries,
  groupEntries,
  groupTitle,
  hasInvalidJson,
  helpFor,
  isReadonlyKey,
  isSensitiveKey,
  objectSummary,
  objToYaml,
  parseFieldValue,
  searchBlob,
  summariseDiffValue,
  TABS,
  type DirtyMap,
} from './logic'

// ── flattenEntries — config.js:430-450 ──────────────────────────────────────
describe('flattenEntries', () => {
  it('leaves scalars, arrays and null as leaves', () => {
    const out = flattenEntries([
      ['host', '127.0.0.1'],
      ['port', 18791],
      ['debug', false],
      ['nada', null],
      ['list', [1, 2, 3]],
    ])
    expect(out).toEqual([
      ['host', '127.0.0.1'],
      ['port', 18791],
      ['debug', false],
      ['nada', null],
      ['list', [1, 2, 3]],
    ])
  })

  it('flattens nested objects into dotted leaf keys', () => {
    const out = flattenEntries([['memory', { retrieval_mode: 'hybrid', inject_limit: 4000 }]])
    expect(out).toEqual([
      ['memory.retrieval_mode', 'hybrid'],
      ['memory.inject_limit', 4000],
    ])
  })

  it('descends up to depth 3 then blobs the object whole', () => {
    // memory.embedding.local.model is depth-3 leaf → flattened.
    const cfg = { memory: { embedding: { local: { model: 'bge' } } } }
    const out = flattenEntries(Object.entries(cfg))
    expect(out).toEqual([['memory.embedding.local.model', 'bge']])
  })

  it('emits a depth-4 object whole as a JSON-blob leaf', () => {
    // memory.embedding.local.deep is one level past the limit → stays an object.
    const cfg = { memory: { embedding: { local: { deep: { x: 1 } } } } }
    const out = flattenEntries(Object.entries(cfg))
    expect(out).toEqual([['memory.embedding.local.deep', { x: 1 }]])
  })

  it('keeps an empty object as a JSON-blob leaf', () => {
    const out = flattenEntries([['channels', {}]])
    expect(out).toEqual([['channels', {}]])
  })
})

// ── entriesForTab — config.js:452-461 ───────────────────────────────────────
describe('entriesForTab', () => {
  const core = TABS.find((t) => t.id === 'core')!
  const ai = TABS.find((t) => t.id === 'ai')!

  it('matches exact prefix, prefix+dot, and prefix+underscore', () => {
    const cfg = {
      host: '127.0.0.1', // exact 'host'
      debug: true, // exact 'debug'
      control_ui: { allowed_origins: [] }, // 'control_ui' exact → flattened
      diagnostics_enabled: false, // 'diagnostics' + '_'
      provider: 'openai', // NOT core (ai)
    }
    const keys = entriesForTab(cfg, core, '').map(([k]) => k)
    expect(keys).toContain('host')
    expect(keys).toContain('debug')
    expect(keys).toContain('control_ui.allowed_origins')
    expect(keys).toContain('diagnostics_enabled')
    expect(keys).not.toContain('provider')
  })

  it('routes provider/model/agent keys to the AI tab', () => {
    const cfg = { provider: 'openai', model: 'gpt', memory: { x: 1 } }
    const keys = entriesForTab(cfg, ai, '').map(([k]) => k)
    expect(keys).toEqual(expect.arrayContaining(['provider', 'model']))
    expect(keys).not.toContain('memory.x')
  })

  it('filters by search over key AND value (case-insensitive)', () => {
    const cfg = { host: '127.0.0.1', debug: true }
    // search the value 127
    expect(entriesForTab(cfg, core, '127').map(([k]) => k)).toEqual(['host'])
    // search the key
    expect(entriesForTab(cfg, core, 'debug').map(([k]) => k)).toEqual(['debug'])
    // no match
    expect(entriesForTab(cfg, core, 'zzz')).toEqual([])
  })

  it('classifies every current GatewayConfig top-level field exactly once', () => {
    const fields = [
      'tls',
      'host',
      'port',
      'version',
      'debug',
      'log_file_enabled',
      'log_level',
      'log_file_max_bytes',
      'log_file_backup_count',
      'workspace_dir',
      'workspace_strict',
      'bootstrap_max_chars',
      'bootstrap_total_max_chars',
      'auth',
      'cors',
      'attachments',
      'rate_limit',
      'tools',
      'permissions',
      'task_runtime',
      'skills',
      'llm',
      'prompt_cache',
      'safety',
      'prompt',
      'memory',
      'agentos_router',
      'agent_token_saving',
      'compaction',
      'mcp',
      'heartbeat',
      'image_generation',
      'audio',
      'sandbox',
      'channels',
      'agents',
      'agents_defaults',
      'subagents',
      'updates',
      'control_ui',
      'diagnostics_enabled',
      'channel_admin_senders',
      'context_budget_tokens',
      'context_overflow_policy',
      'preflight_compact_ratio',
      'agent_runtime_timeout_seconds',
      'agent_iteration_timeout_seconds',
      'agent_tool_timeout_seconds',
      'agent_request_timeout_seconds',
      'agent_max_provider_retries',
      'agent_max_iterations',
      'llm_request_timeout_seconds',
      'agent_stream_heartbeat_interval_seconds',
      'agent_stream_idle_timeout_seconds',
      'webui_stream_idle_grace_seconds',
      'client_ws_keepalive_timeout_s',
      'ws_writer_queue_enabled',
      'ws_writer_queue_maxsize',
      'llm_timeout_seconds',
      'search_provider',
      'search_api_key',
      'search_api_key_env',
      'search_max_results',
      'search_proxy',
      'search_use_env_proxy',
      'search_fallback_policy',
      'search_diagnostics',
      'state_dir',
      'config_path',
    ]
    const config = Object.fromEntries(fields.map((field) => [field, null]))

    for (const field of fields) {
      const owners = TABS.filter((tab) =>
        entriesForTab(config, tab, '').some(([key]) => key === field),
      )
      expect(
        owners.map((tab) => tab.id),
        field,
      ).toHaveLength(1)
    }
  })
})

// ── grouping — config.js:481-510 ────────────────────────────────────────────
describe('groupEntries / groupTitle / fieldLabel', () => {
  it('groups dotted keys under their top segment; bare scalars under General', () => {
    const groups = groupEntries([
      ['memory.retrieval_mode', 'hybrid'],
      ['memory.inject_limit', 4000],
      ['debug', true],
    ])
    const byId = Object.fromEntries(groups.map((g) => [g.id, g]))
    expect(byId.memory!.entries).toHaveLength(2)
    expect(byId.general!.entries).toEqual([['debug', true]])
  })

  it('groups a bare object key under its own id', () => {
    const groups = groupEntries([['channels', { a: 1 }]])
    expect(groups[0]!.id).toBe('channels')
  })

  it('titles ids by de-casing separators', () => {
    expect(groupTitle('general')).toBe('General')
    expect(groupTitle('agentos_router')).toBe('AgentOS Router')
    expect(groupTitle('control-ui')).toBe('Control UI')
    expect(groupTitle('mcp_api')).toBe('MCP API')
  })

  it('strips the group prefix from the field label but not from general', () => {
    expect(fieldLabel('memory.provider.name', 'memory')).toBe('provider.name')
    expect(fieldLabel('debug', 'general')).toBe('debug')
    expect(fieldLabel('debug', 'debug')).toBe('debug')
  })
})

// ── configValueAt — config.js:629-637 ───────────────────────────────────────
describe('configValueAt', () => {
  const cfg = {
    host: '127.0.0.1',
    memory: { embedding: { local: { model: 'bge' } }, inject_limit: 4000 },
    'dotted.key': 'literal',
  }
  it('reads a top-level key', () => {
    expect(configValueAt(cfg, 'host')).toBe('127.0.0.1')
  })
  it('prefers a literal dotted top-level key over path descent', () => {
    expect(configValueAt(cfg, 'dotted.key')).toBe('literal')
  })
  it('descends a dotted path', () => {
    expect(configValueAt(cfg, 'memory.embedding.local.model')).toBe('bge')
    expect(configValueAt(cfg, 'memory.inject_limit')).toBe(4000)
  })
  it('returns undefined for a missing path', () => {
    expect(configValueAt(cfg, 'memory.nope')).toBeUndefined()
    expect(configValueAt(cfg, 'memory.inject_limit.deeper')).toBeUndefined()
  })
})

// ── parseFieldValue — config.js:585-616 ─────────────────────────────────────
describe('parseFieldValue', () => {
  it('coerces booleans', () => {
    expect(parseFieldValue('boolean', 'true')).toEqual({ ok: true, value: true })
    expect(parseFieldValue('boolean', '')).toEqual({ ok: true, value: false })
  })
  it('coerces numbers', () => {
    expect(parseFieldValue('number', '42')).toEqual({ ok: true, value: 42 })
    expect(parseFieldValue('number', '3.5')).toEqual({ ok: true, value: 3.5 })
  })
  it('parses valid JSON', () => {
    expect(parseFieldValue('json', '{"a":1}')).toEqual({ ok: true, value: { a: 1 } })
    expect(parseFieldValue('json', '[1,2]')).toEqual({ ok: true, value: [1, 2] })
  })
  it('flags invalid JSON without a value', () => {
    expect(parseFieldValue('json', '{bad')).toEqual({ ok: false })
  })
  it('passes strings through', () => {
    expect(parseFieldValue('string', 'hello')).toEqual({ ok: true, value: 'hello' })
  })
})

// ── computeDirty — config.js:585-616 (THE dirty/no-op derivation) ────────────
describe('computeDirty', () => {
  const cfg = {
    debug: false,
    port: 18791,
    memory: { inject_limit: 4000, embedding: { local: { model: 'bge' } } },
    tags: ['a', 'b'],
  }

  it('a change from the loaded value is dirty (carries old+new)', () => {
    expect(computeDirty(cfg, 'debug', true)).toEqual({ dirty: true, old: false, new: true })
  })

  it('setting a value back to the loaded value is a no-op (not dirty)', () => {
    expect(computeDirty(cfg, 'debug', false)).toEqual({ dirty: false })
    expect(computeDirty(cfg, 'port', 18791)).toEqual({ dirty: false })
  })

  it('number edits diff by value', () => {
    expect(computeDirty(cfg, 'port', 9000)).toEqual({ dirty: true, old: 18791, new: 9000 })
  })

  it('nested dotted leaf edits diff against the descended value', () => {
    expect(computeDirty(cfg, 'memory.embedding.local.model', 'gemma')).toEqual({
      dirty: true,
      old: 'bge',
      new: 'gemma',
    })
    expect(computeDirty(cfg, 'memory.embedding.local.model', 'bge')).toEqual({ dirty: false })
  })

  it('object/array values diff structurally (JSON), not by reference', () => {
    // a fresh array equal in content is a no-op
    expect(computeDirty(cfg, 'tags', ['a', 'b'])).toEqual({ dirty: false })
    // reordering IS a change
    expect(computeDirty(cfg, 'tags', ['b', 'a'])).toEqual({
      dirty: true,
      old: ['a', 'b'],
      new: ['b', 'a'],
    })
    // added key
    expect(computeDirty(cfg, 'memory', { inject_limit: 4000 })).toMatchObject({ dirty: true })
  })

  it('type changes (string→number) are dirty even when loosely equal', () => {
    // loaded '5' vs new number 5: JSON differs ("5" vs 5) → dirty
    const c = { n: '5' }
    expect(computeDirty(c, 'n', 5)).toEqual({ dirty: true, old: '5', new: 5 })
  })

  it('a key absent from the loaded config is dirty when set to any value', () => {
    expect(computeDirty(cfg, 'brand.new', 'x')).toEqual({ dirty: true, old: undefined, new: 'x' })
  })
})

// ── dirty map aggregation ───────────────────────────────────────────────────
describe('dirtyCount / buildPatchPayload / hasInvalidJson', () => {
  const dirty: DirtyMap = {
    debug: { old: false, new: true },
    'memory.inject_limit': { old: 4000, new: 5000 },
  }

  it('counts dirty keys', () => {
    expect(dirtyCount(dirty)).toBe(2)
    expect(dirtyCount({})).toBe(0)
  })

  it('builds the config.patch patches payload (dotted-key → new value)', () => {
    expect(buildPatchPayload(dirty)).toEqual({
      patches: { debug: true, 'memory.inject_limit': 5000 },
    })
  })

  it('hasInvalidJson reflects any invalid-JSON entry', () => {
    expect(hasInvalidJson({})).toBe(false)
    expect(hasInvalidJson({ 'x.y': true })).toBe(true)
  })
})

// ── buildApplyPayload — config.js:731 ───────────────────────────────────────
describe('buildApplyPayload', () => {
  it('carries the edited YAML text and the loaded baseline', () => {
    expect(buildApplyPayload('a: 1\n', 'a: 0\n')).toEqual({
      config_yaml: 'a: 1\n',
      baseline_yaml: 'a: 0\n',
    })
  })
})

// ── sensitive/readonly keys — config.js:514,519 ─────────────────────────────
describe('isSensitiveKey / isReadonlyKey', () => {
  it('masks key/token/secret/password/api_key on the FULL dotted key', () => {
    expect(isSensitiveKey('memory.embedding.remote.api_key')).toBe(true)
    expect(isSensitiveKey('auth.token')).toBe(true)
    expect(isSensitiveKey('some.secret')).toBe(true)
    expect(isSensitiveKey('x.password')).toBe(true)
    expect(isSensitiveKey('host')).toBe(false)
  })
  it('keeps gateway bind, config path, and runtime-owned auth credentials readonly', () => {
    expect(isReadonlyKey('host')).toBe(true)
    expect(isReadonlyKey('port')).toBe(true)
    expect(isReadonlyKey('version')).toBe(true)
    expect(isReadonlyKey('config_path')).toBe(true)
    expect(isReadonlyKey('auth.token')).toBe(true)
    expect(isReadonlyKey('auth.password')).toBe(true)
    expect(isReadonlyKey('auth.mode')).toBe(false)
    expect(isReadonlyKey('debug')).toBe(false)
  })
})

// ── fieldKind — config.js:516-565 ───────────────────────────────────────────
describe('fieldKind', () => {
  it('classifies readonly first', () => {
    expect(fieldKind('host', '0.0.0.0')).toBe('readonly')
    expect(fieldKind('port', 18791)).toBe('readonly')
    expect(fieldKind('version', '2026.7.19')).toBe('readonly')
    expect(fieldKind('config_path', '/tmp/agentos.toml')).toBe('readonly')
    expect(fieldKind('auth.token', 'runtime-secret')).toBe('readonly')
    expect(fieldKind('auth.password', 'runtime-secret')).toBe('readonly')
  })
  it('classifies by value type', () => {
    expect(fieldKind('debug', true)).toBe('boolean')
    expect(fieldKind('n', 5)).toBe('number')
    expect(fieldKind('obj', { a: 1 })).toBe('object')
    expect(fieldKind('arr', [1])).toBe('object')
    expect(fieldKind('name', 'x')).toBe('string')
    expect(fieldKind('nada', null)).toBe('string')
  })
})

// ── objectSummary / searchBlob / summariseDiffValue — config.js:707-883 ─────
describe('objectSummary', () => {
  it('summarises arrays with a preview and count', () => {
    expect(objectSummary([])).toBe('JSON · empty list')
    expect(objectSummary([1])).toBe('JSON · 1 item · [1]')
    expect(objectSummary([1, 2, 3])).toBe('JSON · 3 items · [1, 2, …]')
  })
  it('summarises objects with a key preview', () => {
    expect(objectSummary({})).toBe('JSON · empty object')
    expect(objectSummary({ a: 1, b: 2, c: 3 })).toBe('JSON · 3 keys · {a: 1, b: 2, …}')
  })
  it('redacts secret-looking keys in the preview', () => {
    expect(objectSummary({ api_key: 'sk-live' })).toBe('JSON · 1 key · {api_key: "***"}')
  })
})

describe('searchBlob', () => {
  it('lowercases scalars and JSON-stringifies objects', () => {
    expect(searchBlob('HeLLo')).toBe('hello')
    expect(searchBlob(42)).toBe('42')
    expect(searchBlob(null)).toBe('')
    expect(searchBlob({ A: 1 })).toBe('{"a":1}')
  })
})

describe('summariseDiffValue', () => {
  it('JSON-encodes and truncates long values', () => {
    expect(summariseDiffValue(true)).toBe('true')
    expect(summariseDiffValue('hi')).toBe('"hi"')
    const long = 'x'.repeat(200)
    expect(summariseDiffValue(long).endsWith('…')).toBe(true)
    expect(summariseDiffValue(long).length).toBe(118)
  })
})

// ── objToYaml — config.js:886-912 ───────────────────────────────────────────
describe('objToYaml', () => {
  it('renders scalars', () => {
    expect(objToYaml(true)).toBe('true')
    expect(objToYaml(5)).toBe('5')
    expect(objToYaml(null)).toBe('null')
    expect(objToYaml('plain')).toBe('plain')
  })
  it('quotes strings with structural characters', () => {
    expect(objToYaml('a: b')).toBe('"a: b"')
    expect(objToYaml(' leading')).toBe('" leading"')
  })
  it('renders empty collections inline', () => {
    expect(objToYaml([])).toBe('[]')
    expect(objToYaml({})).toBe('{}')
  })
  it('renders a nested object block (legacy trimStart posture)', () => {
    // config.js:908 uses rendered.trimStart() for non-inline values, which
    // collapses the leading newline of the block onto the key line. This is a
    // 1:1 port of the legacy serialiser's exact (quirky) output.
    expect(objToYaml({ a: 1, b: { c: 2 } })).toBe('\na: 1\nb: c: 2')
  })
  it('renders a list block (legacy trimStart posture)', () => {
    expect(objToYaml({ xs: [1, 2] })).toBe('\nxs: - 1\n  - 2')
  })
})

// ── helpFor — config.js:127-130 ─────────────────────────────────────────────
describe('helpFor', () => {
  it('returns a specific message for a known key', () => {
    expect(helpFor('host')).toMatch(/Network interface/)
  })
  it('falls back to a generic message', () => {
    expect(helpFor('totally.unknown.key')).toBe('No description yet — see the docs.')
  })
})
