import './pet.css'
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useRpc } from '@/app/providers'
import {
  PET_FRAME_H,
  PET_FRAME_W,
  PET_LOOP_MS,
  petSheetUrl,
  petStateRow,
  rowFrameCounts,
  sheetGeometry,
  type SheetGeometry,
} from '@shared/pet'
import { useSettings } from '~/stores/settings'
import { bindPetSignals, usePet } from '~/stores/pet'
import {
  anchorFromPixels,
  defaultAnchor,
  parseStoredAnchor,
  pixelsFromAnchor,
  type PetAnchor,
  type Size,
} from './logic'

const POS_KEY = 'agentos-desktop.petPosition'

interface Sheet extends SheetGeometry {
  /** Real frames per row (see rowFrameCounts). */
  frames: number[]
  /** The pixels could not be read; every row runs the full stride. */
  tainted?: boolean
}

function loadAnchor(size: Size, win: Size): PetAnchor | null {
  try {
    return parseStoredAnchor(JSON.parse(localStorage.getItem(POS_KEY) || 'null'), size, win)
  } catch {
    return null
  }
}

function saveAnchor(anchor: PetAnchor): void {
  try {
    localStorage.setItem(POS_KEY, JSON.stringify(anchor))
  } catch {
    /* storage unavailable */
  }
}

/** The window's inner size, re-read on every resize. */
function subscribeWindow(onChange: () => void): () => void {
  // The last resize event of a burst can fire before the metrics settle;
  // read once more on the next frame so the pet ends on the final size.
  let frame = 0
  const onResize = () => {
    onChange()
    cancelAnimationFrame(frame)
    frame = requestAnimationFrame(onChange)
  }
  window.addEventListener('resize', onResize)
  return () => {
    window.removeEventListener('resize', onResize)
    cancelAnimationFrame(frame)
  }
}
function readWindow(): string {
  return `${window.innerWidth}x${window.innerHeight}`
}
function useWindowSize(): Size {
  const key = useSyncExternalStore(subscribeWindow, readWindow)
  const [w, h] = key.split('x').map(Number)
  return { w: w ?? 0, h: h ?? 0 }
}

/** Alpha threshold at or below which a frame counts as transparent padding. */
const BLANK_ALPHA = 8
/** Sample every Nth pixel: enough to catch any sprite, cheap on an 11-row sheet. */
const SAMPLE_STRIDE = 4

/**
 * Decode the sheet once and measure which frames of each row are real.
 * Runs off the loaded <img> on a scratch canvas; a decode failure leaves
 * every row at the full stride, which is what the petdex web app does.
 */
function measureSheet(img: HTMLImageElement): Sheet | null {
  const geometry = sheetGeometry(img.naturalWidth, img.naturalHeight)
  if (!geometry) return null
  const full = Array.from({ length: geometry.rows }, () => 6)
  let tainted = false
  try {
    const canvas = document.createElement('canvas')
    canvas.width = img.naturalWidth
    canvas.height = img.naturalHeight
    const ctx = canvas.getContext('2d', { willReadFrequently: true })
    if (!ctx) return { ...geometry, frames: full }
    ctx.drawImage(img, 0, 0)
    const isBlank = (col: number, row: number) => {
      const data = ctx.getImageData(col * PET_FRAME_W, row * PET_FRAME_H, PET_FRAME_W, PET_FRAME_H)
      for (let y = 0; y < PET_FRAME_H; y += SAMPLE_STRIDE) {
        for (let x = 0; x < PET_FRAME_W; x += SAMPLE_STRIDE) {
          if (data.data[(y * PET_FRAME_W + x) * 4 + 3]! > BLANK_ALPHA) return false
        }
      }
      return true
    }
    return { ...geometry, frames: rowFrameCounts(geometry, isBlank) }
  } catch (err) {
    tainted = true
    console.warn('[pet] could not measure sheet frames', err)
    return { ...geometry, frames: full, tainted }
  }
}

