import type { PaletteId, ResolvedTheme } from '@shared/theme'

/**
 * Colour tokens. Every key becomes a `--<key>` custom property on <html>
 * (see apply.ts) and is mapped into Tailwind by tokens.css `@theme inline`.
 *
 * Adding a palette = add an entry to PALETTES. Adding a token = add it to
 * ColorTokens and every palette (the type forces completeness).
 */
export interface ColorTokens {
  background: string
  foreground: string
  surface: string
  elevated: string
  card: string
  'card-foreground': string
  popover: string
  'popover-foreground': string
  primary: string
  'primary-foreground': string
  secondary: string
  'secondary-foreground': string
  muted: string
  'muted-foreground': string
  dim: string
  accent: string
  'accent-foreground': string
  destructive: string
  'destructive-foreground': string
  border: string
  hairline: string
  input: string
  ring: string
  ok: string
  warn: string
  danger: string
  info: string
  sidebar: string
  'sidebar-foreground': string
  'sidebar-primary': string
  'sidebar-primary-foreground': string
  'sidebar-accent': string
  'sidebar-accent-foreground': string
  'sidebar-border': string
  'sidebar-ring': string
  /** Opacity of the atmospheric grain overlay, "0" disables it. */
  'grain-opacity': string
}

export interface PaletteDefinition {
  id: PaletteId
  label: string
  description: string
  light: ColorTokens
  dark: ColorTokens
}

export const COLOR_TOKEN_KEYS = [
  'background',
  'foreground',
  'surface',
  'elevated',
  'card',
  'card-foreground',
  'popover',
  'popover-foreground',
  'primary',
  'primary-foreground',
  'secondary',
  'secondary-foreground',
  'muted',
  'muted-foreground',
  'dim',
  'accent',
  'accent-foreground',
  'destructive',
  'destructive-foreground',
  'border',
  'hairline',
  'input',
  'ring',
  'ok',
  'warn',
  'danger',
  'info',
  'sidebar',
  'sidebar-foreground',
  'sidebar-primary',
  'sidebar-primary-foreground',
  'sidebar-accent',
  'sidebar-accent-foreground',
  'sidebar-border',
  'sidebar-ring',
  'grain-opacity',
] as const satisfies readonly (keyof ColorTokens)[]

/* Tactical — the AgentOS brand system. Near-black operator ground, hairline
   panels, one lime signal (#CCFF00, sampled from the molecule logo) reserved
   for active nav, primary CTA, focus rings. Never decorate with it. */
