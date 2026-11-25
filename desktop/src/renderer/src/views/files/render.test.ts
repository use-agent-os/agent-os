import { describe, expect, it } from 'vitest'
import { highlightCode, renderMarkdown } from './render'

const labels = { remoteImage: 'Remote image not loaded:', localImage: 'Image:' }
const md = (text: string) => renderMarkdown(text, labels)
const dom = (text: string) => {
  const host = document.createElement('div')
  host.innerHTML = md(text)
  return host
}

describe('renderMarkdown', () => {
  it('renders ordinary markdown', () => {
    const host = dom('# Title\n\nSome **bold** text.\n\n- one\n- two\n')
    expect(host.querySelector('h1')?.textContent).toBe('Title')
    expect(host.querySelector('strong')?.textContent).toBe('bold')
    expect(host.querySelectorAll('li')).toHaveLength(2)
  })

  it('never lets markup in a file run or load', () => {
    const html = md(
      [
        '<script>window.pwned = 1</script>',
        '<img src="x" onerror="window.pwned = 1">',
        '<iframe src="https://evil.example"></iframe>',
        '<svg onload="window.pwned = 1"><script>1</script></svg>',
        '<style>body{display:none}</style>',
        '<form action="https://evil.example"><input name="q"><button>go</button></form>',
        '<a href="javascript:window.pwned = 1">x</a>',
        '<object data="x"></object><embed src="x">',
        '<p style="position:fixed;inset:0" id="root" class="files-panel">cover</p>',
        '<meta http-equiv="refresh" content="0;url=https://evil.example">',
        '<base href="https://evil.example/">',
      ].join('\n\n'),
    )
    expect(html).not.toMatch(
      /<script|<iframe|<svg|<style|<form|<input|<button|<object|<embed|<meta|<base/i,
    )
    expect(html).not.toMatch(/onerror|onload|javascript:|style=|id=|files-panel/i)
    expect(html).not.toMatch(/<img/i)
  })

  it('does not load remote images, and links to them instead', () => {
    const host = dom('![chart](https://tracker.example/pixel.png?u=1)')
    expect(host.querySelector('img')).toBeNull()
    const note = host.querySelector('.files-md__blocked')
    expect(note?.textContent).toContain('Remote image not loaded:')
    expect(note?.querySelector('a')?.getAttribute('href')).toBe(
      'https://tracker.example/pixel.png?u=1',
    )
  })

  it('does not resolve images stored beside the file', () => {
    const host = dom('![diagram](./img/diagram.png)')
    expect(host.querySelector('img')).toBeNull()
    expect(host.textContent).toContain('Image: diagram')
  })

  it('keeps inline raster data images but not inline SVG', () => {
    expect(dom('![p](data:image/png;base64,iVBORw0KGgo=)').querySelector('img')).not.toBeNull()
    expect(dom('![s](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)').querySelector('img')).toBeNull()
  })

  it('opens web links outside the app and leaves the rest as text', () => {
    const host = dom(
      '[web](https://example.com) [mail](mailto:a@b.co) [rel](./other.md) [top](#top) [f](file:///etc/passwd)',
    )
    const anchors = [...host.querySelectorAll('a')]
    const web = anchors.find((a) => a.textContent === 'web')
    expect(web?.getAttribute('target')).toBe('_blank')
    expect(web?.getAttribute('rel')).toBe('noreferrer noopener')
    expect(anchors.find((a) => a.textContent === 'mail')?.hasAttribute('href')).toBe(true)
    for (const label of ['rel', 'top', 'f']) {
      expect(anchors.find((a) => a.textContent === label)?.hasAttribute('href')).toBe(false)
    }
  })

  it('highlights fenced code and escapes what is in it', () => {
    const host = dom('```json\n{"a": "<script>x</script>"}\n```')
    const code = host.querySelector('pre > code')
    expect(code?.className).toBe('hljs')
    expect(code?.textContent).toBe('{"a": "<script>x</script>"}\n')
    expect(host.querySelector('script')).toBeNull()
    expect(code?.querySelector('.hljs-string')).not.toBeNull()
  })

  it('wraps tables so they scroll in their own box', () => {
    const host = dom('| a | b |\n| - | - |\n| 1 | 2 |\n')
    expect(host.querySelector('.files-md__table > table')).not.toBeNull()
  })

  it('shows task-list boxes as glyphs, not form controls', () => {
    const host = dom('- [x] done\n- [ ] todo\n')
    expect(host.querySelector('input')).toBeNull()
    expect(host.textContent).toContain('☑')
    expect(host.textContent).toContain('☐')
  })

  it('is empty for an empty file', () => {
    expect(md('')).toBe('')
  })
})

describe('highlightCode', () => {
  it('escapes source for an unknown language', () => {
    expect(highlightCode('<b>&"\'', 'klingon')).toBe('&lt;b&gt;&amp;&quot;&#39;')
    expect(highlightCode('<b>', undefined)).toBe('&lt;b&gt;')
  })
  it('highlights a known language without emitting the source as markup', () => {
    const html = highlightCode('const a = "<img src=x onerror=1>"', 'typescript')
    expect(html).toContain('hljs-keyword')
    expect(html).not.toContain('<img')
  })
  it('resolves aliases', () => {
    expect(highlightCode('a: 1', 'yml')).toContain('hljs-attr')
  })
  it('skips highlighting past the budget', () => {
    const big = 'x = 1\n'.repeat(40_000)
    expect(highlightCode(big, 'python')).not.toContain('<span')
  })
})
