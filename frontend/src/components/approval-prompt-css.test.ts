import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const promptCss = readFileSync('src/components/ApprovalPrompt.css', 'utf8')
const globalsCss = readFileSync('src/styles/globals.css', 'utf8')

describe('blocking approval layer CSS contract', () => {
  it('uses the global critical layer above every route-owned dialog', () => {
    expect(globalsCss).toMatch(/--z-critical-approval: 2000;/)
    expect(promptCss).toMatch(
      /\.approval-backdrop \{[\s\S]*?z-index: var\(--z-critical-approval\);/,
    )
    expect(promptCss).not.toMatch(/\.approval-backdrop \{[\s\S]*?z-index: (?:50|60|80|120);/)
  })
})
