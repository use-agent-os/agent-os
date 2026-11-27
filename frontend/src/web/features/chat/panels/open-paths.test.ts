import { describe, expect, it, vi } from 'vitest'
import type { Item, Turn } from '../model/types'
import {
  plainRelativePath,
  resolveWorkspacePath,
  toolFilePaths,
  writtenFileCount,
} from './open-paths'

type ToolItem = Extract<Item, { kind: 'toolCall' }>

function tool(name: string, args: unknown, state: ToolItem['state'] = 'done'): ToolItem {
  return {
    id: name,
    kind: 'toolCall',
    toolUseId: name,
    name,
    args,
    isError: false,
    truncated: false,
    state,
  } as ToolItem
}

describe('paths in tool calls', () => {
  it('reads the path of a file tool, and every file a patch adds or updates', () => {
    expect(toolFilePaths(tool('read_file', { path: ' docs/a.md ' }))).toEqual(['docs/a.md'])
    expect(toolFilePaths(tool('exec_command', { path: 'x' }))).toEqual([])
    const patch = [
      '*** Begin Patch',
      '*** Update File: src/a.py',
      '*** Add File: docs/new.md',
      '*** Delete File: old.txt',
      '*** End Patch',
    ].join('\n')
    expect(toolFilePaths(tool('apply_patch', { patch }))).toEqual(['src/a.py', 'docs/new.md'])
  })

  it('vouches only for plain relative paths', () => {
    expect(plainRelativePath('./docs//a.md')).toBe('docs/a.md')
    expect(plainRelativePath('/etc/passwd')).toBeNull()
    expect(plainRelativePath('~/x')).toBeNull()
    expect(plainRelativePath('C:/x')).toBeNull()
    expect(plainRelativePath('../x')).toBeNull()
    expect(plainRelativePath('a\\b')).toBeNull()
  })

  it('asks the gateway when it can, and falls back to plain paths when it cannot', async () => {
    const call = vi.fn().mockResolvedValue({ path: 'src/a.py' })
    expect(await resolveWorkspacePath({ call }, null, '/ws/src/a.py', 'main')).toEqual({
      path: 'src/a.py',
    })
    expect(call).toHaveBeenCalledWith('workspace.files.resolve', {
      path: '/ws/src/a.py',
      agentId: 'main',
    })
    const outside = vi.fn().mockResolvedValue({ path: null, reason: 'outside' })
    expect(await resolveWorkspacePath({ call: outside }, null, '/etc/x', 'main')).toEqual({
      path: null,
      reason: 'outside',
    })
    const old = vi.fn()
    expect(await resolveWorkspacePath({ call: old }, ['health'], 'docs/a.md', 'main')).toEqual({
      path: 'docs/a.md',
    })
    expect(old).not.toHaveBeenCalled()
    const failing = vi.fn().mockRejectedValue(new Error('METHOD_NOT_FOUND'))
    expect(await resolveWorkspacePath({ call: failing }, null, '/abs', 'main')).toEqual({
      path: null,
      reason: 'outside',
    })
  })

  it('counts finished file-changing calls', () => {
    const turns = [
      {
        items: [
          tool('write_file', {}),
          tool('edit_file', {}, 'running'),
          tool('read_file', {}),
          tool('exec_command', {}, 'failed'),
        ],
      },
    ] as unknown as Turn[]
    expect(writtenFileCount(turns)).toBe(2)
  })
})
