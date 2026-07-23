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

  it('restores LaTeX-ish spans without letting marked consume them', () => {
    const root = fragment('Keep $x^2$ and $$y_1 + y_2$$ intact.')
    const math = Array.from(root.querySelectorAll('code.math-raw')).map((node) => node.textContent)

    expect(math).toEqual(['$x^2$', '$$y_1 + y_2$$'])
  })
})
