import {
  Bot,
  Clock,
  Globe,
  Hash,
  MessageCircle,
  MessagesSquare,
  Send,
  Smartphone,
} from 'lucide-react'
import { t } from '@/i18n'
import '@/i18n/en/webShell'

const ICONS: Record<string, typeof MessageCircle> = {
  webchat: MessageCircle,
  telegram: Send,
  discord: MessagesSquare,
  slack: Hash,
  whatsapp: Smartphone,
  signal: Smartphone,
  cron: Clock,
  subagent: Bot,
}

/** A channel indicator: an outline icon with the channel named for AT. */
export function ChannelIcon({ channel, className }: { channel: string; className?: string }) {
  const Icon = ICONS[channel] ?? Globe
  const label = t('webShell.channelLabel', { channel })
  return (
    <span role="img" aria-label={label} title={label} className={className}>
      <Icon className="size-3.5" aria-hidden />
    </span>
  )
}
