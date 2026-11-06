import {
  Check,
  Circle,
  CircleCheck,
  Copy,
  ExternalLink,
  KeyRound,
  Lock,
  Pencil,
  Plus,
  QrCode,
  Trash2,
  TriangleAlert,
  Wallet as WalletIcon,
} from 'lucide-react'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { qrDataUrl } from '@/lib/qr'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { isTouchIdDeclined, useTouchIdPrompt } from '~/lib/biometric-gate'
import { desktopApi } from '~/lib/desktop-api'
import {
  usePortfolio,
  useTradingStatus,
  useWalletMutation,
  useWallets,
  useWalletStatus,
} from '~/stores/trading'
import { ChainBadge } from './ChainMark'
import { activeFirst, errorText, formatPct, pnlTone, shortAddress, walletLabel } from './logic'
import { Money, Sheet } from './parts'
import type { ChainStatus, Totals, UnlockMode, Wallet } from './types'

export type WalletSheetMode =
  | { kind: 'setup' }
  | { kind: 'unlock' }
  | { kind: 'manage' }
  | { kind: 'create' }
  | { kind: 'import' }
  | { kind: 'rename'; wallet: Wallet }
  | { kind: 'export'; wallet: Wallet }
  | { kind: 'remove'; wallet: Wallet }
  | { kind: 'receive'; wallet: Wallet }

/**
 * Every write to the vault, one sheet each: create the vault, unlock it,
 * manage the wallets, create or import one, rename, export, remove. Secrets
 * typed here live in component state and are gone when the sheet closes.
 *
 * `manage` is the hub, and the flows it opens are held here rather than
 * handed back to the caller: a flow reached from the manager closes back into
 * the manager, so renaming three wallets is three clicks and not three trips
 * out to the rail.
 */
export function WalletSheet({ mode, onClose }: { mode: WalletSheetMode; onClose: () => void }) {
  const [sub, setSub] = useState<WalletSheetMode | null>(null)
  const [seenMode, setSeenMode] = useState(mode)
  // The caller moved the sheet itself: whatever the hub had open is stale.
  if (seenMode !== mode) {
    setSeenMode(mode)
    if (sub) setSub(null)
  }
  const active = sub ?? mode
  const close = sub ? () => setSub(null) : onClose

  switch (active.kind) {
    case 'setup':
      return <SetupSheet onClose={close} />
    case 'unlock':
      return <UnlockSheet onClose={close} />
    case 'manage':
      return <ManageSheet onClose={close} onOpen={setSub} />
    case 'create':
      return <CreateSheet onClose={close} />
    case 'import':
      return <ImportSheet onClose={close} />
    case 'rename':
      return <RenameSheet wallet={active.wallet} onClose={close} />
    case 'export':
      return <ExportSheet wallet={active.wallet} onClose={close} />
    case 'remove':
      return <RemoveSheet wallet={active.wallet} onClose={close} />
    case 'receive':
      return <ReceiveSheet wallet={active.wallet} onClose={close} />
  }
}

/**
 * The address, big, with a QR of it.
 *
 * The QR is drawn in this process from the address bytes — never fetched from
 * a QR web service, which would disclose the address to whoever runs it. It
 * encodes the bare address and nothing else: no amount, no chain, no EIP-681
 * payment URI. A scan therefore means "here is where to send", which is true
 * on every chain this wallet is on, and cannot be mistaken for a request the
 * sender is agreeing to.
 */
function ReceiveSheet({ wallet, onClose }: { wallet: Wallet; onClose: () => void }) {
  const [copied, setCopied] = useState(false)
  const label = walletLabel(wallet)
  const qr = useMemo(() => qrDataUrl(wallet.address, { size: 200 }), [wallet.address])

  async function copyAddress() {
    try {
      await navigator.clipboard.writeText(wallet.address)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1400)
      toast.success(t('trading.rail.copied'), { id: 'trd-copy' })
    } catch {
      /* clipboard unavailable */
    }
  }

  return (
    <Sheet
      title={`${t('trading.sheet.receive.title')} · ${label}`}
      onClose={onClose}
      foot={
        <>
          <Button onClick={onClose}>{t('trading.sheet.close')}</Button>
          <Button variant="primary" onClick={() => void copyAddress()} data-testid="receive-copy">
            {copied ? t('trading.rail.copied') : t('trading.rail.receive')}
          </Button>
        </>
      }
    >
      <div className="trd-qr">
        <img className="trd-qr__code" src={qr} alt={t('trading.sheet.receive.alt')} width={200} />
        <code className="trd-qr__addr trd-num" data-testid="receive-address">
          {wallet.address}
        </code>
        <p className="trd-qr__note">{t('trading.sheet.receive.note')}</p>
      </div>
    </Sheet>
  )
}

