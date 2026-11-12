// Pieces the hero composer (`/new`) and the docked composer share: the
// attachment tray, the toolbar chip and the round send/stop button.
import { ArrowUp, ChevronDown, FileText, Loader2, Square, X } from 'lucide-react'
import type { ComponentProps, ReactNode } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { cn } from '@/lib/utils'
import type { PendingAttachment } from '@/views/chat/logic'
import { SimpleTooltip } from '@/web/components/ui/tooltip'
import '@/web/styles/chat-stage.css'

export function isUploading(att: PendingAttachment): boolean {
  return att.kind === 'uploading' || att.kind === 'inline_pending'
}

function formatSize(bytes: number): string {
  if (!bytes) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function extension(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot > 0 && dot > name.length - 6 ? name.slice(dot + 1).toUpperCase() : ''
}

/** Staged files inside the composer card, above its text. */
export function AttachmentTray({
  attachments,
  rejection,
  onRemove,
}: {
  attachments: PendingAttachment[]
  rejection?: string | null
  onRemove(localId: number): void
}) {
  if (!attachments.length && !rejection) return null
  return (
    <div className="chat-tray" aria-label={t('webChat.attach')}>
      {attachments.map((att) => {
        const meta = [extension(att.name), formatSize(att.size)].filter(Boolean).join(' · ')
        return (
          <span
            key={att.local_id}
            data-attachment-state={att.kind}
            className="chat-chip anim-scale-in origin-bottom-left"
          >
            <span className="chat-chip-thumb" aria-hidden>
              {isUploading(att) ? (
                <Loader2 className="motion-status size-4 animate-spin text-accent" />
              ) : att.dataUrl && att.mime.startsWith('image/') ? (
                <img src={att.dataUrl} alt="" className="size-full object-cover" />
              ) : (
                <FileText className="size-4" />
              )}
            </span>
            <span className="flex min-w-0 flex-col leading-tight">
              <span className="truncate font-medium text-text">{att.name}</span>
              {meta ? <span className="truncate text-text-muted tabular-nums">{meta}</span> : null}
            </span>
            <button
              type="button"
              aria-label={t('webChat.removeAttachment', { name: att.name })}
              onClick={() => onRemove(att.local_id)}
              className="chat-chip-remove target"
            >
              <X className="size-3.5" aria-hidden />
            </button>
          </span>
        )
      })}
      {rejection ? (
        <p role="alert" className="w-full text-small text-danger">
          {rejection}
        </p>
      ) : null}
    </div>
  )
}

/**
 * A quiet chip in the composer toolbar that opens a menu (agent, project,
 * model route). Spread into a Radix `DropdownMenuTrigger asChild`.
 */
export function ToolbarChip({
  icon,
  label,
  className,
  ...props
}: ComponentProps<'button'> & { icon: ReactNode; label: ReactNode }) {
  return (
    <button type="button" className={cn('chat-tool-chip target', className)} {...props}>
      {icon}
      <span className="chat-tool-chip-label">{label}</span>
      <ChevronDown className="chat-tool-chip-chevron" aria-hidden />
    </button>
  )
}

/**
 * The round primary of a composer. One element for send and stop, so the
 * change between them is a morph (colour, icon cross-fade), not a swap.
 * A disabled send says why in its tooltip and accessible description.
 */
export function SendButton({
  mode,
  label,
  disabled,
  busy,
  reason,
  reasonId,
  reasonDisplay = 'hidden',
  size = 'md',
  onClick,
}: {
  mode: 'send' | 'stop'
  label: string
  disabled?: boolean
  busy?: boolean
  /** Why the button is disabled, or null. */
  reason?: string | null
  /** Id for the reason element (the button's description). */
  reasonId: string
  /**
   * Where the reason text lives: printed beside the button, visually hidden
   * (tooltip only), or rendered by the caller under `reasonId`.
   */
  reasonDisplay?: 'inline' | 'hidden' | 'external'
  size?: 'md' | 'lg'
  onClick(): void
}) {
  const button = (
    <button
      type="button"
      aria-label={label}
      aria-describedby={reason ? reasonId : undefined}
      disabled={disabled}
      data-mode={mode}
      data-size={size}
      onClick={onClick}
      className="chat-send target"
    >
      {busy ? (
        <Loader2 className="motion-status size-[18px] animate-spin" aria-hidden />
      ) : (
        <>
          <ArrowUp className="chat-send-icon" data-icon="send" strokeWidth={2.4} aria-hidden />
          <Square className="chat-send-icon fill-current" data-icon="stop" aria-hidden />
        </>
      )}
    </button>
  )
  return (
    <>
      {reason && reasonDisplay !== 'external' ? (
        <span
          id={reasonId}
          className={cn(
            reasonDisplay === 'inline'
              ? 'min-w-0 truncate px-1 text-small text-text-muted'
              : 'sr-only',
          )}
        >
          {reason}
        </span>
      ) : null}
      {/* A disabled button gets no pointer events: the tooltip hangs on a wrapper. */}
      <SimpleTooltip side="top" label={reason && disabled ? reason : label}>
        <span className="chat-send-slot">{button}</span>
      </SimpleTooltip>
    </>
  )
}
