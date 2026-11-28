import { randomBytes } from 'node:crypto'
import { chmodSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import path from 'node:path'

/** The part of Electron's `safeStorage` this needs; injectable for tests. */
export interface SafeStorageLike {
  isEncryptionAvailable(): boolean
  encryptString(plainText: string): Buffer
  decryptString(encrypted: Buffer): string
}

const KEK_HEX = /^[0-9a-f]{64}$/

/**
 * The key that opens the trading vault's stored password (`unlock.key`), kept
 * by the app instead of in a file beside the vault.
 *
 * The gateway's own store for that key is a 0600 file in the user's home: a
 * process running as the user (an agent's shell with the OS sandbox off) can
 * read it and the vault together. Here the key is a random 256-bit value
 * sealed with `safeStorage`, whose own key lives in the macOS keychain under
 * an entry only this signed app may read without the user being asked. What
 * is on disk (`vault-kek.bin`) is useless without that keychain entry.
 *
 * The supervisor hands the key to the gateway it spawns over a pipe
 * (`AGENTOS_VAULT_KEK_FD`); the gateway seals the vault password under it and
 * deletes the key file it used before (src/agentos/trading/unlock_key.py).
 *
 * Losing the key (a reset keychain, a deleted app-data folder) costs nothing
 * but one prompt: the vault stays locked at the next start, the user unlocks
 * with the vault password, and the gateway seals it again under the new key.
 */
export class VaultKekStore {
  constructor(
    private readonly file: string,
    private readonly storage: SafeStorageLike,
  ) {}

  /**
   * The key as 64 hex characters, made on first use. Null when the keychain
   * is not available: the gateway then keeps its own key, as before. Never
   * throws, never logs the key.
   */
  load(): string | null {
    try {
      if (!this.storage.isEncryptionAvailable()) return null
    } catch {
      return null
    }
    return this.read() ?? this.create()
  }

  private read(): string | null {
    try {
      const kek = this.storage.decryptString(readFileSync(this.file)).trim()
      return KEK_HEX.test(kek) ? kek : null
    } catch {
      // No file yet, or one sealed under a keychain entry that is gone.
      return null
    }
  }

  private create(): string | null {
    const kek = randomBytes(32).toString('hex')
    try {
      mkdirSync(path.dirname(this.file), { recursive: true })
      // Written whole, then renamed: a crash never leaves half a key that
      // reads back as "no key" and replaces one the vault is sealed under.
      const tmp = `${this.file}.tmp`
      writeFileSync(tmp, this.storage.encryptString(kek), { mode: 0o600 })
      chmodSync(tmp, 0o600)
      renameSync(tmp, this.file)
      return kek
    } catch {
      // A key that cannot be stored must not be handed out: the gateway
      // would seal the password under it and no later launch could open it.
      return null
    }
  }
}
