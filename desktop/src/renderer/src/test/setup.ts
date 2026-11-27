import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'
import { useComposerDrafts } from '~/stores/composer-drafts'

afterEach(() => {
  cleanup()
  // A draft outlives the composer by design; it must not outlive the test.
  useComposerDrafts.setState({ drafts: {} })
})
