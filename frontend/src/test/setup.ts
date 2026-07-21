import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { transferableAbortController } from 'node:util'
import { afterEach, vi } from 'vitest'

function createMemoryStorage(): Storage {
  const values = new Map<string, string>()

  return {
    get length() {
      return values.size
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, String(value)),
  }
}

// Node 23's experimental storage globals are undefined unless the process was
// given --localstorage-file, and they shadow jsdom's implementations. Supply
// browser-compatible stores so tests exercise the APIs available in a real tab.
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  writable: true,
  value: createMemoryStorage(),
})
Object.defineProperty(globalThis, 'sessionStorage', {
  configurable: true,
  writable: true,
  value: createMemoryStorage(),
})

// Node's native Request rejects jsdom's cross-realm AbortSignal. React Router
// constructs both during navigation, so keep the controller/signal in Request's
// native realm to prevent false unhandled rejections under Node 23.
const nativeAbortController = transferableAbortController()
Object.defineProperty(globalThis, 'AbortController', {
  configurable: true,
  writable: true,
  value: nativeAbortController.constructor,
})
Object.defineProperty(globalThis, 'AbortSignal', {
  configurable: true,
  writable: true,
  value: nativeAbortController.signal.constructor,
})

// jsdom ships no `window.matchMedia`. `motion`'s `useReducedMotion()` reads
// `(prefers-reduced-motion: reduce)` through it, so without a stub the hook
// would see no match and attempt real enter/exit animations — which jsdom
// cannot drive to completion, hanging AnimatePresence exits (and the
// `waitFor(...).not.toBeInTheDocument()` unmount assertions with them). We
// report reduced-motion = true so motion degrades to instant mount/unmount,
// exactly as it does for a real user who prefers reduced motion. The unmount
// assertions still verify the dialog is genuinely removed.
if (typeof window !== 'undefined' && !window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList
}

// Standard test hygiene: unmount rendered trees and reset spy call history
// between tests so per-test `vi.fn()` call counts start from zero. Module
// mock factories (vi.mock) are unaffected; each test re-establishes its own
// mockResolvedValue/mockRejectedValue implementations.
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})
