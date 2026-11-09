import '@/test/web'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'
import { render as legacyRender } from '@/views/chat/markdown'
import { Markdown, renderMarkdown } from './markdown'

function dom(html: string): HTMLElement {
  const div = document.createElement('div')
  div.innerHTML = html
  return div
}

const HOSTILE = [
  '<img src=x onerror=alert(1)>',
  '[link](javascript:alert(1))',
  '<script>alert(1)</script>',
  '<iframe src="https://evil.example"></iframe>',
  '<a href="javascript:alert(1)">x</a>',
  '<svg><script>alert(1)</script></svg>',
  '<div onclick="alert(1)">click</div>',
  '<a href="https://ok.example" target="_top">top</a>',
  '<style>body{display:none}</style>',
  '<form action="https://evil.example"><input name=x></form>',
  '[x](vbscript:msgbox(1))',
  '<img src="data:text/html;base64,PHNjcmlwdD4=">',
  '<details open ontoggle=alert(1)>x</details>',
  '<a href="java\tscript:alert(1)">tab</a>',
  '<math><mi xlink:href="javascript:alert(1)">x</mi></math>',
  '<object data="x.swf"></object>',
  '<p style="position:fixed;inset:0">overlay</p>',
]

/** Features a sanitizer must strip; each returns true when present. */
const DANGERS: Record<string, (el: HTMLElement) => boolean> = {
  script: (el) => !!el.querySelector('script'),
  iframe: (el) => !!el.querySelector('iframe,object,embed,form'),
  handler: (el) =>
    [...el.querySelectorAll('*')].some((n) => [...n.attributes].some((a) => /^on/i.test(a.name))),
  jsUrl: (el) =>
    [...el.querySelectorAll('[href],[src],[xlink\\:href]')].some((n) =>
      /^\s*(java|vb)\s*script:/i.test(
        (n.getAttribute('href') || n.getAttribute('src') || '').replace(/[\s\u0000-\u001f]/g, ''),
      ),
    ),
  dataHtml: (el) =>
    [...el.querySelectorAll('[src]')].some((n) => /^data:text/i.test(n.getAttribute('src') || '')),
  style: (el) => !!el.querySelector('style,[style]'),
  svgMath: (el) => !!el.querySelector('svg,math'),
  modelTarget: (el) =>
    [...el.querySelectorAll('a[target]')].some((a) => a.getAttribute('target') !== '_blank'),
}

describe('renderMarkdown sanitizer', () => {
  it('neutralises the injected-markup scenario', () => {
    const el = dom(renderMarkdown('<img src=x onerror=alert(1)> and [link](javascript:alert(1))'))
    expect(
      el
        .querySelector('img')
        ?.getAttributeNames()
        .some((n) => n.startsWith('on')),
    ).not.toBe(true)
    expect(el.innerHTML).not.toMatch(/javascript:/i)
  })

  it.each(HOSTILE)('strips at least as much as the legacy sanitizer: %s', (input) => {
    const legacy = dom(legacyRender(input))
    const next = dom(renderMarkdown(input))
    for (const [name, present] of Object.entries(DANGERS)) {
      if (!present(legacy)) expect(present(next), `${name} in ${next.innerHTML}`).toBe(false)
    }
    // And never any of these, whatever the legacy output did.
    for (const name of ['script', 'iframe', 'handler', 'jsUrl', 'dataHtml']) {
      expect(DANGERS[name]!(next), `${name} in ${next.innerHTML}`).toBe(false)
    }
  })

  it('opens links to another origin in a new tab with noopener noreferrer', () => {
    const el = dom(
      renderMarkdown('[docs](https://example.org/x) [here](/control/home)', 'http://localhost'),
    )
    const [external, local] = [...el.querySelectorAll('a')]
    expect(external).toHaveAttribute('target', '_blank')
    expect(external).toHaveAttribute('rel', 'noopener noreferrer')
    expect(local).not.toHaveAttribute('target')
  })

  it('turns remote images into links and keeps data images', () => {
    const el = dom(renderMarkdown('![chart](https://img.example/c.png)'))
    expect(el.querySelector('img')).toBeNull()
    expect(el.querySelector('a')).toHaveAttribute('href', 'https://img.example/c.png')
  })

  it('wraps tables so they scroll on their own', () => {
    const el = dom(renderMarkdown('| a | b |\n| - | - |\n| 1 | 2 |'))
    expect(el.querySelector('.table-scroll > table')).not.toBeNull()
  })

  it('keeps a written-out tool call readable', () => {
    expect(dom(renderMarkdown('<invoke name="x">y</invoke>')).textContent).toContain('<invoke')
  })
})

describe('code blocks', () => {
  it('highlights a fenced python block with theme token classes', () => {
    const el = dom(renderMarkdown('```python\ndef f():\n    return 1\n```'))
    const code = el.querySelector('pre code')!
    expect(code).toHaveClass('hljs', 'language-python')
    expect(code.querySelector('.hljs-keyword')).not.toBeNull()
  })

  it('maps every highlight class to a contrast-checked theme token', () => {
    const css = readFileSync('src/web/styles/transcript.css', 'utf8')
    for (const token of [
      '--hl-keyword',
      '--hl-string',
      '--hl-number',
      '--hl-comment',
      '--hl-title',
    ]) {
      expect(css).toContain(`var(${token})`)
    }
    expect(css).not.toMatch(/#[0-9a-f]{3,6}\b/i)
  })

  it('copies the code with the Copy button', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    render(<Markdown text={'```python\nprint("hi")\n```'} />)
    await user.click(screen.getByRole('button', { name: 'Copy code' }))
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(expect.stringContaining('print("hi")')),
    )
  })
})