const tactical: PaletteDefinition = {
  id: 'tactical',
  label: 'Tactical',
  description: 'AgentOS brand: operator black with a single lime signal.',
  dark: {
    background: '#060608',
    foreground: '#ececef',
    surface: '#0e0e13',
    elevated: '#17171d',
    card: '#0e0e13',
    'card-foreground': '#ececef',
    popover: '#17171d',
    'popover-foreground': '#ececef',
    primary: '#ccff00',
    'primary-foreground': '#0f1400',
    secondary: '#17171d',
    'secondary-foreground': '#ececef',
    muted: '#17171d',
    'muted-foreground': '#a1a1aa',
    dim: '#7c7c85',
    accent: '#21212a',
    'accent-foreground': '#ececef',
    destructive: '#f87171',
    'destructive-foreground': '#1a0505',
    border: 'rgba(255, 255, 255, 0.13)',
    hairline: 'rgba(255, 255, 255, 0.08)',
    input: 'rgba(255, 255, 255, 0.17)',
    ring: '#ccff00',
    ok: '#4ade80',
    warn: '#fbbf24',
    danger: '#f87171',
    info: '#60a5fa',
    sidebar: '#0e0e13',
    'sidebar-foreground': '#ececef',
    'sidebar-primary': '#ccff00',
    'sidebar-primary-foreground': '#0f1400',
    'sidebar-accent': '#21212a',
    'sidebar-accent-foreground': '#ececef',
    'sidebar-border': 'rgba(255, 255, 255, 0.13)',
    'sidebar-ring': '#ccff00',
    'grain-opacity': '0.05',
  },
  light: {
    background: '#f4f5ee',
    foreground: '#18181b',
    surface: '#ffffff',
    elevated: '#eff1e8',
    card: '#ffffff',
    'card-foreground': '#18181b',
    popover: '#ffffff',
    'popover-foreground': '#18181b',
    primary: '#556f00',
    'primary-foreground': '#ffffff',
    secondary: '#eff1e8',
    'secondary-foreground': '#18181b',
    muted: '#eff1e8',
    'muted-foreground': '#4b4b53',
    dim: '#66666e',
    accent: '#e4e7da',
    'accent-foreground': '#18181b',
    destructive: '#a61b1b',
    'destructive-foreground': '#ffffff',
    border: 'rgba(18, 20, 15, 0.1)',
    hairline: 'rgba(18, 20, 15, 0.08)',
    input: 'rgba(18, 20, 15, 0.14)',
    ring: '#556f00',
    ok: '#166534',
    warn: '#7a4f00',
    danger: '#a61b1b',
    info: '#1d4ed8',
    sidebar: '#ffffff',
    'sidebar-foreground': '#18181b',
    'sidebar-primary': '#556f00',
    'sidebar-primary-foreground': '#ffffff',
    'sidebar-accent': '#e4e7da',
    'sidebar-accent-foreground': '#18181b',
    'sidebar-border': 'rgba(18, 20, 15, 0.1)',
    'sidebar-ring': '#556f00',
    'grain-opacity': '0.03',
  },
}

/* Graphite — neutral blue-grey with a cool signal. Closer to stock macOS
   chrome for people who find the lime too loud. No grain. */
const graphite: PaletteDefinition = {
  id: 'graphite',
  label: 'Graphite',
  description: 'Neutral blue-grey, calmer accent, no texture.',
  dark: {
    background: '#090c11',
    foreground: '#f2f4f7',
    surface: '#10141a',
    elevated: '#171c24',
    card: '#10141a',
    'card-foreground': '#f2f4f7',
    popover: '#171c24',
    'popover-foreground': '#f2f4f7',
    primary: '#7dd3fc',
    'primary-foreground': '#04121b',
    secondary: '#191f28',
    'secondary-foreground': '#f2f4f7',
    muted: '#171c24',
    'muted-foreground': '#aeb6c3',
    dim: '#8f99a8',
    accent: '#1b222c',
    'accent-foreground': '#f2f4f7',
    destructive: '#f87171',
    'destructive-foreground': '#1a0505',
    border: 'rgba(226, 232, 240, 0.13)',
    hairline: 'rgba(226, 232, 240, 0.08)',
    input: 'rgba(226, 232, 240, 0.17)',
    ring: '#7dd3fc',
    ok: '#4ade80',
    warn: '#fbbf24',
    danger: '#f87171',
    info: '#60a5fa',
    sidebar: '#0c1016',
    'sidebar-foreground': '#f2f4f7',
    'sidebar-primary': '#7dd3fc',
    'sidebar-primary-foreground': '#04121b',
    'sidebar-accent': '#191f28',
    'sidebar-accent-foreground': '#f2f4f7',
    'sidebar-border': 'rgba(226, 232, 240, 0.1)',
    'sidebar-ring': '#7dd3fc',
    'grain-opacity': '0',
  },
  light: {
    background: '#f3f5f7',
    foreground: '#171b22',
    surface: '#ffffff',
    elevated: '#eef1f4',
    card: '#ffffff',
    'card-foreground': '#171b22',
    popover: '#ffffff',
    'popover-foreground': '#171b22',
    primary: '#0369a1',
    'primary-foreground': '#ffffff',
    secondary: '#edf0f3',
    'secondary-foreground': '#171b22',
    muted: '#edf0f3',
    'muted-foreground': '#505967',
    dim: '#677180',
    accent: '#e8edf0',
    'accent-foreground': '#171b22',
    destructive: '#a61b1b',
    'destructive-foreground': '#ffffff',
    border: 'rgba(27, 34, 45, 0.13)',
    hairline: 'rgba(27, 34, 45, 0.08)',
    input: 'rgba(27, 34, 45, 0.18)',
    ring: '#0369a1',
    ok: '#166534',
    warn: '#7a4f00',
    danger: '#a61b1b',
    info: '#1d4ed8',
    sidebar: '#fbfcfd',
    'sidebar-foreground': '#171b22',
    'sidebar-primary': '#0369a1',
    'sidebar-primary-foreground': '#ffffff',
    'sidebar-accent': '#edf1f3',
    'sidebar-accent-foreground': '#171b22',
    'sidebar-border': 'rgba(27, 34, 45, 0.11)',
    'sidebar-ring': '#0369a1',
    'grain-opacity': '0',
  },
}

