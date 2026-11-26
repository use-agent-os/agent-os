import { useEffect, useState, useSyncExternalStore, type RefObject } from 'react'
import { useRpc } from '@/app/providers'

/**
 * The RPC methods the connected gateway advertised in its handshake, or null
 * when it did not list them (an older gateway, a test double). Re-read on
 * every handshake and connection change.
 */
export function useGatewayMethods(): readonly string[] | null {
  const rpc = useRpc()
  return useSyncExternalStore(
    (onChange) => {
      const offHello = rpc.on('_hello', onChange)
      const offState = rpc.on('_state', onChange)
      return () => {
        offHello()
        offState()
      }
    },
    () => (rpc as { methods?: readonly string[] | null }).methods ?? null,
    () => null,
  )
}

export interface ElementSize {
  width: number
  height: number
}

/** An element's content-box size, live; zero where it cannot be measured. */
export function useElementSize(ref: RefObject<HTMLElement | null>): ElementSize {
  const [size, setSize] = useState<ElementSize>({ width: 0, height: 0 })
  useEffect(() => {
    const node = ref.current
    if (!node || typeof ResizeObserver === 'undefined') return
    const measure = () => {
      const rect = node.getBoundingClientRect()
      setSize((prev) =>
        Math.round(prev.width) === Math.round(rect.width) &&
        Math.round(prev.height) === Math.round(rect.height)
          ? prev
          : { width: rect.width, height: rect.height },
      )
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [ref])
  return size
}

/** Whether the page is in front: a hidden page stops polling. */
export function usePageVisible(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      document.addEventListener('visibilitychange', onChange)
      return () => document.removeEventListener('visibilitychange', onChange)
    },
    () => document.visibilityState !== 'hidden',
    () => true,
  )
}
