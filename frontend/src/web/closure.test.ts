// After the cutover nothing under frontend/src may sit outside both the
// desktop's and the web console's import closures (overhaul-control-ui D2).
import { describe, expect, it } from 'vitest'
import { computeReport } from '../../scripts/ui-closure.mjs'

describe('ui closure', () => {
  it('leaves no orphan file under frontend/src', () => {
    const report = computeReport()
    expect(report.orphans).toEqual([])
    expect(report.orphanTests).toEqual([])
  })

  it('keeps every module desktop imports', () => {
    expect(computeReport().desktopDirect.length).toBeGreaterThan(50)
  })
})
