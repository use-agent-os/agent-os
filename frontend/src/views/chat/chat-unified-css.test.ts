import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/views/chat/chat-unified.css', 'utf8')
const legacyCss = readFileSync('src/views/chat/chat.css', 'utf8')

describe('unified Chat CSS contract', () => {
  it('uses the product type system while preserving mono machine data', () => {
    expect(css).toMatch(/\.chat-surface \.chat-stage \{[\s\S]*?font-family: var\(--font-sans\);/)
    expect(css).toMatch(/\.chat-surface \.msg-meta \{[\s\S]*?font-family: var\(--font-mono\);/)
    expect(css).toMatch(/\.chat-surface \.chat-tools-summary \{/)
  })

  it('replaces the perpetual neon composer animation with a stable focus surface', () => {
    expect(css).toMatch(/\.chat-surface \.chat-composer \{[\s\S]*?animation: none;/)
    expect(css).toMatch(
      /\.chat-surface \.chat-composer::before,[\s\S]*?\.chat-surface \.chat-composer::after \{[\s\S]*?content: none;/,
    )
    expect(css).toMatch(/\.chat-surface \.chat-composer:focus-within \{/)
  })

  it('keeps the large transcript stationary and coordinates only lightweight entry surfaces', () => {
    expect(css).toMatch(
      /\.chat-surface \.chat-thread \{[\s\S]*?overflow-anchor: none;[\s\S]*?scrollbar-gutter: stable;/,
    )
    expect(css).toMatch(
      /\.chat-view-enter \.chat-composer-shell \{[\s\S]*?animation: chat-composer-enter/,
    )
    expect(css).toMatch(
      /\.shell\[data-surface='chat'\] \.shell-chat-header \{[\s\S]*?animation: chat-header-enter/,
    )
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?\.chat-view-enter \.chat-composer-shell,[\s\S]*?animation: none !important;/,
    )
  })

  it('keeps history out of the paint tree until positioned and reserves image geometry', () => {
    expect(css).toMatch(
      /\.chat-surface \.chat-thread\[data-history-ready='false'\] \{[\s\S]*?visibility: hidden;/,
    )
    expect(css).toMatch(
      /\.chat-surface \.chat-thread\[data-history-ready='true'\] \+ \.chat-history-loading \{[\s\S]*?display: none;/,
    )
    expect(css).toMatch(
      /\.chat-surface \.msg-artifact-preview,[\s\S]*?aspect-ratio: 16 \/ 10;[\s\S]*?object-fit: contain;/,
    )
  })

  it('reserves portalled header geometry before reactive controls mount', () => {
    expect(css).toMatch(
      /\.shell-chat-header__context \{[\s\S]*?min-height: 2\.5rem;[\s\S]*?overflow: visible;/,
    )
    expect(css).toMatch(
      /\.shell-chat-header__primary-action \{[\s\S]*?min-width: 6\.75rem;[\s\S]*?min-height: 2\.5rem;/,
    )
    expect(css).toMatch(
      /@media \(max-width: 768px\)[\s\S]*?\.shell-chat-header \{[\s\S]*?min-height: 6\.25rem;/,
    )
  })

  it('keeps lime as a signal instead of filling the user message bubble', () => {
    const userBubble = css.match(/\.chat-surface \.msg\.user \{([\s\S]*?)\n\}/)?.[1] ?? ''
    expect(userBubble).toContain('background: var(--elevated)')
    expect(userBubble).not.toContain('background: var(--primary)')
  })

  it('keeps the Chat toolbar compact while widening popovers and mobile targets', () => {
    expect(css).toMatch(/\.chat-session-popover \{[\s\S]*?width: min\(30rem,/)
    expect(css).toMatch(
      /\.shell-chat-header \{[\s\S]*?width: min\(62rem, calc\(100% - 1rem\)\);[\s\S]*?min-height: 3\.25rem;[\s\S]*?grid-template-columns: auto minmax\(18rem, 1fr\) auto;/,
    )
    expect(css).toMatch(/\.chat-toolbar-popover \{[\s\S]*?width: min\(32rem,/)
    expect(css).toMatch(/@media \(max-width: 768px\)[\s\S]*?min-height: 2\.75rem;/)
    expect(css).toMatch(
      /@media \(max-width: 768px\)[\s\S]*?\.chat-session-popover \{[\s\S]*?position: absolute;[\s\S]*?top: calc\(100% \+ 0\.625rem\);/,
    )
    expect(css).toMatch(
      /@media \(max-width: 560px\)[\s\S]*?\.chat-composer__input,[\s\S]*?font-size: 1rem;/,
    )
  })

  it('keeps the floating Chat header above the transcript stacking context', () => {
    expect(css).toMatch(
      /\.shell\[data-surface='chat'\] \.shell-chat-header \{[\s\S]*?position: relative;[\s\S]*?z-index: 30;[\s\S]*?border-radius: var\(--radius-surface\);/,
    )
    expect(css).toMatch(/\.shell-chat-header__context \{[\s\S]*?overflow: visible;/)
  })

  it('positions the actions menu from a trigger-sized anchor instead of the full header row', () => {
    expect(css).toMatch(/\.chat-session-actions \{[\s\S]*?position: relative;[\s\S]*?flex: none;/)
    expect(css).toMatch(
      /\.chat-session-actions-menu \{[\s\S]*?top: calc\(100% \+ 0\.625rem\);[\s\S]*?right: 0;/,
    )
  })

  it('keeps keyboard focus visible inside header menus and session results', () => {
    expect(css).toMatch(
      /\.chat-session-actions-menu__item:focus-visible \{[\s\S]*?outline: 2px solid var\(--ring\);/,
    )
    expect(css).toMatch(
      /\.chat-session-popover-item:focus-visible \{[\s\S]*?outline: 2px solid var\(--ring\);/,
    )
  })

  it('keeps run status textual instead of reducing it to a color-only dot', () => {
    expect(css).toMatch(
      /@media \(max-width: 560px\)[\s\S]*?\.chat-session-run-status__compact \{[\s\S]*?display: inline;/,
    )
    expect(css).not.toMatch(/\.chat-session-run-status \{[\s\S]{0,300}?font-size: 0;/)
  })

  it('mirrors the unified palette into portalled Chat dialogs', () => {
    expect(css).toMatch(
      /:root\[data-theme='dark'\] :is\(\.chat-modal-overlay, \.chat-output-modal-overlay\)/,
    )
    expect(css).toMatch(
      /:root\[data-theme='light'\] :is\(\.chat-modal-overlay, \.chat-output-modal-overlay\)/,
    )
  })

  it('uses semantic radii for Chat controls, surfaces, and dialogs', () => {
    expect(css).not.toMatch(/border-radius:\s*\d+px/)
    expect(css).toMatch(
      /\.chat-surface \.chat-tools-collapse \{[\s\S]*?border-radius: var\(--radius-control\);/,
    )
    expect(css).toMatch(
      /\.chat-surface \.chat-slash-item \{[\s\S]*?border-radius: var\(--radius-control\);/,
    )
    expect(css).toMatch(
      /\.chat-session-popover-item \{[\s\S]*?border-radius: var\(--radius-control\);/,
    )
    expect(css).toMatch(/\.chat-modal \{[\s\S]*?border-radius: var\(--radius-dialog\);/)
  })

  it('keeps only the clipped popover-search seam square', () => {
    expect(css.match(/border-radius:\s*0;/g)).toHaveLength(1)
    expect(css).toMatch(
      /\.chat-session-popover-search \{[\s\S]*?border-radius: 0;[\s\S]*?background: var\(--surface\);/,
    )
    expect(legacyCss).toMatch(
      /\.chat-tools-collapse \{[\s\S]*?border-radius: var\(--radius-control\);/,
    )
    expect(legacyCss).toMatch(
      /\.chat-attachments__rejection \{[\s\S]*?border-radius: var\(--radius-control\);/,
    )
  })
})
