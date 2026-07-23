import { useRef, useState } from 'react'
import { CheckIcon, CopyIcon } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'

// Common terminal command line: `$ <command>` with an integrated copy action.
// Copy semantics carry the legacy UI.toast contract (health.js:35-62 +
// components.js UI.toast): clipboard API with an execCommand textarea
// fallback, 1600ms ok / 2500ms err toasts, stable per-surface toast ids so
// identical toasts dedupe instead of stacking. Views pass their own
// `toastIdPrefix` to scope the dedupe.
function copyText(text: string): Promise<void> {
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

export function CommandLine({
  command,
  toastIdPrefix = 'cmd-copy',
}: {
  command: string
  toastIdPrefix?: string
}) {
  const [copied, setCopied] = useState(false)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  async function onCopy(): Promise<void> {
    if (!command) return
    try {
      await copyText(command)
      toast.success('Copied command', { id: `${toastIdPrefix}-ok`, duration: 1600 })
      setCopied(true)
      if (resetTimer.current) clearTimeout(resetTimer.current)
      resetTimer.current = setTimeout(() => setCopied(false), 1400)
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Copy failed: ' + message, { id: `${toastIdPrefix}-err`, duration: 2500 })
    }
  }

  return (
    <span className="cmdline">
      <span className="cmdline__prompt" aria-hidden="true">
        $
      </span>
      <code>{command}</code>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        className="cmdline__copy"
        title="Copy command"
        aria-label="Copy command"
        onClick={() => void onCopy()}
      >
        {copied ? <CheckIcon className="text-ok" /> : <CopyIcon />}
      </Button>
    </span>
  )
}
