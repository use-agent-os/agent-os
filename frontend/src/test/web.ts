// Browser APIs the Radix primitives touch that jsdom does not ship. Imported by
// the web console's tests; harmless for any other suite.

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

if (typeof globalThis.ResizeObserver === 'undefined') {
  ;(globalThis as { ResizeObserver?: unknown }).ResizeObserver = ResizeObserverStub
}

if (typeof Element !== 'undefined') {
  const proto = Element.prototype as Element & {
    hasPointerCapture?: (id: number) => boolean
    releasePointerCapture?: (id: number) => void
    setPointerCapture?: (id: number) => void
    scrollIntoView?: () => void
  }
  proto.hasPointerCapture ??= () => false
  proto.releasePointerCapture ??= () => {}
  proto.setPointerCapture ??= () => {}
  proto.scrollIntoView ??= () => {}
}

/** Set what `(prefers-reduced-motion: reduce)` and friends report. */
export function stubMatchMedia(matches: (query: string) => boolean): void {
  window.matchMedia = (query: string) =>
    ({
      matches: matches(query),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList
}
