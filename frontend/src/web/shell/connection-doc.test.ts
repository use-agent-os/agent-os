// docs/web-ui.md's runtime-status table names exactly the runtime-truth
// states (overhaul-control-ui task 4.8).
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { truthStateLabel, type TruthState } from '@/lib/runtime-truth'
import { CONNECT_TIMEOUT_MS } from './connection-gate'

const STATES: TruthState[] = ['ready', 'degraded', 'connecting', 'blocked', 'offline', 'unknown']

describe('docs/web-ui.md runtime status table', () => {
  it('documents every runtime-truth state and nothing else', () => {
    const doc = readFileSync('../docs/web-ui.md', 'utf8')
    const section = doc.slice(doc.indexOf('## Runtime status'), doc.indexOf('## Attention inbox'))
    const rows = section
      .split('\n')
      .filter(
        (line) => line.startsWith('| ') && !line.startsWith('| State') && !line.startsWith('| ---'),
      )
      .map((line) => line.split('|')[1]!.trim())
    expect(rows).toEqual(STATES.map(truthStateLabel))
  })

  it('documents the connection behaviours', () => {
    const doc = readFileSync('../docs/web-ui.md', 'utf8')
    for (const phrase of [
      '### Connect screen',
      '#token=',
      'Restarting gateway…',
      'PROTOCOL_MISMATCH',
      '2 seconds',
      `${CONNECT_TIMEOUT_MS / 1000} seconds`,
      '**Disconnect**',
      'token is never sent to that gateway',
    ]) {
      expect(doc).toContain(phrase)
    }
  })
})
