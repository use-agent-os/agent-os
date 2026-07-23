import {
  IconBrandDingtalk,
  IconBrandDiscord,
  IconBrandMatrix,
  IconBrandQq,
  IconBrandSlack,
  IconBrandTeams,
  IconBrandTelegram,
  IconBrandWechat,
  IconPlugConnected,
  type TablerIcon,
} from '@tabler/icons-react'

const ADAPTER_LOGOS: Record<string, { icon: TablerIcon; key: string }> = {
  dingtalk: { icon: IconBrandDingtalk, key: 'dingtalk' },
  discord: { icon: IconBrandDiscord, key: 'discord' },
  matrix: { icon: IconBrandMatrix, key: 'matrix' },
  microsoftteams: { icon: IconBrandTeams, key: 'teams' },
  msteams: { icon: IconBrandTeams, key: 'teams' },
  qq: { icon: IconBrandQq, key: 'qq' },
  qqbot: { icon: IconBrandQq, key: 'qq' },
  slack: { icon: IconBrandSlack, key: 'slack' },
  telegram: { icon: IconBrandTelegram, key: 'telegram' },
  wechat: { icon: IconBrandWechat, key: 'wechat' },
  wecom: { icon: IconBrandWechat, key: 'wechat' },
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
