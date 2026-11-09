export interface ClosureReport {
  desktopDirect: string[]
  desktopClosure: string[]
  webClosure: string[]
  orphans: string[]
  orphanTests: string[]
}
export function computeReport(): ClosureReport
