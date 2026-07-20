import { describe, expect, it } from 'vitest'
import {
  agentDisplay,
  agentStats,
  agentToForm,
  buildCreatePayload,
  buildUpdatePayload,
  isBuiltinAgent,
  parseToolsInput,
  validateAgentId,
  validateCreate,
  type RawAgent,
} from './logic'

describe('isBuiltinAgent', () => {
  it('is true when type is builtin', () => {
    expect(isBuiltinAgent({ id: 'main', type: 'builtin' })).toBe(true)
  })
  it('is true when isBuiltin flag is set', () => {
    expect(isBuiltinAgent({ id: 'main', isBuiltin: true })).toBe(true)
  })
  it('is false for a plain custom agent', () => {
    expect(isBuiltinAgent({ id: 'x', type: 'custom' })).toBe(false)
    expect(isBuiltinAgent({ id: 'x' })).toBe(false)
  })
})

describe('agentStats', () => {
  // agents.js:93-118 — total, built-in/custom split, distinct models, tools sum.
  it('counts totals, builtin/custom split, distinct models and tool sum', () => {
    const agents: RawAgent[] = [
      { id: 'main', type: 'builtin', model: 'gpt', tools: ['a', 'b'] },
      { id: 'helper', isBuiltin: true, model: 'gpt', tools: [] },
      { id: 'custom1', type: 'custom', model: 'claude', tools: ['a'] },
      { id: 'custom2', model: '', tools: ['x', 'y', 'z'] },
    ]
    const s = agentStats(agents)
    expect(s.total).toBe(4)
    expect(s.builtins).toBe(2)
    expect(s.customs).toBe(2)
    // distinct non-empty models: gpt, claude → 2
    expect(s.models).toBe(2)
    // 2 + 0 + 1 + 3 = 6
    expect(s.tools).toBe(6)
  })
  it('handles an empty list', () => {
    const s = agentStats([])
    expect(s).toEqual({ total: 0, builtins: 0, customs: 0, models: 0, tools: 0 })
  })
  it('ignores non-array tools', () => {
    const s = agentStats([{ id: 'a', tools: 'nope' as unknown as string[] }])
    expect(s.tools).toBe(0)
  })
})

describe('agentDisplay', () => {
  // agents.js:141-179 — per-card derivation.
  it('resolves id/name/type/tone and tool chips (first 8 + overflow)', () => {
    const tools = Array.from({ length: 10 }, (_, i) => `t${i}`)
    const d = agentDisplay({
      id: 'data',
      name: 'Data',
      description: 'x',
      model: 'm',
      tools,
      skills: ['s1'],
    })
    expect(d.id).toBe('data')
    expect(d.name).toBe('Data')
    expect(d.type).toBe('custom')
    expect(d.isBuiltin).toBe(false)
    expect(d.tone).toBe('info')
    expect(d.model).toBe('m')
    expect(d.toolCount).toBe(10)
    expect(d.skillCount).toBe(1)
    expect(d.toolChips).toHaveLength(8)
    expect(d.overflow).toBe(2)
  })
  it('falls back name→id and id→name; builtin gets ok tone', () => {
    const d = agentDisplay({ id: 'main', type: 'builtin' })
    expect(d.name).toBe('main')
    expect(d.type).toBe('builtin')
    expect(d.isBuiltin).toBe(true)
    expect(d.tone).toBe('ok')
    expect(d.overflow).toBe(0)
    expect(d.toolChips).toEqual([])
  })
  it('derives type from isBuiltin when type is absent', () => {
    expect(agentDisplay({ id: 'x', isBuiltin: true }).type).toBe('builtin')
    expect(agentDisplay({ id: 'x' }).type).toBe('custom')
  })
  it('uses em dash when id and name are both missing', () => {
    const d = agentDisplay({})
    expect(d.id).toBe('—')
    expect(d.name).toBe('—')
  })
})

