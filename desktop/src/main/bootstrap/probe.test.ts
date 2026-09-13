// @vitest-environment node
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { probeCli, siblingPython } from './discovery'

let dir: string
beforeEach(() => {
  dir = realpathSync(mkdtempSync(path.join(tmpdir(), 'agentos-probe-')))
})
afterEach(() => rmSync(dir, { recursive: true, force: true }))

function script(file: string, body: string): string {
  mkdirSync(path.dirname(file), { recursive: true })
  writeFileSync(file, `#!/bin/sh\n${body}\n`)
  chmodSync(file, 0o755)
  return file
}

/** A uv-tool-shaped install: ~/.local/bin/agentos -> tools/use-agent-os/bin/agentos, python beside it. */
function toolInstall(versionFlag: string, pythonVersion: string | null) {
  const venvBin = path.join(dir, 'tools', 'use-agent-os', 'bin')
  const real = script(path.join(venvBin, 'agentos'), versionFlag)
  if (pythonVersion !== null) script(path.join(venvBin, 'python'), `echo ${pythonVersion}`)
  const shimDir = path.join(dir, 'bin')
  mkdirSync(shimDir)
  const shim = path.join(shimDir, 'agentos')
  symlinkSync(real, shim)
  return shim
}

describe('probeCli', () => {
  it('reads the version from --version when the engine has it', async () => {
    const cli = toolInstall('echo 2026.9.12', '2026.9.12')
    expect(await probeCli(cli)).toEqual({ ok: true, version: '2026.9.12', detail: '' })
  })

  it('falls back to the venv interpreter for an engine without --version', async () => {
    // Typer's answer on 2026.9.9 and earlier.
    const cli = toolInstall('echo "No such option: --version" >&2; exit 2', '2026.9.9')
    expect(await probeCli(cli)).toEqual({
      ok: true,
      version: '2026.9.9',
      detail: 'via importlib.metadata',
    })
  })

  it('reports an unknown version when neither route answers', async () => {
    const cli = toolInstall('echo "No such option: --version" >&2; exit 2', null)
    expect(await probeCli(cli)).toEqual({
      ok: true,
      version: null,
      detail: 'engine predates --version',
    })
  })

  it('reports a CLI that crashes as not ok, with the last stderr lines', async () => {
    const cli = toolInstall('echo "Traceback"; echo "ImportError: boom" >&2; exit 1', '2026.9.9')
    const result = await probeCli(cli)
    expect(result.ok).toBe(false)
    expect(result.detail).toContain('ImportError: boom')
  })

  it('siblingPython follows the shim symlink into the venv', () => {
    const cli = toolInstall('echo x', '1')
    expect(siblingPython(cli)).toBe(path.join(dir, 'tools', 'use-agent-os', 'bin', 'python'))
    expect(siblingPython(path.join(dir, 'nope'))).toBeNull()
  })
})
