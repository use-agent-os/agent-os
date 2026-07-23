import {
  IconBrandDiscord,
  IconBrandSlack,
  IconBrandTeams,
  IconBrandTelegram,
  IconPlugConnected,
  type TablerIcon,
} from '@tabler/icons-react'

const ADAPTER_LOGOS: Record<string, { icon: TablerIcon; key: string }> = {
  discord: { icon: IconBrandDiscord, key: 'discord' },
  microsoftteams: { icon: IconBrandTeams, key: 'teams' },
  msteams: { icon: IconBrandTeams, key: 'teams' },
  slack: { icon: IconBrandSlack, key: 'slack' },
  telegram: { icon: IconBrandTelegram, key: 'telegram' },
}

function normalizedAdapterType(type: string): string {
  return type
    .trim()
    .toLowerCase()
    .replace(/[\s_-]+/g, '')
}

export function AdapterLogo({ type, className }: { type: string; className?: string }) {
  const logo = ADAPTER_LOGOS[normalizedAdapterType(type)]
  const Icon = logo?.icon ?? IconPlugConnected

  return (
    <Icon
      className={className}
      data-adapter-logo={logo?.key ?? 'generic'}
      stroke={1.8}
      aria-hidden="true"
      focusable="false"
    />
  )
}