/**
 * The wallet manager: every wallet the vault holds, with the facts a wallet
 * actually has — its name, its full address, what it is worth, which chains
 * it lives on — and every action that acts on one, in reach.
 *
 * The address is written out in full rather than shortened. This is the one
 * screen whose job is the address: a truncated `0x1111…1111` cannot be read
 * against a hardware wallet or pasted from a screenshot, and the whole
 * complaint that led here was that the desk never showed it anywhere.
 *
 * The chain badges are the explorer links. One row per chain would have been
 * five more buttons on a surface that already carries four.
 *
 * The active wallet (the vault's primary, the one the agent spends from) is
 * always the first card, so which one it is never takes a scroll to find.
 */
function ManageSheet({
  onClose,
  onOpen,
}: {
  onClose: () => void
  onOpen: (m: WalletSheetMode) => void
}) {
  const { wallets, isPending } = useWallets()
  const portfolio = usePortfolio(undefined, true)
  const status = useTradingStatus()
  const vault = useWalletStatus()
  const chains = status.data?.chains ?? []
  const locked = Boolean(vault.data?.initialized && !vault.data.unlocked)
  const listRef = useRef<HTMLDivElement>(null)

  const totalsByWallet = useMemo(() => {
    const m = new Map<string, Totals>()
    for (const row of portfolio.data?.wallets ?? [])
      m.set(row.wallet.address.toLowerCase(), row.totals)
    return m
  }, [portfolio.data])

  const ordered = useMemo(() => activeFirst(wallets), [wallets])

  // Activating a wallet moves its card to the top, out of a view that is
  // usually scrolled down to it: follow it, or the press looks like it did
  // nothing and the card underneath is suddenly a different wallet.
  const activeAddress = ordered[0]?.primary ? ordered[0].address : null
  useEffect(() => {
    const body = listRef.current?.closest('.trd-modal__body')
    if (body) body.scrollTop = 0
  }, [activeAddress])

  return (
    <Sheet
      title={t('trading.rail.title')}
      onClose={onClose}
      wide
      note={t('trading.sheet.manage.note')}
      foot={
        <>
          <Button onClick={() => onOpen({ kind: 'import' })} data-testid="manage-import">
            <KeyRound className="size-3.5" strokeWidth={1.75} aria-hidden />
            {t('trading.rail.import')}
          </Button>
          <Button
            variant="primary"
            onClick={() => onOpen({ kind: 'create' })}
            data-testid="manage-create"
          >
            <Plus className="size-3.5" strokeWidth={2} aria-hidden />
            {t('trading.rail.create')}
          </Button>
        </>
      }
    >
      {locked ? (
        <button
          type="button"
          className="trd-wman__locked"
          onClick={() => onOpen({ kind: 'unlock' })}
          data-testid="manage-unlock"
        >
          <Lock className="size-3.5" strokeWidth={1.75} aria-hidden />
          {t('trading.sheet.manage.locked')}
        </button>
      ) : null}

      {isPending ? (
        <div className="trd-wman">
          {[0, 1].map((i) => (
            <div key={i} className="trd-wman__row">
              <span className="trd-skel" style={{ width: '60%', height: 16 }} />
              <span className="trd-skel" style={{ width: '100%', height: 14 }} />
            </div>
          ))}
        </div>
      ) : wallets.length === 0 ? (
        <p className="trd-wman__empty">
          <WalletIcon className="size-4" strokeWidth={1.75} aria-hidden />
          {t('trading.sheet.manage.empty')}
        </p>
      ) : (
        <div className="trd-wman" ref={listRef}>
          {ordered.map((w) => (
            <ManageRow
              key={w.address}
              wallet={w}
              totals={totalsByWallet.get(w.address.toLowerCase())}
              chains={chains}
              locked={locked}
              onOpen={onOpen}
            />
          ))}
        </div>
      )}
    </Sheet>
  )
}

