import { describe, expect, it } from 'vitest'
import { PANEL_TOOLS, tabMeta, toolAvailable, toolDef } from './registry'

describe('panel tab registry', () => {
  it('lists Files and Browser as tools, each with its gateway methods', () => {
    expect(PANEL_TOOLS.map((tool) => tool.kind)).toEqual(['files', 'browser'])
    expect(toolDef('files').methods).toContain('workspace.files.list')
    expect(toolDef('browser').methods).toContain('browser.status')
  })

  it('offers a tool only when the gateway advertises its methods', () => {
    const files = toolDef('files')
    expect(toolAvailable(files, null)).toBe(true)
    expect(toolAvailable(files, ['workspace.files.list', 'workspace.files.read'])).toBe(true)
    expect(toolAvailable(files, ['workspace.files.list'])).toBe(false)
    expect(toolAvailable(toolDef('browser'), ['health'])).toBe(false)
  })

  it('names every tab kind', () => {
    expect(tabMeta('files').label).toBe('Files')
    expect(tabMeta('tool-output').label).toBe('Tool output')
    expect(tabMeta('thinking').label).toBe('Reasoning')
  })
})
