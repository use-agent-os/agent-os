import { mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { VaultKekStore, type SafeStorageLike } from './vault-kek'

/** A keychain stand-in: "encryption" is a tag plus a reversible transform. */
function fakeStorage(tag = 'k1', available = true): SafeStorageLike {
  return {
    isEncryptionAvailable: () => available,
    encryptString: (text) => Buffer.from(`${tag}:${Buffer.from(text).toString('base64')}`),
    decryptString: (blob) => {
      const [seenTag, body] = blob.toString().split(':')
      if (seenTag !== tag || !body) throw new Error('wrong key')
      return Buffer.from(body, 'base64').toString()
    },
  }
}

function tmpFile(): string {
  return path.join(mkdtempSync(path.join(os.tmpdir(), 'agentos-kek-')), 'nested', 'vault-kek.bin')
}

describe('VaultKekStore', () => {
  it('makes a 256-bit key on first use and returns the same one afterwards', () => {
    const file = tmpFile()
    const first = new VaultKekStore(file, fakeStorage()).load()
    expect(first).toMatch(/^[0-9a-f]{64}$/)
    expect(new VaultKekStore(file, fakeStorage()).load()).toBe(first)
  })

  it('stores only the sealed form, readable by the user alone', () => {
    const file = tmpFile()
    const kek = new VaultKekStore(file, fakeStorage()).load()!
    expect(readFileSync(file).toString()).not.toContain(kek)
    expect(statSync(file).mode & 0o777).toBe(0o600)
  })

  it('hands out nothing when the keychain is unavailable', () => {
    const file = tmpFile()
    expect(new VaultKekStore(file, fakeStorage('k1', false)).load()).toBeNull()
    expect(() => statSync(file)).toThrow()
  })

  it('hands out nothing when the keychain throws', () => {
    const storage: SafeStorageLike = {
      isEncryptionAvailable: () => {
        throw new Error('not ready')
      },
      encryptString: () => Buffer.alloc(0),
      decryptString: () => '',
    }
    expect(new VaultKekStore(tmpFile(), storage).load()).toBeNull()
  })

  it('replaces a key sealed under a keychain entry that is gone', () => {
    const file = tmpFile()
    const old = new VaultKekStore(file, fakeStorage('k1')).load()
    const next = new VaultKekStore(file, fakeStorage('k2')).load()
    expect(next).toMatch(/^[0-9a-f]{64}$/)
    expect(next).not.toBe(old)
    expect(new VaultKekStore(file, fakeStorage('k2')).load()).toBe(next)
  })

  it('replaces a file that does not hold a key', () => {
    const file = tmpFile()
    const storage = fakeStorage()
    new VaultKekStore(file, storage).load()
    writeFileSync(file, storage.encryptString('not a key'))
    expect(new VaultKekStore(file, storage).load()).toMatch(/^[0-9a-f]{64}$/)
  })

  it('does not hand out a key it could not store', () => {
    // The parent "directory" is a file: nothing can be written under it.
    const blocker = path.join(mkdtempSync(path.join(os.tmpdir(), 'agentos-kek-')), 'blocker')
    writeFileSync(blocker, '')
    expect(new VaultKekStore(path.join(blocker, 'vault-kek.bin'), fakeStorage()).load()).toBeNull()
  })
})