function ManageRow({
  wallet,
  totals,
  chains,
  locked,
  onOpen,
}: {
  wallet: Wallet
  totals: Totals | undefined
  chains: ChainStatus[]
  /** Export and Remove need the vault open; the buttons say so rather than fail. */
  locked: boolean
  onOpen: (m: WalletSheetMode) => void
}) {
  const [copied, setCopied] = useState(false)
  const write = useWalletMutation()
  const label = walletLabel(wallet)
  const delta = totals?.change24hUsd ?? null

  async function copyAddress() {
    try {
      await navigator.clipboard.writeText(wallet.address)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1400)
      toast.success(t('trading.rail.copied'), { id: 'trd-copy' })
    } catch {
      /* clipboard unavailable */
    }
  }

  return (
    <section className="trd-wman__row" data-primary={wallet.primary || undefined}>
      <header className="trd-wman__top">
        <span className="trd-wman__name">
          {wallet.primary ? (
            <CircleCheck
              className="trd-wman__active size-3.5"
              strokeWidth={2}
              aria-label={t('trading.rail.active')}
            />
          ) : null}
          {label}
        </span>
        <span className="trd-wman__value">
          {totals ? (
            <Money value={totals.valueUsd} />
          ) : (
            <span className="trd-skel" style={{ width: 56 }} />
          )}
          {totals ? (
            <span className="trd-wman__delta trd-num" data-tone={pnlTone(delta)}>
              {formatPct(totals.change24hPct ?? null, { signed: true })}
            </span>
          ) : null}
        </span>
      </header>

      <div className="trd-wman__addr">
        {/* Selectable and complete: this is the value people came for. */}
        <code className="trd-num" data-testid="manage-address">
          {wallet.address}
        </code>
        <button
          type="button"
          className="trd-wman__copy"
          onClick={() => void copyAddress()}
          title={t('trading.rail.receive')}
          aria-label={`${t('trading.rail.receive')} · ${label}`}
          data-testid="manage-copy"
        >
          {copied ? (
            <Check className="size-3.5" strokeWidth={2.5} aria-hidden />
          ) : (
            <Copy className="size-3.5" strokeWidth={1.75} aria-hidden />
          )}
        </button>
      </div>

      {wallet.chains.length ? (
        <div className="trd-wman__chains">
          {wallet.chains.map((chainId) => {
            const chain = chains.find((c) => c.chainId === chainId)
            if (!chain) return <ChainBadge key={chainId} chainId={chainId} />
            return (
              <button
                key={chainId}
                type="button"
                className="trd-wman__chain"
                title={`${t('trading.rail.explorer')} · ${chain.name}`}
                onClick={() =>
                  void desktopApi().app.openExternal(`${chain.explorer}/address/${wallet.address}`)
                }
              >
                <ChainBadge chainId={chainId} />
                <ExternalLink className="size-3 opacity-60" strokeWidth={1.75} aria-hidden />
              </button>
            )
          })}
        </div>
      ) : null}

      <footer className="trd-wman__acts">
        {/* On every card, so "Active" is never a word only the inactive wallets
            carry. One wallet is always active, so it switches on and not off. */}
        <button
          type="button"
          className="trd-wman__act"
          aria-pressed={wallet.primary}
          aria-disabled={wallet.primary || undefined}
          disabled={!wallet.primary && write.isPending}
          data-testid="manage-primary"
          onClick={() => {
            if (wallet.primary) return
            write.mutate(
              { method: 'wallet.setPrimary', params: { address: wallet.address } },
              {
                onError: (err) => toast.error(`${t('trading.sheet.error')}: ${errorText(err)}`),
              },
            )
          }}
        >
          {wallet.primary ? (
            <CircleCheck className="size-3.5" strokeWidth={2} aria-hidden />
          ) : (
            <Circle className="size-3.5" strokeWidth={1.75} aria-hidden />
          )}
          {t('trading.rail.active')}
        </button>
        <button
          type="button"
          className="trd-wman__act"
          onClick={() => onOpen({ kind: 'receive', wallet })}
          data-testid="manage-qr"
        >
          <QrCode className="size-3.5" strokeWidth={1.75} aria-hidden />
          {t('trading.rail.qr')}
        </button>
        <button
          type="button"
          className="trd-wman__act"
          onClick={() => onOpen({ kind: 'rename', wallet })}
          data-testid="manage-rename"
        >
          <Pencil className="size-3.5" strokeWidth={1.75} aria-hidden />
          {t('trading.rail.rename')}
        </button>
        <button
          type="button"
          className="trd-wman__act"
          disabled={locked}
          title={locked ? t('trading.sheet.manage.lockedAction') : undefined}
          onClick={() => onOpen({ kind: 'export', wallet })}
          data-testid="manage-export"
        >
          <KeyRound className="size-3.5" strokeWidth={1.75} aria-hidden />
          {t('trading.rail.export')}
        </button>
        <button
          type="button"
          className="trd-wman__act trd-wman__act--danger"
          disabled={locked}
          title={locked ? t('trading.sheet.manage.lockedAction') : undefined}
          onClick={() => onOpen({ kind: 'remove', wallet })}
          data-testid="manage-remove"
        >
          <Trash2 className="size-3.5" strokeWidth={1.75} aria-hidden />
          {t('trading.rail.remove')}
        </button>
      </footer>
    </section>
  )
}

