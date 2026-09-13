/**
 * Make the development Electron.app a real app in macOS's eyes.
 *
 * The npm-distributed `Electron.app` carries only a linker signature and no
 * resource seal, so `codesign --verify` rejects it. macOS 15+ then refuses
 * everything that needs an app identity, notifications first among them:
 * `usernotificationsd` logs "addRequest not allowed: com.github.Electron"
 * and `Notification.show()` silently does nothing in `npm run dev`. A fresh
 * ad-hoc signature fixes the seal, and registering the bundle with
 * LaunchServices lets the notification daemon find its record (otherwise
 * the permission request fails with LS error -10814).
 *
 * Runs after `npm ci` and before `npm run dev`; a no-op when the bundle is
 * already valid, and never fails the install.
 */
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const LSREGISTER =
  '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'

function run(cmd, args) {
  execFileSync(cmd, args, { stdio: ['ignore', 'ignore', 'pipe'] })
}

function main() {
  if (process.platform !== 'darwin') return
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
  const app = path.join(root, 'node_modules', 'electron', 'dist', 'Electron.app')
  if (!existsSync(app)) return // binary not downloaded yet (sandboxed install)

  let valid = true
  try {
    run('codesign', ['--verify', '--deep', '--strict', app])
  } catch {
    valid = false
  }
  if (!valid) {
    run('codesign', ['--force', '--deep', '--sign', '-', app])
    console.log(
      '[desktop] re-signed node_modules/electron Electron.app (ad-hoc) so macOS notifications work in dev',
    )
  }
  if (existsSync(LSREGISTER)) run(LSREGISTER, ['-f', app])
}

try {
  main()
} catch (err) {
  console.warn(
    `[desktop] could not prepare Electron.app for macOS notifications: ${err?.message ?? err}`,
  )
}
