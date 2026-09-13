/**
 * electron-builder `afterSign` hook: make sure the packaged .app is signed.
 *
 * A release build is signed with the Developer ID identity (and notarized)
 * by electron-builder itself before this hook runs; the hook then only
 * verifies and leaves it alone — re-signing ad hoc would destroy that
 * signature and the notarization ticket with it. (It has to be `afterSign`,
 * not `afterPack`: `afterPack` runs before electron-builder signs, so the
 * ad-hoc signature would be applied and then overwritten every time.)
 *
 * A local build without an identity is left by electron-builder with only
 * Electron's linker signature (identifier "Electron", no resource seal).
 * macOS 15+ treats such a bundle as no app at all: notifications are refused
 * by `usernotificationsd` ("addRequest not allowed") and the app never shows
 * in System Settings › Notifications. A plain ad-hoc signature over the whole
 * bundle gives it a valid seal and its own identifier (`dev.agentos.desktop`),
 * which is enough for Notification Center.
 */
import { execFileSync, spawnSync } from 'node:child_process'
import path from 'node:path'

export default async function afterSign(context) {
  if (context.electronPlatformName !== 'darwin') return
  const app = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`)

  const info = signingInfo(app)
  if (/Authority=Developer ID Application/.test(info)) {
    execFileSync('codesign', ['--verify', '--deep', '--strict', app], { stdio: 'inherit' })
    console.log(`  • ${path.basename(app)} carries a Developer ID signature (afterSign: verified)`)
    return
  }

  execFileSync('codesign', ['--force', '--deep', '--sign', '-', app], { stdio: 'inherit' })
  execFileSync('codesign', ['--verify', '--deep', '--strict', app], { stdio: 'inherit' })
  console.log(`  • ad-hoc signed ${path.basename(app)} (afterSign: no Developer ID identity)`)
}

function signingInfo(app) {
  // codesign prints the signature description on stderr; an unsigned bundle
  // exits non-zero, which is just "no signature" here.
  const result = spawnSync('codesign', ['-dvv', app], { encoding: 'utf8' })
  return `${result.stdout ?? ''}${result.stderr ?? ''}`
}