function Field({
  id,
  label,
  help,
  error,
  children,
}: {
  id?: string
  label: string
  help?: string
  error?: string | null
  children: React.ReactNode
}) {
  return (
    <div className="trd-field">
      <label htmlFor={id}>{label}</label>
      {children}
      {error ? (
        <span className="trd-field__error">{error}</span>
      ) : help ? (
        <span className="trd-field__help">{help}</span>
      ) : null}
    </div>
  )
}

function SetupSheet({ onClose }: { onClose: () => void }) {
  const ids = { pw: useId(), pw2: useId() }
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [unlockMode, setUnlockMode] = useState<UnlockMode>('auto')
  const m = useWalletMutation()
  const short = pw.length > 0 && pw.length < 8
  const mismatch = pw2.length > 0 && pw !== pw2
  const ok = pw.length >= 8 && pw === pw2
  return (
    <Sheet
      title={t('trading.sheet.setup.title')}
      onClose={onClose}
      foot={
        <>
          <Button onClick={onClose} disabled={m.isPending}>
            {t('trading.sheet.cancel')}
          </Button>
          <Button
            variant="primary"
            disabled={!ok || m.isPending}
            data-testid="setup-submit"
            onClick={() =>
              m.mutate(
                { method: 'wallet.setup', params: { password: pw, unlockMode } },
                {
                  onSuccess: onClose,
                  onError: (err) => toast.error(`${t('trading.sheet.error')}: ${errorText(err)}`),
                },
              )
            }
          >
            {m.isPending ? t('trading.sheet.working') : t('trading.sheet.setup.cta')}
          </Button>
        </>
      }
    >
      <p className="text-[12.5px] text-muted-foreground">{t('trading.setup.body')}</p>
      <Field
        id={ids.pw}
        label={t('trading.sheet.setup.password')}
        error={short ? t('trading.sheet.setup.short') : null}
      >
        <input
          id={ids.pw}
          className="mac-input"
          type="password"
          autoComplete="new-password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
        />
      </Field>
      <Field
        id={ids.pw2}
        label={t('trading.sheet.setup.confirm')}
        error={mismatch ? t('trading.sheet.setup.mismatch') : null}
      >
        <input
          id={ids.pw2}
          className="mac-input"
          type="password"
          autoComplete="new-password"
          value={pw2}
          onChange={(e) => setPw2(e.target.value)}
        />
      </Field>
      <div className="trd-field">
        <span className="trd-field__label">{t('trading.sheet.setup.mode')}</span>
        <div className="trd-choice" role="radiogroup" aria-label={t('trading.sheet.setup.mode')}>
          <label>
            <input
              type="radio"
              name="unlock"
              checked={unlockMode === 'auto'}
              onChange={() => setUnlockMode('auto')}
            />
            <b>{t('trading.sheet.setup.mode.auto')}</b>
            <span>{t('trading.sheet.setup.mode.auto.help')}</span>
          </label>
          <label>
            <input
              type="radio"
              name="unlock"
              checked={unlockMode === 'manual'}
              onChange={() => setUnlockMode('manual')}
            />
            <b>{t('trading.sheet.setup.mode.manual')}</b>
            <span>{t('trading.sheet.setup.mode.manual.help')}</span>
          </label>
        </div>
      </div>
    </Sheet>
  )
}

