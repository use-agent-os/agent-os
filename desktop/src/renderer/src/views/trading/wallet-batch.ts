/**
 * The pure halves of the bulk wallet flows: the CSV a new batch downloads as,
 * and the rows a pasted list of private keys reads as.
 */

/** The most wallets one `wallet.createBatch` makes (the engine's cap too). */
export const MAX_BATCH_CREATE = 30
/** The most keys one `wallet.importBatch` takes (the engine's cap too). */
export const MAX_BATCH_IMPORT = 100

/** One wallet's secrets, as `wallet.exportBatch` returns them. */
export interface MintedSecret {
  address: string
  privateKey: string
  mnemonic: string
}

function csvField(value: string): string {
  return /[",\r\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value
}

/** `no,address,private_key,seed_phrase`, one row per wallet in batch order, CRLF line ends. */
export function walletsCsv(rows: readonly MintedSecret[]): string {
  const lines = [['no', 'address', 'private_key', 'seed_phrase'].join(',')]
  rows.forEach((row, i) => {
    lines.push([String(i + 1), row.address, row.privateKey, row.mnemonic].map(csvField).join(','))
  })
  return `${lines.join('\r\n')}\r\n`
}

/** `agentos-wallets-20261116-0930.csv`, in local time. */
export function walletsCsvName(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  const day = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
  return `agentos-wallets-${day}-${pad(now.getHours())}${pad(now.getMinutes())}.csv`
}

/** A pasted key and the line it came from (1-based), so a failure can name the line. */
export interface KeyRow {
  line: number
  key: string
}

/**
 * One private key per line. Blank lines are skipped; each line is trimmed and
 * nothing else is guessed at: a malformed key is the engine's to refuse, by
 * line, without the key being echoed back.
 */
export function parseKeyRows(text: string): KeyRow[] {
  const rows: KeyRow[] = []
  text.split(/\r\n|\r|\n/).forEach((raw, i) => {
    const key = raw.trim()
    if (key) rows.push({ line: i + 1, key })
  })
  return rows
}

/** Save `text` as a file through the browser's download (Electron asks where). */
export function downloadFile(text: string, filename: string, type = 'text/csv'): void {
  const blob = new Blob([text], { type })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = filename
  a.click()
  URL.revokeObjectURL(a.href)
}
