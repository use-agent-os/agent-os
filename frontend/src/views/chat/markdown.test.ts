import { describe, expect, it } from 'vitest'
import { render } from './markdown'

function fragment(markdown: string): HTMLDivElement {
  const root = document.createElement('div')
  root.innerHTML = render(markdown)
  return root
}

describe('chat markdown renderer', () => {
  it('renders GFM emphasis, lists, links, and inline code', () => {
    const root = fragment(
      '**bold** and *italic* with `value`\n\n- one\n- two\n\n[docs](https://example.com)',
    )

    expect(root.querySelector('strong')?.textContent).toBe('bold')
    expect(root.querySelector('em')?.textContent).toBe('italic')
    expect(root.querySelector('code.inline-code')?.textContent).toBe('value')
    expect(Array.from(root.querySelectorAll('li')).map((item) => item.textContent)).toEqual([
      'one',
      'two',
    ])
    expect(root.querySelector('a')?.getAttribute('href')).toBe('https://example.com')
  })

  it('opens external links in a new tab', () => {
    // Following a link in place unmounts the transcript, losing scroll
    // position and any in-flight turn — painful for a link the assistant just
    // asked the user to open, like an OAuth approval page.
    const root = fragment('[approve](https://accounts.x.ai/oauth2/device?user_code=ABCD)')
    const link = root.querySelector('a')

    expect(link?.getAttribute('target')).toBe('_blank')
    expect(link?.getAttribute('rel')).toContain('noopener')
    expect(link?.getAttribute('rel')).toContain('noreferrer')
  })

  it('leaves in-page and app-relative links alone', () => {
    const root = fragment('[top](#section) and [settings](/control/setup)')
    const targets = Array.from(root.querySelectorAll('a')).map((a) => a.getAttribute('target'))

    expect(targets).toEqual([null, null])
  })

  it('does not let the model choose its own browsing context', () => {
    const root = fragment('<a href="/control/setup" target="_top">go</a>')
    expect(root.querySelector('a')?.getAttribute('target')).toBeNull()
  })

  it('sanitizes scripts, event handlers, and unsafe link protocols', () => {
    const root = fragment(
      '<script>alert(1)</script><img src="x" onerror="alert(2)"><a href="javascript:alert(3)">bad</a>',
    )

    expect(root.querySelector('script')).toBeNull()
    expect(root.innerHTML).not.toContain('onerror')
    expect(root.querySelector('img')?.getAttribute('src')).toBe('x')
    expect(root.querySelector('a')?.hasAttribute('href')).toBe(false)
  })

  it('wraps fenced code with a language label and copy target', () => {
    const root = fragment('```js\nconst answer = 42\n```')
    const block = root.querySelector('.code-block')
    const pre = block?.querySelector('pre')
    const button = block?.querySelector<HTMLButtonElement>('.copy-btn')

    expect(block).not.toBeNull()
    expect(block?.querySelector('.code-lang')?.textContent).toBe('js')
    expect(pre?.querySelector('code.language-js')?.textContent).toContain('const answer = 42')
    expect(button?.dataset.target).toBe(pre?.id)
  })

  it('draws an agentos-qr image here instead of asking a web service for it', () => {
    const root = fragment('![QR Main](agentos-qr:0x89e0fA1B2c3D4e5F60718293a4b5C6d7E8f9da97)')
    const image = root.querySelector('img')

    expect(image?.getAttribute('alt')).toBe('QR Main')
    const src = image?.getAttribute('src') || ''
    expect(src.startsWith('data:image/svg+xml;base64,')).toBe(true)
    expect(atob(src.slice('data:image/svg+xml;base64,'.length))).toContain('<svg')
  })

  it('turns a remote image into a link, so no host is told about the chat', () => {
    // What an assistant reaches for unprompted: the wallet address handed to a
    // QR service in a query string, which also never loads under the CSP.
    const root = fragment(
      '![QR](https://api.qrserver.com/v1/create-qr-code/?data=0x89e0fA1B2c3D4e5F60718293a4b5C6d7E8f9da97)',
    )

    expect(root.querySelector('img')).toBeNull()
    const link = root.querySelector('.msg-extimg a')
    expect(link?.getAttribute('href')).toContain('api.qrserver.com')
    expect(link?.getAttribute('rel')).toBe('noreferrer noopener')
    expect(link?.textContent).toBe('QR')
  })

  it('names the host when a remote image has no alt text to show', () => {
    const root = fragment('![](https://example.com/a.png)')
    expect(root.querySelector('.msg-extimg a')?.textContent).toBe('example.com')
  })

  it('leaves data: and blob: images alone — they are already in hand', () => {
    const root = fragment('![a](data:image/png;base64,iVBORw0KGgo=)')
    expect(root.querySelector('img')?.getAttribute('src')).toContain('data:image/png')
  })

  it('restores LaTeX-ish spans without letting marked consume them', () => {
    const root = fragment('Keep $x^2$ and $$y_1 + y_2$$ intact.')
    const math = Array.from(root.querySelectorAll('code.math-raw')).map((node) => node.textContent)

    expect(math).toEqual(['$x^2$', '$$y_1 + y_2$$'])
  })

  it('keeps a tool call written as text readable instead of dropping its tags', () => {
    // Markup that reaches the console never ran; the user must see it as written.
    const inline = fragment(
      'Checking.\n<tool_call>{"name":"read_file","arguments":{"path":"a"}}</tool_call>',
    )
    expect(inline.textContent).toContain('<tool_call>{"name":"read_file"')
    expect(inline.textContent).toContain('</tool_call>')

    const block = fragment('<invoke name="exec">\n<parameter name="cmd">ls</parameter>\n</invoke>')
    expect(block.textContent).toContain('<invoke name="exec">')
    expect(block.textContent).toContain('<parameter name="cmd">ls</parameter>')
    expect(block.querySelector('invoke, parameter')).toBeNull()
  })

  it('leaves ordinary html and code examples of tool calls alone', () => {
    const root = fragment('<b>bold</b> and `<invoke name="x">`')

    expect(root.querySelector('b')?.textContent).toBe('bold')
    expect(root.querySelector('code.inline-code')?.textContent).toBe('<invoke name="x">')
  })
})
