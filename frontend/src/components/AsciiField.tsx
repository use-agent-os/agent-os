import { useEffect, useRef } from 'react'

// Atmospheric ASCII ember field (decorative). Canvas-driven so the animation
// never touches React state: glyphs drift slowly upward and flicker, dense at
// the base and dissolving toward the top. Honors prefers-reduced-motion by
// rendering a single static frame. Color follows the token system (signal).
const GLYPHS = ['^', '*', '+', 'x', '·', '"', "'"] as const

type Particle = {
  x: number
  y0: number
  vy: number // px/s upward
  char: string
  phase: number
  flickerHz: number
}

function spawnParticles(width: number, height: number): Particle[] {
  const count = Math.min(900, Math.max(160, Math.floor(width / 2.2)))
  const particles: Particle[] = []
  for (let i = 0; i < count; i++) {
    // Bias spawn depth toward the bottom (ember bed).
    const depth = Math.pow(Math.random(), 0.55)
    particles.push({
      x: Math.random() * width,
      y0: height * depth,
      vy: 4 + Math.random() * 10,
      char: GLYPHS[Math.floor(Math.random() * GLYPHS.length)]!,
      phase: Math.random() * Math.PI * 2,
      flickerHz: 0.2 + Math.random() * 0.5,
    })
  }
  return particles
}

export function AsciiField() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    const ctx = canvas?.getContext?.('2d')
    if (!canvas || !ctx) return // jsdom / non-canvas environments: static nothing

    let raf = 0
    let particles: Particle[] = []
    let width = 0
    let height = 0
    let color = '#ccff00'
    let colorRefreshAt = 0

    const reduceMotion = (() => {
      try {
        return window.matchMedia('(prefers-reduced-motion: reduce)').matches
      } catch {
        return false
      }
    })()

    const resize = () => {
      const parent = canvas.parentElement
      if (!parent) return
      const rect = parent.getBoundingClientRect()
      const dpr = window.devicePixelRatio || 1
      width = rect.width
      height = rect.height
      canvas.width = Math.max(1, Math.floor(width * dpr))
      canvas.height = Math.max(1, Math.floor(height * dpr))
      canvas.style.width = `${width}px`
      canvas.style.height = `${height}px`
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      particles = spawnParticles(width, height)
    }

    const draw = (tMs: number) => {
      const t = tMs / 1000
      // Token color can change with the theme; re-read it about once a second.
      if (tMs > colorRefreshAt) {
        color = getComputedStyle(canvas).color || color
        colorRefreshAt = tMs + 1000
      }
      ctx.clearRect(0, 0, width, height)
      ctx.font = '11px "JetBrains Mono Variable", ui-monospace, monospace'
      ctx.fillStyle = color
      for (const p of particles) {
        const y = (((p.y0 - t * p.vy) % height) + height) % height
        const depth = y / height // 0 top .. 1 bottom
        const flicker = 0.65 + 0.35 * Math.sin(t * p.flickerHz * Math.PI * 2 + p.phase)
        // Fade toward the top so embers dissolve as they rise.
        const alpha = Math.min(1, depth * 0.85 + 0.3) * flicker
        if (alpha < 0.03) continue
        ctx.globalAlpha = alpha
        ctx.fillText(p.char, p.x, y)
      }
      ctx.globalAlpha = 1
      if (!reduceMotion) raf = requestAnimationFrame(draw)
    }

    resize()
    const ro = new ResizeObserver(resize)
    if (canvas.parentElement) ro.observe(canvas.parentElement)
    raf = requestAnimationFrame(draw)

    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
    }
  }, [])

  return (
    <div className="ascii-field" aria-hidden="true">
      <canvas ref={canvasRef} />
    </div>
  )
}
