// Pure agents-view helpers ported 1:1 from the legacy view
// (src/agentos/gateway/static/js/views/agents.js). Each function below carries
// the legacy line range it mirrors so the parity matrix stays auditable. RPC
// calls, mutations, dialogs and rendering live in AgentsPage.tsx; this module
// owns the pure derivations (stats, card display, form seed/diff, validation).

/** A raw agent row from agents.list (all fields optional). */
export interface RawAgent {
  id?: string
  name?: string
  type?: string
  isBuiltin?: boolean
  description?: string
  model?: string
  tools?: string[]
  skills?: string[]
  workspace?: string
  agent_dir?: string
  agentDir?: string
  enabled?: boolean
  system_prompt?: string
  systemPrompt?: string
  [key: string]: unknown
}

/** Tone token for a card gutter/chip (status color ONLY via --tone). */
export type AgentTone = 'ok' | 'info'

/** agents.js:97,144-146 — an agent is built-in via `type==='builtin'` or the flag. */
export function isBuiltinAgent(agent: RawAgent): boolean {
  return agent.type === 'builtin' || agent.isBuiltin === true
}

export interface AgentStats {
  total: number
  builtins: number
  customs: number
  models: number
  tools: number
}

/** agents.js:93-118 — stat-row numbers: totals, builtin/custom split, distinct
 * models and total tools wired across all agents. */
export function agentStats(agents: RawAgent[]): AgentStats {
  const total = agents.length
  const builtins = agents.filter((a) => isBuiltinAgent(a)).length
  const customs = total - builtins
  const tools = agents.reduce((acc, a) => acc + (Array.isArray(a.tools) ? a.tools.length : 0), 0)
  const models = new Set<string>()
  agents.forEach((a) => {
    if (a.model) models.add(a.model)
  })
  return { total, builtins, customs, models: models.size, tools }
}

export interface AgentDisplay {
  id: string
  name: string
  type: string
  isBuiltin: boolean
  tone: AgentTone
  description: string
  model: string
  toolCount: number
  skillCount: number
  toolChips: string[]
  overflow: number
}

const TOOL_CHIP_LIMIT = 8

/** agents.js:141-179 — resolve one card's display fields: id/name/type, the
 * ok/info tone (builtin→ok, custom→info; status color ONLY via --tone),
 * counts, and the first-8 tool chips with an overflow remainder. */
export function agentDisplay(agent: RawAgent): AgentDisplay {
  const id = agent.id || agent.name || '—'
  const name = agent.name || agent.id || '—'
  const isBuiltin = isBuiltinAgent(agent)
  const type = agent.type || (isBuiltin ? 'builtin' : 'custom')
  const tone: AgentTone = isBuiltin ? 'ok' : 'info'
  const tools = Array.isArray(agent.tools) ? agent.tools : []
  const skills = Array.isArray(agent.skills) ? agent.skills : []
  return {
    id,
    name,
    type,
    isBuiltin,
    tone,
    description: agent.description || '',
    model: agent.model || '',
    toolCount: tools.length,
    skillCount: skills.length,
    toolChips: tools.slice(0, TOOL_CHIP_LIMIT),
    overflow: Math.max(0, tools.length - TOOL_CHIP_LIMIT),
  }
}

/** The editable shape of an agent in the create/edit dialog. */
export interface AgentForm {
  id: string
  name: string
  description: string
  tools: string[]
  workspace: string
  agentDir: string
  enabled: boolean
}

/** agents.js:260-270 — seed the edit form from an agent (enabled defaults true). */
export function agentToForm(agent: RawAgent): AgentForm {
  return {
    id: agent.id || '',
    name: agent.name || '',
    description: agent.description || '',
    tools: Array.isArray(agent.tools) ? agent.tools.slice() : [],
    workspace: agent.workspace || '',
    agentDir: agent.agentDir || agent.agent_dir || '',
    enabled: agent.enabled !== false,
  }
}

/** agents.js:376 — parse the comma-separated tools input into a clean list. */
export function parseToolsInput(value: string): string[] {
  return String(value || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

/** The create-dialog fields. */
export interface CreateInput {
  id: string
  name: string
}

/** agents.js:226,228 — the agent ID is required (trimmed, non-empty). */
export function validateAgentId(id: string): string | null {
  return id.trim() ? null : 'Agent ID is required.'
}

/** Field-keyed validation errors for the create dialog (empty = valid). */
export type CreateErrors = Partial<Record<'id', string>>

/** Validate the create dialog; blocks submit when any error is present. */
export function validateCreate(input: CreateInput): CreateErrors {
  const errors: CreateErrors = {}
  const idError = validateAgentId(input.id)
  if (idError) errors.id = idError
  return errors
}

/** agents.js:226-230 — the agents.create payload: {id} always, name only when
 * provided (both trimmed). */
export function buildCreatePayload(input: CreateInput): { id: string; name?: string } {
  const id = input.id.trim()
  const name = input.name.trim()
  const payload: { id: string; name?: string } = { id }
  if (name) payload.name = name
  return payload
}

/** agents.js:467-476 — diff the seed vs the edited form: {id} plus only the
 * changed keys. Tools compared structurally. */
export function buildUpdatePayload(
  initial: AgentForm,
  current: AgentForm,
): Record<string, unknown> {
  const payload: Record<string, unknown> = { id: current.id }
  const scalarKeys: Array<keyof AgentForm> = [
    'name',
    'description',
    'workspace',
    'agentDir',
    'enabled',
  ]
  for (const k of scalarKeys) {
    if (initial[k] !== current[k]) payload[k] = current[k]
  }
  if (JSON.stringify(initial.tools || []) !== JSON.stringify(current.tools || [])) {
    payload.tools = current.tools
  }
  return payload
}
