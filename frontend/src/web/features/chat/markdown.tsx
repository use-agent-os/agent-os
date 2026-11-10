import DOMPurify, { type Config } from 'dompurify'
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import python from 'highlight.js/lib/languages/python'
import typescript from 'highlight.js/lib/languages/typescript'
import yaml from 'highlight.js/lib/languages/yaml'
import { Marked } from 'marked'
import { useCallback, useMemo, type MouseEvent } from 'react'
import { toast } from 'sonner'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { qrDataUrl } from '@/lib/qr'
import { cn } from '@/lib/utils'
import '@/web/styles/transcript.css'

// Chat Markdown (overhaul-control-ui D7, chat spec "Markdown is sanitized"):
// marked -> DOMPurify with an explicit allowlist -> controlled post-passes ->
// a final sanitize. highlight.js runs on the code text inside marked's
// renderer, so the highlighted markup goes through the sanitizer too, and
// its colours come from theme tokens (web/styles/transcript.css).

hljs.registerLanguage('bash', bash)
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('json', json)
hljs.registerLanguage('python', python)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('yaml', yaml)

const ALLOWED_TAGS = [
  'a',
  'abbr',
  'b',
  'blockquote',
  'br',
  'button',
  'code',
  'del',
  'details',
  'div',
  'em',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'hr',
  'i',
  'img',
  'input',
  'kbd',
  'li',
  'ol',
  'p',
  'pre',
  's',
  'span',
  'strong',
  'sub',
  'summary',
  'sup',
  'table',
  'tbody',
  'td',
  'tfoot',
  'th',
  'thead',
  'tr',
  'ul',
]
const ALLOWED_ATTR = [
  'href',
  'title',
  'alt',
  'src',
  'class',
  'colspan',
  'rowspan',
  'align',
  'type',
  'checked',
  'disabled',
  'data-copy',
  'aria-label',
  'target',
  'rel',
  'start',
]
/** http(s), mailto, in-app relative/anchor links, and inline data/blob images. */
const ALLOWED_URI =
  /^(?:(?:https?|mailto):|data:image\/(?:png|gif|jpeg|webp);|blob:|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$))/i

const PURIFY: Config = {
  ALLOWED_TAGS,
  ALLOWED_ATTR,
  ALLOWED_URI_REGEXP: ALLOWED_URI,
  ALLOW_DATA_ATTR: false,
  FORBID_TAGS: ['style', 'script', 'iframe', 'object', 'embed', 'form', 'svg', 'math'],
  FORBID_ATTR: ['style'],
}

function escapeHtml(text: string): string {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

// Code spans win at the same position, so `$` inside code is never math.
const MATH_SCAN =
  /(```[\s\S]*?```|`[^`\n]+?`|\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([^)\n]+?\\\)|\$(?![\s\d])(?:\\\$|[^$\n])+?(?<![\s])\$)/g

/**
 * A per-render token wrapped in private-use characters: the stash markers
 * can neither collide with ordinary text ("M4") nor be forged by it.
 */
function renderNonce(): string {
  return Math.random().toString(36).slice(2, 10)
}

function stashMath(text: string, nonce: string): { text: string; stash: string[] } {
  const stash: string[] = []
  const out = text.replace(MATH_SCAN, (match) => {
    if (match.startsWith('`')) return match
    stash.push(match)
    return `\uE000${nonce}:${stash.length - 1}\uE001`
  })
  return { text: out, stash }
}

function restoreMath(html: string, stash: string[], nonce: string): string {
  if (stash.length === 0) return html
  const marker = new RegExp(`\uE000${nonce}:(\\d+)\uE001`, 'g')
  return html.replace(marker, (whole, i: string) => {
    const raw = stash[Number(i)]
    if (raw === undefined) return whole
    return `<code class="math-raw" title="${escapeHtml(t('webChat.mathTitle'))}">${escapeHtml(raw)}</code>`
  })
}

/**
 * `![alt](agentos-qr:payload)` -> a QR drawn locally. Its `data:image/svg+xml`
 * source would be stripped like any model-supplied SVG, so the Markdown
 * carries an in-page reference (`#qr-<nonce>-<n>`) and the source is set
 * after sanitizing, from this list only.
 */