/* ── Derived palettes ──────────────────────────────────────────────────────
   The two above are hand-tuned. The rest are built from a few seeds per mode
   (ground, ink, signal) with the same proportions Tactical uses, so every
   palette shares one structure and only the mood changes. */

interface Seed {
  bg: string
  fg: string
  primary: string
  primaryFg: string
  /** Sidebar ground; defaults to the surface tone. */
  sidebar?: string
  grain?: string
}

function rgb(hex: string): [number, number, number] {
  const h = hex.replace('#', '')
  const n = parseInt(h.length === 3 ? h.replace(/./g, (c) => c + c) : h, 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

/** `t` of the way from `a` to `b`, as hex. */
function mix(a: string, b: string, t: number): string {
  const [r1, g1, b1] = rgb(a)
  const [r2, g2, b2] = rgb(b)
  const c = (x: number, y: number) => Math.round(x + (y - x) * t)
  return `#${[c(r1, r2), c(g1, g2), c(b1, b2)].map((v) => v.toString(16).padStart(2, '0')).join('')}`
}

function alpha(hex: string, a: number): string {
  const [r, g, b] = rgb(hex)
  return `rgba(${r}, ${g}, ${b}, ${a})`
}

const SIGNALS = {
  dark: { ok: '#4ade80', warn: '#fbbf24', danger: '#f87171', info: '#60a5fa' },
  light: { ok: '#166534', warn: '#7a4f00', danger: '#a61b1b', info: '#1d4ed8' },
} as const

function derive(seed: Seed, mode: ResolvedTheme): ColorTokens {
  const { bg, fg, primary, primaryFg } = seed
  const surface = mix(bg, fg, 0.04)
  const elevated = mix(bg, fg, 0.08)
  const accent = mix(bg, fg, 0.12)
  const sidebar = seed.sidebar ?? surface
  const signals = SIGNALS[mode]
  const destructive = mode === 'dark' ? '#f87171' : '#a61b1b'
  return {
    background: bg,
    foreground: fg,
    surface,
    elevated,
    card: surface,
    'card-foreground': fg,
    popover: elevated,
    'popover-foreground': fg,
    primary,
    'primary-foreground': primaryFg,
    secondary: elevated,
    'secondary-foreground': fg,
    muted: elevated,
    'muted-foreground': mix(fg, bg, 0.32),
    dim: mix(fg, bg, 0.5),
    accent,
    'accent-foreground': fg,
    destructive,
    'destructive-foreground': mode === 'dark' ? '#1a0505' : '#ffffff',
    border: alpha(fg, 0.13),
    hairline: alpha(fg, 0.08),
    input: alpha(fg, 0.17),
    ring: primary,
    ...signals,
    sidebar,
    'sidebar-foreground': fg,
    'sidebar-primary': primary,
    'sidebar-primary-foreground': primaryFg,
    'sidebar-accent': accent,
    'sidebar-accent-foreground': fg,
    'sidebar-border': alpha(fg, 0.11),
    'sidebar-ring': primary,
    'grain-opacity': seed.grain ?? '0',
  }
}

function palette(
  id: PaletteId,
  label: string,
  description: string,
  dark: Seed,
  light: Seed,
): PaletteDefinition {
  return { id, label, description, dark: derive(dark, 'dark'), light: derive(light, 'light') }
}

const everforest = palette(
  'everforest',
  'Everforest',
  'Warm, low-contrast forest greens.',
  { bg: '#232a2e', fg: '#d3c6aa', primary: '#a7c080', primaryFg: '#1e2326', grain: '0.04' },
  { bg: '#fdf6e3', fg: '#5c6a72', primary: '#8da101', primaryFg: '#ffffff', grain: '0.03' },
)

const solarized = palette(
  'solarized',
  'Solarized',
  'Fixed-contrast light and dark, the classic pair.',
  { bg: '#002b36', fg: '#a7b7b8', primary: '#2aa198', primaryFg: '#002b36' },
  { bg: '#fdf6e3', fg: '#586e75', primary: '#268bd2', primaryFg: '#ffffff' },
)

const nord = palette(
  'nord',
  'Nord',
  'Arctic blue-greys with a frost accent.',
  { bg: '#242933', fg: '#e5e9f0', primary: '#88c0d0', primaryFg: '#2e3440', sidebar: '#2b303b' },
  { bg: '#eceff4', fg: '#2e3440', primary: '#5e81ac', primaryFg: '#ffffff', sidebar: '#f4f6fa' },
)

const midnight = palette(
  'midnight',
  'Midnight',
  'Deep blue-violet with cool accents.',
  { bg: '#0a0b1c', fg: '#dcdcf2', primary: '#a5a0ff', primaryFg: '#0a0b1c', grain: '0.04' },
  { bg: '#f3f2fb', fg: '#1d1c3a', primary: '#5145d6', primaryFg: '#ffffff' },
)

const slate = palette(
  'slate',
  'Slate',
  'Cool slate blue, a focused developer theme.',
  { bg: '#0b1220', fg: '#e2e8f0', primary: '#38bdf8', primaryFg: '#061a2b', sidebar: '#0f172a' },
  { bg: '#f1f5f9', fg: '#0f172a', primary: '#0f4c81', primaryFg: '#ffffff', sidebar: '#f8fafc' },
)

const ember = palette(
  'ember',
  'Ember',
  'Warm crimson and bronze, forge light.',
  { bg: '#140a0a', fg: '#f2e4d8', primary: '#f4a261', primaryFg: '#2a1206', grain: '0.05' },
  { bg: '#fbf3ec', fg: '#2f1a12', primary: '#b4461d', primaryFg: '#ffffff', grain: '0.03' },
)

const mono = palette(
  'mono',
  'Mono',
  'Clean grayscale, minimal and focused.',
  { bg: '#0a0a0a', fg: '#e8e8e8', primary: '#ffffff', primaryFg: '#0a0a0a' },
  { bg: '#f5f5f5', fg: '#111111', primary: '#111111', primaryFg: '#ffffff' },
)

const cyberpunk = palette(
  'cyberpunk',
  'Cyberpunk',
  'Neon green on black, matrix terminal.',
  { bg: '#000000', fg: '#c8ffd4', primary: '#00ff66', primaryFg: '#001a08', grain: '0.06' },
  { bg: '#f0fff4', fg: '#052e16', primary: '#15803d', primaryFg: '#ffffff' },
)

export const PALETTES: Record<PaletteId, PaletteDefinition> = {
  tactical,
  graphite,
  everforest,
  solarized,
  nord,
  midnight,
  slate,
  ember,
  mono,
  cyberpunk,
}

export function paletteTokens(palette: PaletteId, mode: ResolvedTheme): ColorTokens {
  return PALETTES[palette][mode]
}