function UnlockSheet({ onClose }: { onClose: () => void }) {
  const id = useId()
  const [pw, setPw] = useState('')
  const [wrong, setWrong] = useState(false)
  const m = useWalletMutation()
  function submit() {
    setWrong(false)
    m.mutate(
      { method: 'wallet.unlock', params: { password: pw } },
      {
        onSuccess: onClose,
        onError: () => setWrong(true),
      },
    )
  }
  return (
    <Sheet
      title={t('trading.sheet.unlock.title')}
      onClose={onClose}
      foot={
        <>
          <Button onClick={onClose} disabled={m.isPending}>
            {t('trading.sheet.cancel')}
          </Button>
          <Button
            variant="primary"
            disabled={!pw || m.isPending}
            onClick={submit}
            data-testid="unlock-submit"
          >
            {m.isPending ? t('trading.sheet.working') : t('trading.sheet.unlock.cta')}
          </Button>
        </>
      }
    >
      <Field
        id={id}
        label={t('trading.sheet.password')}
        error={wrong ? t('trading.sheet.unlock.wrong') : null}
      >
        <input
          id={id}
          className="mac-input"
          type="password"
          autoComplete="current-password"
          autoFocus
          value={pw}
          data-invalid={wrong ? 'true' : undefined}
          onChange={(e) => setPw(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && pw) submit()
          }}
        />
      </Field>
    </Sheet>
  )
}

function CreateSheet({ onClose }: { onClose: () => void }) {
  const id = useId()
  const [label, setLabel] = useState('')
  const m = useWalletMutation<{ wallet: Wallet }>()
  return (
    <Sheet
      title={t('trading.sheet.create.title')}
      onClose={onClose}
      foot={
        <>
          <Button onClick={onClose} disabled={m.isPending}>
            {t('trading.sheet.cancel')}
          </Button>
          <Button
            variant="primary"
            disabled={m.isPending}
            data-testid="create-submit"
            onClick={() =>
              m.mutate(
                {
                  method: 'wallet.create',
                  params: { label: label.trim() || t('trading.sheet.create.label.placeholder') },
                },
                {
                  onSuccess: (res) => {
                    toast.success(t('trading.sheet.create.done'), {
                      description: res?.wallet ? shortAddress(res.wallet.address) : undefined,
                    })
                    onClose()
                  },
                  onError: (err) => toast.error(`${t('trading.sheet.error')}: ${errorText(err)}`),
                },
              )
            }
          >
            {m.isPending ? t('trading.sheet.working') : t('trading.sheet.create.cta')}
          </Button>
        </>
      }
    >
      <Field id={id} label={t('trading.sheet.create.label')}>
        <input
          id={id}
          className="mac-input"
          autoFocus
          placeholder={t('trading.sheet.create.label.placeholder')}
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
      </Field>
    </Sheet>
  )
}

