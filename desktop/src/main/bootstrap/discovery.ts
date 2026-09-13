import { spawn as nodeSpawn } from 'node:child_process'
import { existsSync, realpathSync } from 'node:fs'
import path from 'node:path'
import type { GatewaySettings } from '@shared/settings'
import type { EngineDiscovery } from '@shared/bootstrap'
import { compareVersions } from '@shared/updates'
import { locateCli } from '../gateway/cli-locator'
import { hardenedEnv } from '../updates/engine-updater'

const VERSION_TIMEOUT_MS = 15_000

export interface ProbeResult {
  ok: boolean
  version: string | null
  detail: string
}

/**
 * `agentos --version`, the cheap smoke test: a CLI that prints a version
 * both exists and starts. An engine from before the flag existed answers
 * with Typer's "No such option"; that is still a real engine, so its version
 * is then read through the interpreter next to the entry point (a uv tool
 * or pipx venv keeps `python` beside `agentos`). Only when that fails too is
 * the version unknown.
 */
export async function probeCli(
  cli: string,
  spawn: typeof nodeSpawn = nodeSpawn,
): Promise<ProbeResult> {
  const first = await runVersionFlag(cli, spawn)
  if (first.ok && first.version === null) {
    const python = siblingPython(cli)
    if (python) {
      const viaPython = await runMetadataVersion(python, spawn)
      if (viaPython) return { ok: true, version: viaPython, detail: 'via importlib.metadata' }
    }
  }
  return first
}

/** `bin/python` of the venv the entry point lives in, following the shim symlink. */
export function siblingPython(cli: string): string | null {
  let target = cli
  try {
    target = realpathSync(cli)
  } catch {
    /* keep the shim path */
  }
  const candidate = path.join(path.dirname(target), 'python')
  return existsSync(candidate) ? candidate : null
}

function runMetadataVersion(python: string, spawn: typeof nodeSpawn): Promise<string | null> {
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(
        python,
        ['-c', "import importlib.metadata as m; print(m.version('use-agent-os'))"],
        { env: hardenedEnv(), stdio: ['ignore', 'pipe', 'pipe'] },
      )
    } catch {
      resolve(null)
      return
    }
    let stdout = ''
    child.stdout?.on('data', (c: Buffer) => (stdout += c.toString()))
    const timer = setTimeout(() => {
      child.kill('SIGKILL')
      resolve(null)
    }, VERSION_TIMEOUT_MS)
    child.once('error', () => {
      clearTimeout(timer)
      resolve(null)
    })
    child.once('exit', (code) => {
      clearTimeout(timer)
      const version = stdout.trim().split(/\s+/).pop() ?? ''
      resolve(code === 0 && /^\d+\.\d+/.test(version) ? version : null)
    })
  })
}

function runVersionFlag(cli: string, spawn: typeof nodeSpawn): Promise<ProbeResult> {
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(cli, ['--version'], { env: hardenedEnv(), stdio: ['ignore', 'pipe', 'pipe'] })
    } catch (err) {
      resolve({ ok: false, version: null, detail: String(err) })
      return
    }
    let stdout = ''
    let stderr = ''
    child.stdout?.on('data', (c: Buffer) => (stdout += c.toString()))
    child.stderr?.on('data', (c: Buffer) => (stderr += c.toString()))
    const timer = setTimeout(() => {
      child.kill('SIGKILL')
      resolve({ ok: false, version: null, detail: 'agentos --version timed out' })
    }, VERSION_TIMEOUT_MS)
    child.once('error', (err) => {
      clearTimeout(timer)
      resolve({ ok: false, version: null, detail: err.message })
    })
    child.once('exit', (code) => {
      clearTimeout(timer)
      const version = stdout.trim().split(/\s+/).pop() ?? ''
      if (code === 0 && /^\d+\.\d+/.test(version)) {
        resolve({ ok: true, version, detail: '' })
      } else if (/no such option/i.test(stderr)) {
        resolve({ ok: true, version: null, detail: 'engine predates --version' })
      } else {
        resolve({
          ok: false,
          version: null,
          detail: `exit ${code ?? 'null'}: ${stderr.trim().split('\n').slice(-2).join(' ')}`,
        })
      }
    })
  })
}

export interface DiscoverOptions {
  settings: GatewaySettings
  appVersion: string
  locate?: (override: string | null) => string | null
  probe?: (cli: string) => Promise<ProbeResult>
}

/**
 * Decide, at launch, whether the app can start a gateway or must install the
 * engine first. The rules, in order:
 *
 * 1. `gateway.cliPath` set in settings is the user's decision: used as-is,
 *    never reinstalled over (they may point at a checkout).
 * 2. A CLI found on PATH / the usual dirs is compared with the app version:
 *    same or newer → use it; older or unreadable → install this app's
 *    version over it ("the app always ships the engine it was built for").
 * 3. Nothing found → install.
 */
export async function discoverEngine(opts: DiscoverOptions): Promise<EngineDiscovery> {
  const locate = opts.locate ?? ((override) => locateCli({ override }))
  const probe = opts.probe ?? probeCli
  const appVersion = opts.appVersion

  const override = opts.settings.cliPath
  if (override) {
    const found = locate(override)
    if (found === override) {
      const result = await probe(found)
      return {
        source: 'override',
        cliPath: found,
        version: result.version,
        appVersion,
        relation: relationOf(result.version, appVersion),
        needsInstall: false,
        reason: result.ok
          ? 'Using the command-line tool set in Settings.'
          : `The command-line tool set in Settings does not run: ${result.detail}`,
      }
    }
  }

  const found = locate(null)
  if (!found) {
    return {
      source: 'missing',
      cliPath: null,
      version: null,
      appVersion,
      relation: null,
      needsInstall: true,
      reason: 'The AgentOS engine is not installed on this Mac.',
    }
  }

  const result = await probe(found)
  if (!result.ok) {
    return {
      source: 'found',
      cliPath: found,
      version: null,
      appVersion,
      relation: null,
      needsInstall: true,
      reason: `The installed engine does not start (${result.detail}); it will be reinstalled.`,
    }
  }
  const relation = relationOf(result.version, appVersion)
  if (relation === 'older' || relation === null) {
    return {
      source: 'found',
      cliPath: found,
      version: result.version,
      appVersion,
      relation,
      needsInstall: true,
      reason: result.version
        ? `Engine ${result.version} is older than this app (${appVersion}); it will be updated.`
        : `The installed engine is older than this app (${appVersion}); it will be updated.`,
    }
  }
  return {
    source: 'found',
    cliPath: found,
    version: result.version,
    appVersion,
    relation,
    needsInstall: false,
    reason:
      relation === 'newer'
        ? `Engine ${result.version} is newer than this app; keeping it.`
        : `Engine ${result.version} matches this app.`,
  }
}

function relationOf(version: string | null, appVersion: string): EngineDiscovery['relation'] {
  if (!version) return null
  const cmp = compareVersions(version.split('+')[0] ?? version, appVersion)
  return cmp < 0 ? 'older' : cmp > 0 ? 'newer' : 'same'
}