/**
 * The petdex mascot floating over the window: one sheet, one row per state,
 * the row's real frames stepped by CSS the way the petdex web app animates.
 * Drag it anywhere (the spot is remembered); click it and it waves back.
 */
export function PetOverlay() {
  const rpc = useRpc()
  const pet = useSettings((s) => s.settings.pet)
  useEffect(() => bindPetSignals(rpc), [rpc])
  if (!pet.enabled || !pet.slug) return null
  return <PetSprite slug={pet.slug} scale={pet.scale} />
}

function PetSprite({ slug, scale }: { slug: string; scale: number }) {
  const state = usePet((s) => s.state)
  const poke = usePet((s) => s.poke)
  const [sheet, setSheet] = useState<Sheet | null>(null)
  const win = useWindowSize()
  // Whole pixels: a fractional frame width puts every step on a sub-pixel
  // boundary and the sprite shimmers.
  const w = Math.max(1, Math.round(PET_FRAME_W * scale))
  const h = Math.max(1, Math.round(PET_FRAME_H * scale))
  const size: Size = { w, h }
  // The spot is kept as a share of the free space, so a resized window
  // carries the pet along and can never strand it off screen.
  const [anchor, setAnchor] = useState<PetAnchor>(
    () => loadAnchor(size, win) ?? defaultAnchor(size, win),
  )
  const drag = useRef<{ dx: number; dy: number; moved: boolean } | null>(null)
  const url = petSheetUrl(slug)

  // Measure the sheet once per pet: the grid says which rows exist, the
  // alpha says how many frames each row really has.
  useEffect(() => {
    let cancelled = false
    const img = new Image()
    // Needed to read pixels back in measureSheet; the pet scheme allows it.
    img.crossOrigin = 'anonymous'
    img.onload = () => {
      if (!cancelled) setSheet(measureSheet(img))
    }
    img.onerror = () => {
      if (!cancelled) setSheet(null)
    }
    img.src = url
    return () => {
      cancelled = true
    }
  }, [url])

  if (!sheet) return null
  const pos = pixelsFromAnchor(anchor, size, win)
  const row = petStateRow(state, sheet.rows)
  const frames = sheet.frames[row] ?? 1
  const style: React.CSSProperties = {
    width: w,
    height: h,
    backgroundImage: `url("${url}")`,
    backgroundSize: `${sheet.cols * w}px ${sheet.rows * h}px`,
    backgroundPositionY: `${-row * h}px`,
    ['--pet-frame-w' as string]: `${w}px`,
    ['--pet-frames' as string]: String(frames),
    ['--pet-loop' as string]: `${Math.round((PET_LOOP_MS * frames) / 6)}ms`,
    left: pos.x,
    top: pos.y,
  }

  function onPointerDown(e: React.PointerEvent<HTMLButtonElement>) {
    const rect = e.currentTarget.getBoundingClientRect()
    drag.current = { dx: e.clientX - rect.left, dy: e.clientY - rect.top, moved: false }
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  function onPointerMove(e: React.PointerEvent<HTMLButtonElement>) {
    if (!drag.current) return
    drag.current.moved = true
    setAnchor(
      anchorFromPixels(
        { x: e.clientX - drag.current.dx, y: e.clientY - drag.current.dy },
        size,
        win,
      ),
    )
  }
  function onPointerUp(e: React.PointerEvent<HTMLButtonElement>) {
    const d = drag.current
    drag.current = null
    e.currentTarget.releasePointerCapture(e.pointerId)
    if (d?.moved) saveAnchor(anchor)
    else if (d) poke()
  }

  return (
    <button
      // A new row restarts the walk from its first frame, as Hermes does.
      key={row}
      type="button"
      className="pet app-no-drag"
      data-state={state}
      data-frames={frames}
      data-measured={sheet.tainted ? 'false' : 'true'}
      aria-label={`Pet: ${state}`}
      title={slug}
      style={style}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={() => {
        drag.current = null
      }}
    />
  )
}
