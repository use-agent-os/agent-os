import { accessSync, constants } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'

/**
 * Find the installed `agentos` CLI. GUI apps on macOS launch with a minimal
 * PATH, so the user's shell PATH is not visible here — hence the fallback
 * directories that the installers (install.sh, uv tool, pipx, Homebrew) use.
 */
export const DEFAULT_FALLBACK_DIRS: readonly string[] = [
  path.join(homedir(), '.local', 'bin'),
  '/opt/homebrew/bin',
  '/usr/local/bin',
]

export interface LocateOptions {
  /** Explicit override from settings. Returned as-is when executable. */
  override?: string | null
  envPath?: string | undefined
  fallbackDirs?: readonly string[]
  binaryName?: string
}

export function locateCli(opts: LocateOptions = {}): string | null {
  const name = opts.binaryName ?? 'agentos'
  if (opts.override && isExecutable(opts.override)) return opts.override

  const envPath = opts.envPath ?? process.env.PATH ?? ''
  const dirs = [
    ...envPath.split(path.delimiter).filter(Boolean),
    ...(opts.fallbackDirs ?? DEFAULT_FALLBACK_DIRS),
  ]
  for (const dir of dirs) {
    const candidate = path.join(dir, name)
    if (isExecutable(candidate)) return candidate
  }
  return null
}

function isExecutable(file: string): boolean {
  try {
    accessSync(file, constants.X_OK)
    return true
  } catch {
    return false
  }
}
