import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/views/mcp/mcp.css', 'utf8')

describe('MCP visual contract', () => {
  it('uses the shared soft radius system without a decorative side rail', () => {
    expect(css).toContain('border-radius: var(--radius-surface)')
    expect(css).toContain('border-radius: var(--radius-control)')
    expect(css).not.toMatch(/\.mcp-partner::before[^}]*border-left/s)
  })

  it('keeps server inventory as separated rows instead of duplicate cards', () => {
    expect(css).toMatch(/\.mcp-server-list\s*{[^}]*border-top:/s)
    expect(css).toMatch(/\.mcp-server-row\s*{[^}]*border-bottom:/s)
    expect(css).not.toMatch(/\.mcp-server-row\s*{[^}]*border:\s*1px/s)
  })

  it('has explicit mobile and reduced-motion fallbacks', () => {
    expect(css).toMatch(
      /@media \(max-width: 1100px\)[\s\S]*?\.control-surface \.mcp-stage__header\s*{[^}]*grid-template-columns:\s*1fr/s,
    )
    expect(css).toContain('@media (max-width: 760px)')
    expect(css).toContain('@media (prefers-reduced-motion: reduce)')
    expect(css).toContain(".mcp-servers__header [data-slot='button']")
    expect(css).toContain(".mcp-server-row__actions [data-slot='button']:not([data-size^='icon'])")
    expect(css).not.toMatch(/\.mcp-(?:servers__header|server-row__actions) \.btn/)
  })

  it('keeps the timeout suffix clear of native number steppers', () => {
    expect(css).toMatch(
      /\.mcp-timeout-input input\[type='number'\]\s*{[^}]*appearance:\s*textfield/s,
    )
    expect(css).toContain("input[type='number']::-webkit-inner-spin-button")
  })
})