describe('agentToForm', () => {
  // agents.js:260-270 — seed the edit form from an agent.
  it('maps agent fields to a form snapshot with defaults', () => {
    const f = agentToForm({
      id: 'a',
      name: 'A',
      description: 'd',
      tools: ['t1', 't2'],
      workspace: '/ws',
      agent_dir: '/dir',
      enabled: false,
    })
    expect(f).toEqual({
      id: 'a',
      name: 'A',
      description: 'd',
      tools: ['t1', 't2'],
      workspace: '/ws',
      agentDir: '/dir',
      enabled: false,
    })
  })
  it('defaults enabled to true when not explicitly false', () => {
    expect(agentToForm({ id: 'a' }).enabled).toBe(true)
    expect(agentToForm({ id: 'a', enabled: false }).enabled).toBe(false)
  })
  it('prefers agentDir camelCase over agent_dir and copies tools array', () => {
    const tools = ['x']
    const f = agentToForm({ id: 'a', agentDir: '/camel', agent_dir: '/snake', tools })
    expect(f.agentDir).toBe('/camel')
    // must be a copy, not the same reference
    expect(f.tools).toEqual(['x'])
    expect(f.tools).not.toBe(tools)
  })
})

describe('parseToolsInput', () => {
  // agents.js:376 — split comma list, trim, drop blanks.
  it('splits, trims and drops blanks', () => {
    expect(parseToolsInput(' a , b ,, c ')).toEqual(['a', 'b', 'c'])
  })
  it('returns [] for blank input', () => {
    expect(parseToolsInput('')).toEqual([])
    expect(parseToolsInput('   ')).toEqual([])
  })
})

describe('validateAgentId', () => {
  // agents.js:226,228 — id is required (trimmed non-empty) for create.
  it('rejects empty / whitespace-only ids', () => {
    expect(validateAgentId('')).toBe('Agent ID is required.')
    expect(validateAgentId('   ')).toBe('Agent ID is required.')
  })
  it('accepts a non-empty id', () => {
    expect(validateAgentId('data-analyst')).toBeNull()
    expect(validateAgentId('  x  ')).toBeNull()
  })
})

describe('validateCreate', () => {
  it('returns the id error when id is blank', () => {
    expect(validateCreate({ id: '  ', name: 'X' })).toEqual({ id: 'Agent ID is required.' })
  })
  it('returns no errors for a valid create', () => {
    expect(validateCreate({ id: 'x', name: '' })).toEqual({})
  })
})

describe('buildCreatePayload', () => {
  // agents.js:226-230 — {id} always, name only when provided (trimmed).
  it('includes trimmed id and omits blank name', () => {
    expect(buildCreatePayload({ id: '  data ', name: '' })).toEqual({ id: 'data' })
    expect(buildCreatePayload({ id: 'data', name: '   ' })).toEqual({ id: 'data' })
  })
  it('includes a trimmed name when provided', () => {
    expect(buildCreatePayload({ id: 'data', name: '  Data Bot ' })).toEqual({
      id: 'data',
      name: 'Data Bot',
    })
  })
})

describe('buildUpdatePayload', () => {
  // agents.js:467-476 — diff initial vs current; {id} plus only changed keys.
  const base = agentToForm({
    id: 'a',
    name: 'A',
    description: 'd',
    tools: ['t1'],
    workspace: '/ws',
    agent_dir: '/dir',
    enabled: true,
  })

  it('returns only {id} when nothing changed', () => {
    expect(buildUpdatePayload(base, { ...base })).toEqual({ id: 'a' })
  })
  it('includes only the changed scalar fields', () => {
    const next = { ...base, name: 'A2', enabled: false }
    expect(buildUpdatePayload(base, next)).toEqual({ id: 'a', name: 'A2', enabled: false })
  })
  it('includes tools only when the array differs', () => {
    const same = { ...base, tools: ['t1'] }
    expect(buildUpdatePayload(base, same)).toEqual({ id: 'a' })
    const changed = { ...base, tools: ['t1', 't2'] }
    expect(buildUpdatePayload(base, changed)).toEqual({ id: 'a', tools: ['t1', 't2'] })
  })
  it('carries description, workspace and agentDir edits', () => {
    const next = { ...base, description: 'd2', workspace: '/ws2', agentDir: '/dir2' }
    expect(buildUpdatePayload(base, next)).toEqual({
      id: 'a',
      description: 'd2',
      workspace: '/ws2',
      agentDir: '/dir2',
    })
  })
})
