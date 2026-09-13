import anthropic from '~/assets/providers/anthropic.svg?raw'
import dashscope from '~/assets/providers/dashscope.svg?raw'
import deepseek from '~/assets/providers/deepseek.svg?raw'
import gemini from '~/assets/providers/gemini.svg?raw'
import moonshot from '~/assets/providers/moonshot.svg?raw'
import ollama from '~/assets/providers/ollama.svg?raw'
import openai from '~/assets/providers/openai.svg?raw'
import openrouter from '~/assets/providers/openrouter.svg?raw'
import qianfan from '~/assets/providers/qianfan.svg?raw'
import volcengine from '~/assets/providers/volcengine.svg?raw'
import zhipu from '~/assets/providers/zhipu.svg?raw'
import opencap from '~/assets/providers/opencap.jpg'
import surplus from '~/assets/providers/surplus.png'
import { Plug } from 'lucide-react'
import { cn } from '~/lib/utils'
import { CUSTOM_PROVIDER_ID } from './logic'

/**
 * Brand marks by provider id (Lobe Icons, see assets/providers/LICENSE.txt),
 * inlined so the monochrome ones take `currentColor`. The window's CSP
 * allows no remote images, so anything not bundled falls back to a
 * monogram on a hue derived from the id.
 */
const MARKS: Record<string, string> = {
  anthropic,
  dashscope,
  deepseek,
  gemini,
  moonshot,
  ollama,
  openai,
  openrouter,
  qianfan,
  volcengine,
  zhipu,
}

/** Raster marks the vendor publishes only as bitmaps. `bleed`: fills the plate edge to edge. */
const RASTER: Record<string, { src: string; bleed?: boolean }> = {
  opencap: { src: opencap, bleed: true },
  surplus: { src: surplus },
}

/** Monogram tint: a stable hue per id so unknown providers still look distinct. */
export function providerHue(id: string): number {
  let h = 0
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) % 360
  return h
}

export function providerMonogram(label: string): string {
  const words = label
    .trim()
    .split(/[\s-]+/)
    .filter(Boolean)
  if (words.length >= 2) return (words[0]![0]! + words[1]![0]!).toUpperCase()
  // One word: its capitals when it has two ("OpenCAP" → OC), else the first two letters.
  const caps = label.replace(/[^A-Z]/g, '')
  return caps.length >= 2 ? caps.slice(0, 2) : label.slice(0, 2).toUpperCase()
}

export function hasProviderMark(id: string): boolean {
  return id in MARKS || id in RASTER || id === CUSTOM_PROVIDER_ID
}

export function ProviderLogo({
  id,
  label,
  size = 28,
  className,
}: {
  id: string
  label: string
  size?: number
  className?: string
}) {
  if (id === CUSTOM_PROVIDER_ID) {
    return (
      <span
        className={cn('prov-logo', className)}
        style={{ width: size, height: size }}
        aria-hidden
      >
        <Plug className="prov-logo__glyph" strokeWidth={1.75} />
      </span>
    )
  }
  const raster = RASTER[id]
  if (raster) {
    return (
      <span
        className={cn('prov-logo', raster.bleed && 'prov-logo--bleed', className)}
        style={{ width: size, height: size }}
        aria-hidden
      >
        <img src={raster.src} alt="" draggable={false} />
      </span>
    )
  }
  const mark = MARKS[id]
  if (mark) {
    return (
      <span
        className={cn('prov-logo', className)}
        style={{ width: size, height: size }}
        aria-hidden
        // Static, bundled SVG from assets/providers: not user content.
        dangerouslySetInnerHTML={{ __html: mark }}
      />
    )
  }
  return (
    <span
      className={cn('prov-logo prov-logo--mono', className)}
      style={{ width: size, height: size, ['--hue' as string]: providerHue(id) }}
      aria-hidden
    >
      {providerMonogram(label)}
    </span>
  )
}
