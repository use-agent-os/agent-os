import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// Standard test hygiene: unmount rendered trees and reset spy call history
// between tests so per-test `vi.fn()` call counts start from zero. Module
// mock factories (vi.mock) are unaffected; each test re-establishes its own
// mockResolvedValue/mockRejectedValue implementations.
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})