function ImportSheet({ onClose }: { onClose: () => void }) {
  const ids = { label: useId(), key: useId(), ks: useId(), kspw: useId() }
  const [kind, setKind] = useState<'key' | 'keystore'>('key')
  const [label, setLabel] = useState('')
  const [key, setKey] = useState('')
  const [keystore, setKeystore] = useState('')
  const [ksPassword, setKsPassword] = useState('')
  const m = useWalletMutation<{ wallet: Wallet }>()
  const ready = kind === 'key' ? key.trim().length >= 64 : keystore.trim().length > 0
  return (
    <Sheet
      title={t('trading.sheet.import.title')}
      onClose={onClose}
      wide
      foot={
        <>
          <Button onClick={onClose} disabled={m.isPending}>
            {t('trading.sheet.cancel')}
          </Button>
          <Button
            variant="primary"
            disabled={!ready || m.isPending}
            data-testid="import-submit"
            onClick={() =>
              m.mutate(
                {
                  method: 'wallet.import',
                  params:
                    kind === 'key'
                      ? {
                          label: label.trim() || t('trading.sheet.import.label.default'),
                          privateKey: key.trim(),
                        }
                      : {
                          label: label.trim() || t('trading.sheet.import.label.default'),
                          keystoreJson: keystore.trim(),
                          keystorePassword: ksPassword,
                        },
                },
                {
                  onSuccess: (res) => {
                    toast.success(t('trading.sheet.import.done'), {
                      description: res?.wallet ? shortAddress(res.wallet.address) : undefined,
                    })
                    onClose()
                  },
                  onError: (err) => toast.error(`${t('trading.sheet.error')}: ${errorText(err)}`),
                },
              )
            }
          >
            {m.isPending ? t('trading.sheet.working') : t('trading.sheet.import.cta')}
          </Button>
        </>
      }
    >
      <div className="trd-field">
        <span className="trd-field__label">{t('trading.sheet.import.kind')}</span>
        <div
          role="radiogroup"
          aria-label={t('trading.sheet.import.kind')}
          className="mac-segmented self-start"
        >
          <button
            type="button"
            role="radio"
            aria-checked={kind === 'key'}
            className="mac-segment"
            onClick={() => setKind('key')}
          >
            {t('trading.sheet.import.kind.key')}
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={kind === 'keystore'}
            className="mac-segment"
            onClick={() => setKind('keystore')}
          >
            {t('trading.sheet.import.kind.keystore')}
          </button>
        </div>
      </div>
      <Field id={ids.label} label={t('trading.sheet.create.label')}>
        <input
          id={ids.label}
          className="mac-input"
          placeholder={t('trading.sheet.create.label.placeholder')}
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
      </Field>
      {kind === 'key' ? (
        <Field
          id={ids.key}
          label={t('trading.sheet.import.key')}
          help={t('trading.sheet.import.key.help')}
        >
          <input
            id={ids.key}
            className="mac-input"
            data-mono="true"
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder={t('trading.sheet.import.key.placeholder')}
            value={key}
            onChange={(e) => setKey(e.target.value)}
          />
        </Field>
      ) : (
        <>
          <Field id={ids.ks} label={t('trading.sheet.import.keystore')}>
            <textarea
              id={ids.ks}
              className="mac-textarea"
              data-mono="true"
              rows={5}
              spellCheck={false}
              placeholder={t('trading.sheet.import.keystore.placeholder')}
              value={keystore}
              onChange={(e) => setKeystore(e.target.value)}
            />
          </Field>
          <Field id={ids.kspw} label={t('trading.sheet.import.keystore.password')}>
            <input
              id={ids.kspw}
              className="mac-input"
              type="password"
              autoComplete="off"
              value={ksPassword}
              onChange={(e) => setKsPassword(e.target.value)}
            />
          </Field>
        </>
      )}
      <p className="trd-field__help">{t('trading.sheet.import.history')}</p>
    </Sheet>
  )
}

function RenameSheet({ wallet, onClose }: { wallet: Wallet; onClose: () => void }) {
  const id = useId()
  const [label, setLabel] = useState(wallet.label)
  const m = useWalletMutation()
  return (
    <Sheet
      title={t('trading.sheet.rename.title')}
      onClose={onClose}
      foot={
        <>
          <Button onClick={onClose} disabled={m.isPending}>
            {t('trading.sheet.cancel')}
          </Button>
          <Button
            variant="primary"
            disabled={!label.trim() || m.isPending}
            onClick={() =>
              m.mutate(
                {
                  method: 'wallet.rename',
                  params: { address: wallet.address, label: label.trim() },
                },
                {
                  onSuccess: onClose,
                  onError: (err) => toast.error(`${t('trading.sheet.error')}: ${errorText(err)}`),
                },
              )
            }
          >
            {t('trading.sheet.rename.cta')}
          </Button>
        </>
      }
    >
      <Field id={id} label={t('trading.sheet.create.label')}>
        <input
          id={id}
          className="mac-input"
          autoFocus
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
      </Field>
    </Sheet>
  )
}

