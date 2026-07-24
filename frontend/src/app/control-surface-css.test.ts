import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const controlCss = readFileSync('src/styles/control-surface.css', 'utf8')
const globalsCss = readFileSync('src/styles/globals.css', 'utf8')
const setupCss = readFileSync('src/views/setup/setup.css', 'utf8')
const configCss = readFileSync('src/views/config/config.css', 'utf8')
const cronCss = readFileSync('src/views/cron/cron.css', 'utf8')

describe('Control surface CSS contract', () => {
  it('keeps the rail logo centered and pins the toggle to the sidebar edge', () => {
    expect(controlCss).toMatch(
      /\.shell\[data-design='unified'\] \.shell-sidebar\[data-collapsed='true'\] \{\s*width: 5rem;/,
    )
    expect(controlCss).toMatch(
      /\.shell\[data-design='unified'\] \.shell-sidebar\[data-collapsed='true'\] \.shell-sidebar__head \{[\s\S]*?justify-content: center;[\s\S]*?background: transparent;/,
    )
    expect(controlCss).toMatch(
      /\.shell\[data-design='unified'\] \.shell-sidebar\[data-collapsed='true'\] \.shell-sidebar__brand \{[\s\S]*?max-width: none;[\s\S]*?gap: 0;[\s\S]*?opacity: 1;/,
    )
    expect(controlCss).toMatch(
      /\.shell\[data-design='unified'\] \.shell-sidebar\[data-collapsed='true'\] \.shell-sidebar__collapse \{[\s\S]*?position: absolute;[\s\S]*?right: 0;[\s\S]*?translate: 50% -50%;/,
    )
    expect(controlCss).toMatch(
      /\.shell\[data-design='unified'\] \.shell-sidebar__head \{[\s\S]*?background: color-mix\(in srgb, var\(--elevated\) 78%, var\(--sidebar\)\);/,
    )
    expect(controlCss).toMatch(
      /\.shell\[data-design='unified'\] \.shell-sidebar\[data-collapsed='true'\] \.shell-sidebar__collapse \{[\s\S]*?border: 0;[\s\S]*?box-shadow: none;/,
    )
    expect(controlCss).toMatch(
      /\.shell-sidebar\[data-collapsed='true'\][\s\S]*?\.shell-sidebar__collapse::after \{[\s\S]*?inset: 0 0 0 50%;[\s\S]*?border: 1px solid var\(--sidebar-border\);[\s\S]*?border-left: 0;/,
    )
    expect(controlCss).toMatch(
      /\.shell\[data-design='unified'\][\s\S]*?\.shell-sidebar\[data-collapsed='true'\][\s\S]*?\.shell-nav-link \{[\s\S]*?justify-content: center;[\s\S]*?padding-inline: 0;/,
    )
  })

  it('shares the modern shell tokens across Chat and Control routes', () => {
    expect(controlCss).toMatch(/:root\[data-theme='dark'\] \.shell\[data-design='unified'\]/)
    expect(controlCss).toMatch(
      /\.shell\[data-design='unified'\] \.shell-sidebar \{[\s\S]*?border-radius: var\(--radius-dialog\);[\s\S]*?box-shadow:/,
    )
    expect(controlCss).not.toMatch(/\.shell-header--unified/)
  })

  it('takes the mobile drawer out of flex flow so route content keeps the viewport width', () => {
    expect(controlCss).toMatch(
      /@media \(max-width: 768px\)[\s\S]*?\.shell\[data-design='unified'\] \.shell-sidebar \{[\s\S]*?position: fixed;/,
    )
  })

  it('keeps the persistent route scroll root immediate across page changes', () => {
    const routeScroller = controlCss.match(/\.shell-main--control \{([\s\S]*?)\n\}/)?.[1] ?? ''
    expect(routeScroller).toContain('scroll-behavior: auto')
    expect(routeScroller).not.toContain('scroll-behavior: smooth')
  })

  it('uses one semantic soft-radius scale across controls, surfaces, and dialogs', () => {
    expect(globalsCss).toMatch(/--radius-compact: 6px;/)
    expect(globalsCss).toMatch(/--radius-control: 8px;/)
    expect(globalsCss).toMatch(/--radius: 10px;/)
    expect(globalsCss).toMatch(/--radius-surface: 14px;/)
    expect(globalsCss).toMatch(/--radius-dialog: 18px;/)
    expect(globalsCss).toMatch(/--radius-pill: 999px;/)

    expect(controlCss).toMatch(
      /\.control-surface \.panel,[\s\S]*?border-radius: var\(--radius-surface\);/,
    )
    expect(controlCss).toMatch(
      /:is\(\.ag-modal, \.sess-modal, \.sk-modal, \.cron-modal, \.cron-panel\) \{[\s\S]*?border-radius: var\(--radius-dialog\);/,
    )
  })

  it('retires square legacy Setup and Cron controls while preserving structural seams', () => {
    expect(setupCss).not.toMatch(/border-radius:\s*(?:0|[1-3]px)/)
    expect(cronCss).not.toMatch(/border-radius:\s*(?:0|[1-3]px)/)
    expect(controlCss.match(/border-radius:\s*0;/g)).toHaveLength(1)
    expect(controlCss).toMatch(
      /:is\([\s\S]*?\.ov-stats,[\s\S]*?\.ap-stats[\s\S]*?\) \{[\s\S]*?border: 0;[\s\S]*?border-radius: 0;[\s\S]*?background: transparent;/,
    )
  })

  it('mirrors Control tokens into every portalled dialog family including Cron and MCP', () => {
    expect(controlCss).toMatch(
      /:root\[data-theme='dark'\][\s\S]*?:is\([\s\S]*?\.ag-modal__overlay,[\s\S]*?\.cron-modal__overlay,[\s\S]*?\.mcp-modal__overlay[\s\S]*?\)/,
    )
    expect(controlCss).toMatch(
      /:root\[data-theme='light'\][\s\S]*?:is\([\s\S]*?\.ag-modal__overlay,[\s\S]*?\.cron-modal__overlay,[\s\S]*?\.mcp-modal__overlay[\s\S]*?\)/,
    )
  })

  it('lets the session workflow dialog use the wider control-page measure', () => {
    expect(controlCss).toMatch(/\.sess-modal,\s*\.sk-modal \{\s*max-width: 46rem;/)
    expect(controlCss).toMatch(/\.sess-modal\.sess-confirm \{\s*max-width: 32rem;/)
  })

  it('renders an unframed ASCII signal field behind Control headers with safe motion fallbacks', () => {
    expect(controlCss).toMatch(
      /\.control-surface > \.ascii-field \{[\s\S]*?height: 10rem;[\s\S]*?border: 0;[\s\S]*?box-shadow: none;/,
    )
    expect(controlCss).toMatch(
      /@keyframes control-signal-sweep \{[\s\S]*?transform: translate3d\([\s\S]*?opacity:/,
    )
    expect(controlCss).toMatch(
      /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?\.control-surface > \.ascii-field::after \{[\s\S]*?animation: none;/,
    )
  })

  it('keeps data surfaces separated from their page header without doubling Setup or Cron gaps', () => {
    expect(controlCss).toMatch(
      /\.control-surface\s+:is\([\s\S]*?\.ov-stage__header,[\s\S]*?\.cfg-stage__header[\s\S]*?\) \{\s*margin-bottom: 1\.5rem;/,
    )
    expect(controlCss).not.toMatch(
      /:is\([\s\S]*?\.setup-stage__header,[\s\S]*?\.cron-stage__header[\s\S]*?\) \{\s*margin-bottom: 1\.5rem;/,
    )
  })

  it('places Settings on the shared Control hero and action rhythm', () => {
    expect(controlCss).toMatch(
      /:is\([\s\S]*?\.settings-stage__header[\s\S]*?\) \{[\s\S]*?min-height: 10rem;[\s\S]*?padding: 1\.5rem 1\.75rem;/,
    )
    expect(controlCss).toMatch(
      /:is\([\s\S]*?\.settings-stage__title-block[\s\S]*?\) \{[\s\S]*?min-width: 0;/,
    )
    expect(controlCss).toMatch(
      /:is\([\s\S]*?\.settings-stage__actions[\s\S]*?\) \{[\s\S]*?justify-content: flex-end;/,
    )
  })

  it('lets embedded Settings reset legacy Control chrome with route-level specificity', () => {
    expect(setupCss).toMatch(
      /\.control-surface \.setup-stage--embedded \.setup-stepper \{[\s\S]*?flex-direction: column;[\s\S]*?border: 0;[\s\S]*?background: transparent;[\s\S]*?box-shadow: none;/,
    )
    expect(setupCss).toMatch(
      /\.control-surface \.setup-stage--embedded \.setup-panel \{[\s\S]*?border: 0;[\s\S]*?background: transparent;[\s\S]*?box-shadow: none;/,
    )
    expect(configCss).toMatch(
      /\.control-surface \.cfg-stage--embedded \.cfg-stage__header--embedded \{[\s\S]*?min-height: 0;[\s\S]*?margin-bottom: 0\.75rem;/,
    )
    expect(configCss).toMatch(
      /\.control-surface \.cfg-stage--embedded \.cfg-group \{[\s\S]*?grid-template-columns: minmax\(0, 1fr\);[\s\S]*?border: 0;[\s\S]*?background: transparent;[\s\S]*?box-shadow: none;/,
    )
    expect(configCss).toMatch(
      /\.control-surface \.cfg-stage--embedded \.cfg-group__head \{[\s\S]*?border-right: 0;[\s\S]*?border-bottom: 1px solid var\(--hairline\);/,
    )
    expect(configCss).toMatch(
      /\.control-surface \.cfg-stage \.cfg-yaml__area \{[\s\S]*?min-height: 60vh;/,
    )
    expect(configCss).toMatch(
      /@media \(max-width: 840px\) \{[\s\S]*?\.control-surface \.cfg-stage--embedded \.cfg-group__fields \{[\s\S]*?grid-template-columns: 1fr;/,
    )
    expect(controlCss).toMatch(
      /\.control-surface \.setup-router-toolbar input\[type='number'\] \{[\s\S]*?box-sizing: border-box;[\s\S]*?padding: 0\.58rem 2\.65rem 0\.58rem 0\.75rem;/,
    )
  })

  it('shows keyboard focus on the custom Advanced Config switch track', () => {
    expect(configCss).toMatch(
      /\.cfg-switch input:focus-visible \+ \.cfg-switch__track \{[\s\S]*?box-shadow: 0 0 0 3px/,
    )
  })
})
