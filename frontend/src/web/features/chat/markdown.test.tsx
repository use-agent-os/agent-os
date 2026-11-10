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

const ORIGIN = 'http://localhost:8765'

/** Every element that would make the browser fetch something on render. */
function autoLoads(el: HTMLElement): string[] {
  return [...el.querySelectorAll('img[src], input[src], [srcset]')]
    .map((n) => n.getAttribute('src') || '')
    .filter((src) => !/^(?:data:image\/(?:png|gif|jpeg|webp)[;,]|blob:)/i.test(src))
}

describe('model output cannot load remote resources', () => {
  it.each([
    '![a](//evil.example/a.png)',
    '<img src="//evil.example/p.png">',
    '<img src="\\\\evil.example/a.png">',
    '<img src="/\\evil.example/a.png">',
    '<img src="HTTPS://evil.example/a.png">',
    '<img src=" https://evil.example/a.png">',
    '<img src="/api/v1/sessions?delete=1">',
    '<img src="relative/path.png">',
    '<input type="image" src="https://evil.example/t.gif">',
    '<input src="//evil.example/t.gif">',
  ])('does not auto-load %s', (input) => {
    const el = dom(renderMarkdown(input, ORIGIN))
    expect(autoLoads(el), el.innerHTML).toEqual([])
  })

  it('shows a protocol-relative image as a link to the host it resolves to', () => {
    const el = dom(renderMarkdown('![pic](//evil.example/a.png)', ORIGIN))
    expect(el.querySelector('img')).toBeNull()
    expect(el.querySelector('a')).toHaveAttribute('href', 'http://evil.example/a.png')
  })

  it('keeps inline raster and blob images', () => {
    const png = 'data:image/png;base64,iVBORw0KGgo='
    const el = dom(renderMarkdown(`![p](${png})\n\n<img src="blob:${ORIGIN}/x">`, ORIGIN))
    expect([...el.querySelectorAll('img')].map((i) => i.getAttribute('src'))).toEqual([
      png,
      `blob:${ORIGIN}/x`,
    ])
  })

  it('locks every input as a disabled checkbox', () => {
    const el = dom(
      renderMarkdown('- [x] done\n- [ ] todo\n\n<input type="text" value="x">', ORIGIN),
    )
    const inputs = [...el.querySelectorAll('input')]
    expect(inputs).toHaveLength(3)
    for (const input of inputs) {
      expect(input).toHaveAttribute('type', 'checkbox')
      expect(input).toBeDisabled()
    }
    expect(inputs.map((i) => i.hasAttribute('checked'))).toEqual([true, false, false])
  })

  it('opens protocol-relative links in a new tab, in-app paths in place', () => {
    const el = dom(
      renderMarkdown(
        '<a href="//evil.example/x">rel</a> [in](/settings) [same](http://localhost:8765/x)',
        ORIGIN,
      ),
    )
    const [rel, inApp, same] = [...el.querySelectorAll('a')]
    expect(rel).toHaveAttribute('target', '_blank')
    expect(rel).toHaveAttribute('rel', 'noopener noreferrer')
    expect(inApp).not.toHaveAttribute('target')
    expect(same).not.toHaveAttribute('target')
  })

  it('drops model-written classes but keeps the renderer’s own', () => {
    const el = dom(
      renderMarkdown(
        '<div class="fixed inset-0 z-50 bg-surface-1"><a href="https://evil.example">Session expired – sign in</a></div>\n\n```python\ndef f(): pass\n```',
        ORIGIN,
      ),
    )
    expect(el.querySelector('.fixed, .inset-0, .z-50')).toBeNull()
    expect(el.querySelector('.code-block .hljs')).not.toBeNull()
    expect(el.querySelector('.hljs-keyword')).not.toBeNull()
  })
})

describe('math and QR placeholders', () => {
  it('leaves text that looks like a stash marker alone', () => {
    const el = dom(renderMarkdown('The Apple M4 chip costs $x$ more than the M1 and M0.', ORIGIN))
    expect(el.textContent?.trim()).toBe('The Apple M4 chip costs $x$ more than the M1 and M0.')
    expect(el.querySelectorAll('code.math-raw')).toHaveLength(1)
  })

  it('cannot forge a math or QR marker', () => {
    const el = dom(renderMarkdown('\uE000abc:0\uE001 ![q](#qr-abc-0) $y$', ORIGIN))
    expect(el.querySelectorAll('code.math-raw')).toHaveLength(1)
    expect(el.querySelector('img')).toBeNull()
  })

  it('renders an agentos-qr: image as a locally drawn picture', () => {
    const el = dom(renderMarkdown('![wallet](agentos-qr:0xabc)', ORIGIN))
    const img = el.querySelector('img')
    expect(img).toHaveAttribute('alt', 'wallet')
    expect(img?.getAttribute('src')).toMatch(/^data:image\/svg\+xml/)
  })

  it('still strips an SVG data image the model wrote itself', () => {
    const el = dom(renderMarkdown('![x](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)', ORIGIN))
    expect(el.querySelector('img')?.getAttribute('src') ?? null).toBeNull()
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
