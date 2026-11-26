import type { Item, Turn } from '../model/types'

// Paths a transcript mentions, and how one becomes a file in the Files tab.

export { PANEL_TOGGLE_KEY } from '@/web/features/palette/bindings'

export const RESOLVE_METHOD = 'workspace.files.resolve'

type ToolItem = Extract<Item, { kind: 'toolCall' }>

/** File tools whose `path` argument names one workspace file or folder. */
const PATH_TOOLS: ReadonlySet<string> = new Set([
  'read_file',
  'write_file',
  'edit_file',
  'read_spreadsheet',
  'list_dir',
  'publish_artifact',
])
const PATCH_FILE = /^\*\*\* (?:Add|Update) File: (.+)$/gm
const MAX_PATHS = 5

/** Tools that change workspace files: the Files tab refreshes when one finishes. */
const WRITE_TOOLS: ReadonlySet<string> = new Set([
  'write_file',
  'edit_file',
  'apply_patch',
  'exec_command',
  'execute_code',
  'publish_artifact',
])

/** How many file-changing tool calls in `turns` have finished. */
export function writtenFileCount(turns: readonly Turn[]): number {
  let count = 0
  for (const turn of turns) {
    for (const item of turn.items) {
      if (item.kind === 'toolCall' && item.state !== 'running' && WRITE_TOOLS.has(item.name)) {
        count += 1
      }
    }
  }
  return count
}

/** The browser tool's name: its rows offer the Browser tab. */
export function isBrowserTool(name: string): boolean {
  return name === 'browser'
}

/** Every file a tool call wrote or read, as the model typed it (at most five). */
export function toolFilePaths(item: ToolItem): string[] {
  const args = (item.args && typeof item.args === 'object' ? item.args : {}) as Record<
    string,
    unknown
  >
  const found: string[] = []
  if (PATH_TOOLS.has(item.name) && typeof args.path === 'string' && args.path.trim()) {
    found.push(args.path.trim())
  }
  if (item.name === 'apply_patch' && typeof args.patch === 'string') {
    for (const match of args.patch.matchAll(PATCH_FILE)) {
      const path = match[1]!.trim()
      if (path && !found.includes(path)) found.push(path)
      if (found.length >= MAX_PATHS) break
    }
  }
  return found.slice(0, MAX_PATHS)
}

/**
 * A workspace-relative path the client can vouch for without asking the
 * gateway: relative, no `..`, no backslashes. Anything else needs the
 * gateway's `workspace.files.resolve`, which knows the root.
 */
export function plainRelativePath(raw: string): string | null {
  const text = raw.trim()
  if (!text || text.startsWith('/') || text.startsWith('~') || /^[A-Za-z]:/.test(text)) return null
  if (text.includes('\\')) return null
  const parts = text.split('/').filter((part) => part !== '' && part !== '.')
  if (parts.some((part) => part === '..')) return null
  return parts.join('/')
}

export interface ResolvedPath {
  path: string | null
  reason?: string
}

interface ResolveRpc {
  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>
}

/**
 * Map a transcript path to the Files tab's form. A gateway that has
 * `workspace.files.resolve` answers (it can place absolute paths); one that
 * does not still opens plain relative paths.
 */
export async function resolveWorkspacePath(
  rpc: ResolveRpc,
  methods: readonly string[] | null,
  raw: string,
  agentId: string,
): Promise<ResolvedPath> {
  const local = plainRelativePath(raw)
  if (methods && !methods.includes(RESOLVE_METHOD)) {
    return local !== null ? { path: local } : { path: null, reason: 'outside' }
  }
  try {
    const body = await rpc.call<{ path?: unknown; reason?: unknown }>(RESOLVE_METHOD, {
      path: raw,
      agentId,
    })
    if (typeof body?.path === 'string') return { path: body.path }
    return { path: null, reason: typeof body?.reason === 'string' ? body.reason : 'outside' }
  } catch {
    return local !== null ? { path: local } : { path: null, reason: 'outside' }
  }
}