function ExportSheet({ wallet, onClose }: { wallet: Wallet; onClose: () => void }) {
  const ids = { pw: useId() }
  const [format, setFormat] = useState<'keystore' | 'privateKey'>('keystore')
  const [pw, setPw] = useState('')
  const [secret, setSecret] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const m = useWalletMutation<{ keystoreJson?: string; privateKey?: string }>()
  // Touch ID (Settings › Security) runs inside the mutation, before the
  // password is sent; the button says so while the sheet is up.
  const prompting = useTouchIdPrompt((s) => s.key === `wallet:${wallet.address.toLowerCase()}`)
  function reveal() {
    // Return in the password field must not start a second export (or a
    // second Touch ID prompt) while the first is in flight.
    if (m.isPending) return
    m.mutate(
      {
        method: 'wallet.export',
        params: { address: wallet.address, password: pw, format },
        label: wallet.label,
      },
      {
        onSuccess: (res) => {
          const value = res?.privateKey ?? res?.keystoreJson ?? null
          setSecret(value)
          setPw('')
        },
        onError: (err) => {
          // Declined: the gate has said why, and nothing was sent.
          if (isTouchIdDeclined(err)) return
          toast.error(`${t('trading.sheet.error')}: ${errorText(err)}`)
        },
      },
    )
  }
  // A copied key must not sit in the clipboard indefinitely. Best effort: a
  // minute later, if the clipboard still holds exactly this secret, it is
  // blanked; anything else pasted over it in the meantime is left alone.
  const clearTimer = useRef<number | null>(null)
  useEffect(
    () => () => {
      if (clearTimer.current !== null) window.clearTimeout(clearTimer.current)
    },
    [],
  )
  async function copy() {
    if (!secret) return
    try {
      await navigator.clipboard.writeText(secret)
      setCopied(true)
      toast.success(t('trading.sheet.export.clipboard'), { id: 'trd-export' })
      if (clearTimer.current !== null) window.clearTimeout(clearTimer.current)
      clearTimer.current = window.setTimeout(() => {
        void clearClipboardIf(secret)
      }, CLIPBOARD_CLEAR_MS)
    } catch {
      /* clipboard unavailable */
    }
  }
  function close() {
    setSecret(null)
    onClose()
  }
  return (
    <Sheet
      title={`${t('trading.sheet.export.title')} · ${walletLabel(wallet)}`}
      onClose={secret ? close : onClose}
      role="alertdialog"
      wide
      foot={
        secret ? (
          <>
            <Button onClick={close} data-testid="export-close">
              {t('trading.sheet.export.close')}
            </Button>
            <Button onClick={() => setSecret(null)}>{t('trading.sheet.export.hide')}</Button>
            <Button variant="primary" onClick={() => void copy()} data-testid="export-copy">
              {copied ? (
                <Check className="size-3.5" strokeWidth={2.5} aria-hidden />
              ) : (
                <Copy className="size-3.5" strokeWidth={1.75} aria-hidden />
              )}
              {t('trading.sheet.export.copy')}
            </Button>
          </>
        ) : (
          <>
            <Button onClick={onClose} disabled={m.isPending}>
              {t('trading.sheet.cancel')}
            </Button>
            <Button
              variant="danger"
              disabled={!pw || m.isPending}
              onClick={reveal}
              aria-busy={prompting || undefined}
              data-testid="export-reveal"
            >
              {prompting
                ? t('trading.touchId.prompting')
                : m.isPending
                  ? t('trading.sheet.working')
                  : t('trading.sheet.export.cta')}
            </Button>
          </>
        )
      }
    >
      <div className="trd-warn" role="alert">
        <TriangleAlert className="size-3.5" strokeWidth={2} aria-hidden />
        <span>{t('trading.sheet.export.warning')}</span>
      </div>
      {secret ? (
        <pre className="trd-secret" data-testid="export-secret">
          {secret}
        </pre>
      ) : (
        <>
          <div className="trd-field">
            <span className="trd-field__label">{t('trading.sheet.export.format')}</span>
            <div
              role="radiogroup"
              aria-label={t('trading.sheet.export.format')}
              className="mac-segmented self-start"
            >
              <button
                type="button"
                role="radio"
                aria-checked={format === 'keystore'}
                className="mac-segment"
                onClick={() => setFormat('keystore')}
              >
                {t('trading.sheet.export.format.keystore')}
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={format === 'privateKey'}
                className="mac-segment"
                onClick={() => setFormat('privateKey')}
              >
                {t('trading.sheet.export.format.privateKey')}
              </button>
            </div>
          </div>
          <Field id={ids.pw} label={t('trading.sheet.export.password')}>
            <input
              id={ids.pw}
              className="mac-input"
              type="password"
              autoComplete="current-password"
              autoFocus
              value={pw}
              onChange={(e) => setPw(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && pw) reveal()
              }}
            />
          </Field>
        </>
      )}
    </Sheet>
  )
}