const QR_IMAGE = /!\[([^\]\n]*)\]\(\s*agentos-qr:([^)\s]+)\s*\)/g
function stashQrImages(text: string, nonce: string): { text: string; urls: string[] } {
  const urls: string[] = []
  if (!text.includes('agentos-qr:')) return { text, urls }
  const out = text.replace(QR_IMAGE, (whole, alt: string, payload: string) => {
    let data = payload
    try {
      data = decodeURIComponent(payload)
    } catch {
      /* take the bytes as written */
    }
    if (!data || data.length > 512) return whole
    try {
      urls.push(qrDataUrl(data))
    } catch {
      return whole
    }
    return `![${alt}](${qrPrefix(nonce)}${urls.length - 1})`
  })
  return { text: out, urls }
}

function qrPrefix(nonce: string): string {
  return `#qr-${nonce}-`
}

function placeQrImages(root: DocumentFragment, urls: string[], nonce: string): void {
  const prefix = qrPrefix(nonce)
  for (const image of root.querySelectorAll('img[src]')) {
    const src = image.getAttribute('src') || ''
    if (!src.startsWith(prefix)) continue
    const url = urls[Number(src.slice(prefix.length))]
    if (url) image.setAttribute('src', url)
    else image.removeAttribute('src')
  }
}

// Tool-call tags the model wrote as text stay readable instead of vanishing.
const TEXT_TOOL_CALL_TAG =
  /<\/?\s*(?:invoke|parameter|tool_calls?|tvoe_calls|function|minimax:tool_call)(?![\w-])/i

function highlight(code: string, lang: string): string {
  const language = lang && hljs.getLanguage(lang) ? lang : null
  try {
    return language ? hljs.highlight(code, { language }).value : escapeHtml(code)
  } catch {
    return escapeHtml(code)
  }
}

const marked = new Marked({
  gfm: true,
  breaks: true,
  renderer: {
    html({ text }) {
      if (!TEXT_TOOL_CALL_TAG.test(text)) return text
      return escapeHtml(text).replace(/\n/g, '<br>')
    },
    code({ text, lang }) {
      const language = (lang || '').trim().split(/\s+/)[0]?.toLowerCase() ?? ''
      const safeLang = /^[\w+-]{1,32}$/.test(language) ? language : ''
      const languageClass = safeLang ? ` language-${safeLang}` : ''
      return (
        '<div class="code-block">' +
        '<div class="code-block-header">' +
        `<span>${escapeHtml(safeLang)}</span>` +
        `<button type="button" class="copy-btn" data-copy="code" aria-label="${escapeHtml(t('webChat.copyCode'))}">${escapeHtml(t('webChat.copy'))}</button>` +
        '</div>' +
        `<pre><code class="hljs${languageClass}">${highlight(text, safeLang)}</code></pre>` +
        '</div>'
      )
    },
  },
})

/** Wide tables scroll inside their own container. */
function wrapTables(root: DocumentFragment): void {
  for (const table of root.querySelectorAll('table')) {
    const wrapper = root.ownerDocument!.createElement('div')
    wrapper.className = 'table-scroll'
    table.replaceWith(wrapper)
    wrapper.appendChild(table)
  }
}

/** Only inline raster `data:` images and local `blob:` objects load. */
const LOCAL_IMAGE = /^(?:data:image\/(?:png|gif|jpeg|webp)[;,]|blob:)/i

function resolve(url: string, origin: string): URL | null {
  try {
    return new URL(url, origin)
  } catch {
    return null
  }
}

/**
 * Every other image becomes a link: loading it would send a request the
 * model chose (to another host, carrying conversation data, or to the
 * gateway itself) without the reader asking. The source is resolved the way
 * the browser would, so protocol-relative (`//host`) and backslash forms
 * cannot slip past a prefix check.
 */
function linkRemoteImages(root: DocumentFragment, origin: string, qrPrefix: string): void {
  for (const image of root.querySelectorAll('img')) {
    const src = (image.getAttribute('src') || '').trim()
    if (!src) continue
    if (LOCAL_IMAGE.test(src) || src.startsWith(qrPrefix)) continue
    const url = resolve(src, origin)
    if (!url || (url.protocol !== 'http:' && url.protocol !== 'https:')) {
      image.removeAttribute('src')
      continue
    }
    const note = root.ownerDocument!.createElement('span')
    note.className = 'ext-image'
    note.title = t('webChat.externalImageTitle')
    note.append(`${t('webChat.externalImage')} `)
    const link = root.ownerDocument!.createElement('a')
    link.setAttribute('href', url.href)
    link.textContent = image.getAttribute('alt') || url.host
    note.appendChild(link)
    image.replaceWith(note)
  }
}

