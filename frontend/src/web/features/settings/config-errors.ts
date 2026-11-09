// Reading the gateway's config write errors (config_commit.py / pydantic).

export function isRevisionConflict(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? '')
  return /revision mismatch/i.test(message)
}

export function isWriteBlocked(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? '')
  return /diverged from the running gateway/i.test(message)
}

/**
 * Field errors from a pydantic validation message:
 *
 *     1 validation error for GatewayConfig
 *     agentos_router.enabled
 *       Input should be a valid boolean [type=bool_type, …]
 *
 * Returns path -> message for every located error.
 */
export function fieldErrors(error: unknown): Record<string, string> {
  const message = error instanceof Error ? error.message : String(error ?? '')
  const out: Record<string, string> = {}
  const lines = message.split('\n')
  for (let i = 0; i < lines.length - 1; i++) {
    const path = lines[i]!.trim()
    const next = lines[i + 1]!
    if (!path || /validation error/i.test(path) || /^\s/.test(lines[i]!)) continue
    if (!/^\s+\S/.test(next)) continue
    if (!/^[\w.[\]-]+$/.test(path)) continue
    out[path] = next.trim().replace(/\s*\[type=.*$/, '')
  }
  return out
}