/** How long a copied secret may sit in the clipboard before it is blanked. */
export const CLIPBOARD_CLEAR_MS = 60_000

/** Blank the clipboard only if it still holds `secret`; swallow every failure. */
export async function clearClipboardIf(secret: string): Promise<void> {
  try {
    const current = await navigator.clipboard.readText()
    if (current === secret) await navigator.clipboard.writeText('')
  } catch {
    /* clipboard unreadable: nothing to do */
  }
}

function RemoveSheet({ wallet, onClose }: { wallet: Wallet; onClose: () => void }) {
  const id = useId()
  const [pw, setPw] = useState('')
  const m = useWalletMutation()
  // Touch ID (Settings › Security) runs inside the mutation, before the password is sent.
  const prompting = useTouchIdPrompt((s) => s.key === `wallet:${wallet.address.toLowerCase()}`)
  // What the key still controls: the one fact that should give pause.
  const portfolio = usePortfolio(wallet.address, true)
  const worth = portfolio.data?.totals?.valueUsd ?? null
  const funded = worth !== null && worth >= 0.01
  return (
    <Sheet
      title={`${t('trading.sheet.remove.title')} · ${walletLabel(wallet)}`}
      onClose={onClose}
      role="alertdialog"
      foot={
        <>
          <Button onClick={onClose} disabled={m.isPending}>
            {t('trading.sheet.cancel')}
          </Button>
          <Button
            variant="danger"
            disabled={!pw || m.isPending}
            data-testid="remove-submit"
            onClick={() =>
              m.mutate(
                {
                  method: 'wallet.remove',
                  params: { address: wallet.address, password: pw },
                  label: wallet.label,
                },
                {
                  onSuccess: () => {
                    toast.success(t('trading.sheet.remove.done'))
                    onClose()
                  },
                  onError: (err) => {
                    // Declined: the gate has said why, and nothing was sent.
                    if (isTouchIdDeclined(err)) return
                    toast.error(`${t('trading.sheet.error')}: ${errorText(err)}`)
                  },
                },
              )
            }
            aria-busy={prompting || undefined}
          >
            {prompting
              ? t('trading.touchId.prompting')
              : m.isPending
                ? t('trading.sheet.working')
                : t('trading.sheet.remove.cta')}
          </Button>
        </>
      }
    >
      <div className="trd-warn" role="alert" data-tone={funded ? 'danger' : undefined}>
        <TriangleAlert className="size-3.5" strokeWidth={2} aria-hidden />
        <span>
          {t('trading.sheet.remove.body')}
          {funded ? ` ${t('trading.sheet.remove.funded')}` : ''}
        </span>
      </div>
      <p className="trd-mono text-[11.5px] text-muted-foreground">{wallet.address}</p>
      <p className="trd-remove__worth" data-testid="remove-worth" data-funded={funded || undefined}>
        <span>{t('trading.sheet.remove.worth')}</span>
        <b>
          <Money value={worth} />
        </b>
      </p>
      <Field id={id} label={t('trading.sheet.remove.password')}>
        <input
          id={id}
          className="mac-input"
          type="password"
          autoComplete="current-password"
          autoFocus
          value={pw}
          onChange={(e) => setPw(e.target.value)}
        />
      </Field>
    </Sheet>
  )
}