/**
 * The only input Markdown produces is a task-list checkbox: any other kind
 * (`type=image` loads its `src`) is turned into one, and none is editable.
 */
function lockInputs(root: DocumentFragment): void {
  for (const input of root.querySelectorAll('input')) {
    const checked = input.hasAttribute('checked')
    for (const name of input.getAttributeNames()) input.removeAttribute(name)
    input.setAttribute('type', 'checkbox')
    input.setAttribute('disabled', '')
    if (checked) input.setAttribute('checked', '')
  }
}

/** Classes the renderer itself writes; anything else came from the model. */
const OWN_CLASS =
  /^(?:code-block|code-block-header|copy-btn|hljs|hljs-[\w-]+|[a-z]+_|language-[\w+-]+|math-raw|table-scroll|ext-image)$/

/**
 * Model-written classes go: with the console's utility classes a reply could
 * otherwise lay a fake sign-in panel over the whole page.
 */
function stripForeignClasses(root: DocumentFragment): void {
  for (const el of root.querySelectorAll('[class]')) {
    const kept = (el.getAttribute('class') || '').split(/\s+/).filter((c) => OWN_CLASS.test(c))
    if (kept.length) el.setAttribute('class', kept.join(' '))
    else el.removeAttribute('class')
  }
}

/**
 * Links to another origin open in a new tab with noopener/noreferrer; any
 * `target` the model chose itself is removed. Links are classified after
 * resolving them, so `//host` counts as the other host it is.
 */
function routeLinks(root: DocumentFragment, origin: string): void {
  for (const anchor of root.querySelectorAll('a[href]')) {
    anchor.removeAttribute('target')
    anchor.removeAttribute('rel')
    const url = resolve(anchor.getAttribute('href') || '', origin)
    if (!url || (url.protocol !== 'http:' && url.protocol !== 'https:')) continue
    if (url.origin === origin) continue
    anchor.setAttribute('target', '_blank')
    anchor.setAttribute('rel', 'noopener noreferrer')
  }
}

/** Render untrusted Markdown to sanitized HTML. */
export function renderMarkdown(text: string, origin: string = window.location.origin): string {
  if (!text) return ''
  const nonce = renderNonce()
  let html: string
  let stash: string[] = []
  let qr: string[] = []
  try {
    const withQr = stashQrImages(text, nonce)
    qr = withQr.urls
    const stashed = stashMath(withQr.text, nonce)
    stash = stashed.stash
    html = String(marked.parse(stashed.text, { async: false }))
  } catch {
    html = `<p>${escapeHtml(text).replace(/\n/g, '<br>')}</p>`
  }
  const template = document.createElement('template')
  template.innerHTML = String(DOMPurify.sanitize(html, PURIFY))
  stripForeignClasses(template.content)
  lockInputs(template.content)
  linkRemoteImages(template.content, origin, qrPrefix(nonce))
  routeLinks(template.content, origin)
  // Renderer markup is added only now, past the class filter.
  template.innerHTML = restoreMath(template.innerHTML, stash, nonce)
  wrapTables(template.content)
  // The post-passes only add controlled markup; sanitize once more last.
  template.innerHTML = String(DOMPurify.sanitize(template.innerHTML, PURIFY))
  // Drawn here, not by the model: set after the last sanitize.
  placeQrImages(template.content, qr, nonce)
  return template.innerHTML
}

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text)
  throw new Error('Clipboard unavailable')
}

/** Sanitized Markdown with working code-block Copy buttons. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const html = useMemo(() => renderMarkdown(text), [text])
  const onClick = useCallback((e: MouseEvent<HTMLDivElement>) => {
    const button = (e.target as HTMLElement).closest<HTMLButtonElement>('button[data-copy="code"]')
    if (!button) return
    const code = button.closest('.code-block')?.querySelector('pre')?.textContent ?? ''
    void copyText(code)
      .then(() => toast.success(t('webChat.copied')))
      .catch(() => toast.error(t('webChat.copyFailed')))
  }, [])
  return (
    <div
      className={cn('prose-chat', className)}
      onClick={onClick}
      // Sanitized by renderMarkdown (DOMPurify allowlist, twice).
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
