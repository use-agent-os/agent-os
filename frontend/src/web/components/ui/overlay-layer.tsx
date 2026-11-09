import { useOverlayLayer } from '@/components/overlay-layer'

/**
 * Render inside an overlay's content (mounted only while it is open) so the
 * kept shortcut provider sees an overlay and keeps single-key shortcuts quiet.
 */
export function OverlayLayer() {
  useOverlayLayer()
  return null
}
