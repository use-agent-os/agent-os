import { useEffect, useRef } from 'react'

// Atmospheric telemetry field (decorative). The reference motion reads like a
// living ASCII landscape; AgentOS translates that into slow signal drift rather
// than literal fire. Canvas work never touches React state, pauses off-screen,
// and becomes a single static frame for reduced-motion users.
const GLYPHS = ['·', '·', ':', '+', '+', '×', '^', '^', '│', '╎'] as const

type Particle = {
  x: number
  y0: number
  vy: number // px/s upward
  char: string
  phase: number
  flickerHz: number
  drift: number
}

function spawnParticles(width: number, height: number): Particle[] {
  const count = Math.min(900, Math.max(260, Math.floor(width / 1.65)))
  const particles: Particle[] = []
  for (let i = 0; i < count; i++) {
    // Keep the signal denser near the lower edge, like a terminal skyline.
    const depth = Math.pow(Math.random(), 0.48)
    particles.push({
      x: Math.random() * width,
      y0: height * depth,
      vy: 8 + Math.random() * 10,
      char: GLYPHS[Math.floor(Math.random() * GLYPHS.length)]!,
      phase: Math.random() * Math.PI * 2,
      flickerHz: 0.3 + Math.random() * 0.45,
      drift: 3 + Math.random() * 6,
    })
  }
  return particles
}

export function AsciiField() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    // jsdom has neither a real canvas nor ResizeObserver. Exit before asking
    // for a context so shell tests stay quiet and deterministic.
    if (!canvas || typeof ResizeObserver !== 'function') return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let raf = 0
    let particles: Particle[] = []
    let width = 0
    let height = 0
    let color = '#ccff00'
    let colorRefreshAt = 0
    let isVisible = document.visibilityState !== 'hidden'
    let isInViewport = true

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

    const shouldAnimate = () => !reduceMotion && isVisible && isInViewport

    const requestFrame = () => {
      if (raf || !shouldAnimate()) return
      raf = requestAnimationFrame(draw)
    }

    const draw = (tMs: number) => {
      raf = 0
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
        const x = p.x + Math.sin(t * 0.48 + p.phase) * p.drift
        const depth = y / height // 0 top .. 1 bottom
        const flicker = 0.64 + 0.36 * Math.sin(t * p.flickerHz * Math.PI * 2 + p.phase)
        // Fade toward the top so signal noise resolves into a dense baseline.
        const alpha = Math.min(1, depth * 0.9 + 0.16) * flicker
        if (alpha < 0.03) continue
        ctx.globalAlpha = alpha
        ctx.fillText(p.char, x, y)
      }
      ctx.globalAlpha = 1
      requestFrame()
    }

    resize()
    const ro = new ResizeObserver(resize)
    if (canvas.parentElement) ro.observe(canvas.parentElement)

    const io =
      typeof IntersectionObserver === 'function'
        ? new IntersectionObserver(([entry]) => {
            isInViewport = entry?.isIntersecting ?? true
            if (!isInViewport && raf) {
              cancelAnimationFrame(raf)
              raf = 0
            }
            requestFrame()
          })
        : null
    io?.observe(canvas)

    const handleVisibility = () => {
      isVisible = document.visibilityState !== 'hidden'
      if (!isVisible && raf) {
        cancelAnimationFrame(raf)
        raf = 0
      }
      requestFrame()
    }
    document.addEventListener('visibilitychange', handleVisibility)

    // The first frame is always painted; reduced-motion stops here.
    raf = requestAnimationFrame(draw)

    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      io?.disconnect()
      document.removeEventListener('visibilitychange', handleVisibility)
    }
  }, [])

  return (
    <div className="ascii-field" data-testid="control-header-signal" aria-hidden="true">
      <canvas ref={canvasRef} />
    </div>
  )
}
