import { sha256 } from '@noble/hashes/sha2.js'
import { bytesToHex, utf8ToBytes } from '@noble/hashes/utils.js'
import { desktopApi } from '~/lib/desktop-api'

/** How long a copied secret may sit in the clipboard before it is blanked. */
export const CLIPBOARD_CLEAR_MS = 60_000

/** SHA-256 of the text, hex: how main names a secret without holding it. */
export function secretDigest(text: string): string {
  return bytesToHex(sha256(utf8ToBytes(text)))
}

/**
 * Put a secret (an exported private key) on the clipboard, and have it
 * blanked `CLIPBOARD_CLEAR_MS` later if the clipboard still holds exactly
 * it. The clear is owned by main (`app.scrubClipboard`), not by the view
 * that copied: closing the sheet, navigating away or reloading the window
 * cannot cancel it, and quitting the app scrubs at once. Rejects when the
 * clipboard refused the write; scheduling the clear is best effort.
 */
export async function copySecret(secret: string): Promise<void> {
  await navigator.clipboard.writeText(secret)
  try {
    await desktopApi().app.scrubClipboard(secretDigest(secret), CLIPBOARD_CLEAR_MS)
  } catch {
    /* no bridge: nothing more this view can do */
  }
}
