import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import c from 'highlight.js/lib/languages/c'
import cpp from 'highlight.js/lib/languages/cpp'
import csharp from 'highlight.js/lib/languages/csharp'
import css from 'highlight.js/lib/languages/css'
import diff from 'highlight.js/lib/languages/diff'
import django from 'highlight.js/lib/languages/django'
import dockerfile from 'highlight.js/lib/languages/dockerfile'
import go from 'highlight.js/lib/languages/go'
import graphql from 'highlight.js/lib/languages/graphql'
import ini from 'highlight.js/lib/languages/ini'
import java from 'highlight.js/lib/languages/java'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import kotlin from 'highlight.js/lib/languages/kotlin'
import lua from 'highlight.js/lib/languages/lua'
import makefile from 'highlight.js/lib/languages/makefile'
import markdown from 'highlight.js/lib/languages/markdown'
import php from 'highlight.js/lib/languages/php'
import python from 'highlight.js/lib/languages/python'
import r from 'highlight.js/lib/languages/r'
import ruby from 'highlight.js/lib/languages/ruby'
import rust from 'highlight.js/lib/languages/rust'
import scss from 'highlight.js/lib/languages/scss'
import sql from 'highlight.js/lib/languages/sql'
import swift from 'highlight.js/lib/languages/swift'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'
import { HIGHLIGHT_MAX_CHARS } from '@/lib/workspace-files'

// The file viewer's languages: the gateway's `classify()` names one per
// extension (workspace_files._CODE_LANGUAGES). Registered once, in the
// viewer's own chunk; the chat's Markdown registers its few on the same core.

const LANGUAGES = {
  bash,
  c,
  cpp,
  csharp,
  css,
  diff,
  django,
  dockerfile,
  go,
  graphql,
  ini,
  java,
  javascript,
  json,
  kotlin,
  lua,
  makefile,
  markdown,
  php,
  python,
  r,
  ruby,
  rust,
  scss,
  sql,
  swift,
  typescript,
  xml,
  yaml,
}

for (const [name, language] of Object.entries(LANGUAGES)) {
  if (!hljs.getLanguage(name)) hljs.registerLanguage(name, language)
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/**
 * Source as highlighted HTML, or escaped text when the language is unknown
 * or the file is too long to colour without stalling the page. hljs escapes
 * the text and adds only its own spans; the sanitizer holds it to exactly
 * that, so a file can never put markup on the page.
 */
export function highlightSource(text: string, language: string | undefined): string {
  if (!language || !hljs.getLanguage(language) || text.length > HIGHLIGHT_MAX_CHARS) {
    return escapeHtml(text)
  }
  try {
    const html = hljs.highlight(text, { language, ignoreIllegals: true }).value
    return String(DOMPurify.sanitize(html, { ALLOWED_TAGS: ['span'], ALLOWED_ATTR: ['class'] }))
  } catch {
    return escapeHtml(text)
  }
}
