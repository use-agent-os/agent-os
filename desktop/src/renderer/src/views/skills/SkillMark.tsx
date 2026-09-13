import { Package } from 'lucide-react'
import { useState } from 'react'
import agentosMarkUrl from '@/assets/agentos-mark.png'
import aeonSymbolUrl from '@/assets/aeon-symbol.png'
import bankrSymbolUrl from '@/assets/bankr-symbol.svg'
import capminalSymbolUrl from '@/assets/capminal-symbol.svg'
import gmgnSymbolUrl from '@/assets/gmgn-symbol.png'
import robinhoodSymbolUrl from '@/assets/robinhood-symbol.png'
import { t as tw } from '@/i18n'
import {
  initials,
  isGmgnSkill,
  safeUrl,
  skillGroupKey,
  type RawSkill,
  type RegistryItem,
} from '@/views/skills/logic'
import { isPartnerBrand, PARTNER_LABEL, partnerBrandOf, type PartnerBrand } from './logic'

/**
 * Bundled brand artwork. A local import is nothing a SKILL.md could point at;
 * which skill wears which mark is the payload's call (`publisher.id`, resolved
 * server-side against an allowlist).
 */
const PARTNER_ASSET: Record<PartnerBrand, string> = {
  robinhood: robinhoodSymbolUrl,
  bankr: bankrSymbolUrl,
  aeon: aeonSymbolUrl,
  capminal: capminalSymbolUrl,
}

export type MarkSize = 'sm' | 'md' | 'lg'

/** A partner's mark; the brand's initial when the asset fails to load. */
export function BrandMark({
  brand,
  size = 'sm',
  decorative = true,
}: {
  brand: PartnerBrand
  size?: MarkSize
  decorative?: boolean
}) {
  const [broken, setBroken] = useState(false)
  const label = PARTNER_LABEL[brand]
  if (broken) {
    return (
      <span className="sk-mark sk-mark--initials" data-size={size} aria-hidden>
        {label.slice(0, 1)}
      </span>
    )
  }
  return (
    <img
      className="sk-mark sk-mark--img"
      data-size={size}
      src={PARTNER_ASSET[brand]}
      alt={decorative ? '' : `${label} logo`}
      draggable={false}
      onError={() => setBroken(true)}
    />
  )
}

/**
 * The mark for an installed skill, most specific first: the partner's brand,
 * then the GMGN upstream with the skill's own emoji as a corner badge (seven
 * GMGN skills would otherwise be seven identical marks), then the AgentOS
 * mark for the rest of the crypto group, then the generic package glyph.
 */
export function SkillMark({ skill, size = 'sm' }: { skill: RawSkill; size?: MarkSize }) {
  const brand = partnerBrandOf(skill)
  if (brand) return <BrandMark brand={brand} size={size} />
  const emoji = (skill.emoji ?? '').trim()
  if (isGmgnSkill(skill)) {
    return (
      <span className="sk-mark-wrap">
        <img
          className="sk-mark sk-mark--img"
          data-size={size}
          src={gmgnSymbolUrl}
          alt={tw('skills.altGmgnLogo')}
          draggable={false}
        />
        {emoji ? (
          <span className="sk-mark__emoji" aria-hidden>
            {emoji}
          </span>
        ) : null}
      </span>
    )
  }
  if (skillGroupKey(skill) === 'crypto') {
    return (
      <img
        className="sk-mark sk-mark--img"
        data-size={size}
        src={agentosMarkUrl}
        alt={tw('skills.altAgentosLogo')}
        draggable={false}
      />
    )
  }
  return (
    <span className="sk-mark sk-mark--glyph" data-size={size} aria-hidden>
      <Package strokeWidth={1.5} />
    </span>
  )
}

/**
 * The mark for a catalog row: its own logo when the catalog gave an http(s)
 * one, else the partner mark for a partner source, else initials.
 */
export function RegistryMark({ item, size = 'sm' }: { item: RegistryItem; size?: MarkSize }) {
  const logoUrl = safeUrl(item.logo)
  const [broken, setBroken] = useState(false)
  if (!logoUrl || broken) {
    const source = String(item.source || '').toLowerCase()
    if (isPartnerBrand(source)) return <BrandMark brand={source} size={size} />
    return (
      <span className="sk-mark sk-mark--initials" data-size={size} aria-hidden>
        {initials(item.provider || item.name)}
      </span>
    )
  }
  return (
    <img
      className="sk-mark sk-mark--img"
      data-size={size}
      src={logoUrl}
      alt=""
      loading="lazy"
      referrerPolicy="no-referrer"
      draggable={false}
      onError={() => setBroken(true)}
    />
  )
}
