// Copy with a non-secure-context fallback: the clipboard API when the page has
// it, else a hidden-textarea execCommand, so copying still works when the
// console is not served from a secure context.
export function copyWithFallback(text: string): Promise<void> {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    return navigator.clipboard.writeText(text)
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.position = 'fixed'
  ta.style.left = '-9999px'
  document.body.appendChild(ta)
  ta.select()
  const ok = document.execCommand('copy')
  document.body.removeChild(ta)
  return ok ? Promise.resolve() : Promise.reject(new Error('Copy command failed'))
}
