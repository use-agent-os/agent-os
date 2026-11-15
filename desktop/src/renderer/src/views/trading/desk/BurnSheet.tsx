import { Flame, Sparkles, TriangleAlert } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { isTouchIdDeclined } from '~/lib/biometric-gate'
import { useBalances, useSend } from '~/stores/trading'
import {
  chainName,
  compareAmounts,
  errorText,
  formatAmount,
  formatUsd,
  sameAddress,
  shortAddress,
  walletLabel,
} from '../logic'
import { Sheet } from '../parts'
import { CHAINS, type Balance, type Wallet } from '../types'
import {
  BURN_ADDRESS,
  composeBurnPrompt,
  validateBurn,
  type BurnError,
  type BurnForm,
  type BurnHolding,
} from './desk-logic'

const ARM_RESET_MS = 4000
/** Above this, the sheet stops calling it dust and says sell it instead. */
const WORTH_SELLING_USD = 1

function holdingOf(balance: Balance | null): BurnHolding | null {
  if (!balance) return null
  return {
    symbol: balance.token.symbol,
    decimals: balance.token.decimals,
    amount: balance.amount,
    native: balance.token.native,
    valueUsd: balance.valueUsd,
  }
}

/**
 * Burn a token: send it to an address with no key, on purpose, for good.
 *
 * The sheet is built around the two things that make a burn different from
 * a send. The recipient is not a field — it is pinned to the burn address
 * and cannot be typed over, so there is no paste to get wrong. And the
 * token is picked from what the wallet actually holds rather than typed,
 * because "which token" is the one thing nobody should be able to fat-finger
 * when the answer is unrecoverable. Hidden junk is in that list on purpose:
 * it is usually the whole reason someone opened this.
 *
 * Two gates before anything moves: the symbol typed back by hand, and then
 * the button arming. A third is the engine's — an agent-initiated send always
 * waits for the operator, and "Burn now" signs as the operator themself.
 *
 * Native assets are not burnable here at all. ETH sent to the burn address
 * is money destroyed with nothing in return, and the sheet says so rather
 * than making it a step you can click past.
 */
export function BurnSheet({
  wallets,
  primary,
  onClose,
  onAsk,
}: {
  wallets: readonly Wallet[]
  primary: string | null
  onClose: () => void
  /** Post the composed prompt into the desk chat. */
  onAsk: (prompt: string) => void
}) {
  const [form, setForm] = useState<BurnForm>({
    chainId: 8453,
    wallet: primary,
    token: '',
    amount: '',
    confirm: '',
    note: '',
  })
  const [touched, setTouched] = useState(false)
  const [armed, setArmed] = useState(false)
  const send = useSend()

  useEffect(() => {
    if (!armed) return
    const id = window.setTimeout(() => setArmed(false), ARM_RESET_MS)
    return () => window.clearTimeout(id)
  }, [armed])

  const walletRow =
    wallets.find((w) => sameAddress(w.address, form.wallet)) ??
    wallets.find((w) => w.primary) ??
    wallets[0] ??
    null

  // The junk is the point: this is the one balance read in the desk that
  // asks for hidden rows too.
  const { balances, isPending } = useBalances(walletRow?.address, Boolean(walletRow), true)
  const options = useMemo(
    () =>
      balances
        .filter((b) => b.chainId === form.chainId && !b.token.native)
        .sort((a, b) => (b.valueUsd ?? 0) - (a.valueUsd ?? 0)),
    [balances, form.chainId],
  )
  const picked = useMemo(
    () =>
      options.find((b) => b.token.address.toLowerCase() === form.token.trim().toLowerCase()) ??
      null,
    [options, form.token],
  )
  const held = holdingOf(picked)

  // Changing chain or wallet invalidates a token picked on the old one.
  const patch = (p: Partial<BurnForm>) => {
    setTouched(true)
    setForm((f) => {
      const next = { ...f, ...p }
      if (p.chainId !== undefined || p.wallet !== undefined)
        return { ...next, token: '', amount: '', confirm: '' }
      // A different token means a different symbol to type back.
      if (p.token !== undefined) return { ...next, amount: '', confirm: '' }
      return next
    })
  }

  const check = validateBurn(form, held)
  const prompt = useMemo(() => composeBurnPrompt(form, { wallets, held }), [form, wallets, held])
  const valuable = held?.valueUsd != null && held.valueUsd >= WORTH_SELLING_USD
  const burningAll =
    held !== null && form.amount.trim()
      ? compareAmounts(form.amount.trim(), held.amount, held.decimals) === 0
      : false

  function ask() {
    setTouched(true)
    if (!check.ok) return
    onAsk(prompt)
  }

  function burnNow() {
    setTouched(true)
    if (!check.ok || !held) return
    if (!armed) {
      setArmed(true)
      return
    }
    setArmed(false)
    send.mutate(
      {
        chainId: form.chainId,
        ...(walletRow ? { wallet: walletRow.address } : {}),
        token: form.token.trim(),
        recipients: [{ to: BURN_ADDRESS, amount: form.amount.trim() }],
        note: form.note.trim() || `burn ${form.amount.trim()} ${held.symbol}`,
        // Names the burn in the Touch ID sheet (Settings › Security); never sent.
        touchId: {
          what: `${form.amount.trim()} ${held.symbol} → ${shortAddress(BURN_ADDRESS)}`,
          valueUsd: burningAll ? held.valueUsd : null,
        },
      },
      {
        onSuccess: (res) => {
          const failed = res.orders.find((o) => o.status === 'failed')
          if (failed) {
            // A scam token that blocks transfers is the common case, not a
            // desk fault: the toast points at the tool that always works.
            toast.error(`${t('trading.burn.failed')}: ${failed.reason ?? ''}`, {
              id: 'trd-burn',
              description: t('trading.burn.failed.body'),
            })
            return
          }
          toast.success(t('trading.burn.sent'), {
            id: 'trd-burn',
            description: t('trading.burn.sent.body'),
          })
          onClose()
        },
        onError: (err) => {
          // Declined: the gate has said why, and nothing was sent.
          if (isTouchIdDeclined(err)) return
          toast.error(`${t('trading.burn.failed')}: ${errorText(err)}`, { id: 'trd-burn' })
        },
      },
    )
  }

  const error: BurnError | undefined = touched && !check.ok ? check.error : undefined
  const pending = send.isPending

  return (
    <Sheet
      title={t('trading.tool.burn.name')}
      onClose={pending ? () => {} : onClose}
      wide
      note={
        <span className="trd-send__warn">
          <TriangleAlert className="size-3" strokeWidth={2} aria-hidden />
          {t('trading.burn.irreversible')}
        </span>
      }
      foot={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            {t('trading.sheet.cancel')}
          </Button>
          <Button
            variant="secondary"
            disabled={pending}
            onClick={burnNow}
            data-armed={armed || undefined}
            title={t('trading.burn.now.help')}
            data-testid="burn-now"
          >
            <Flame className="size-3.5" strokeWidth={2} aria-hidden />
            {pending
              ? t('trading.burn.burning')
              : armed
                ? t('trading.burn.now.confirm')
                : t('trading.burn.now')}
          </Button>
          <Button
            variant="primary"
            disabled={pending}
            onClick={ask}
            title={t('trading.burn.ask.help')}
            data-testid="burn-ask"
          >
            <Sparkles className="size-3.5" strokeWidth={2} aria-hidden />
            {t('trading.burn.ask')}
          </Button>
        </>
      }
    >
      <div className="trd-burn" data-testid="burn-sheet">
        <p className="trd-burn__lead">{t('trading.burn.lead')}</p>

        <div className="trd-send__row">
          <label className="trd-send__field">
            <span>{t('trading.send.chain')}</span>
            <select
              className="mac-input"
              value={form.chainId}
              onChange={(e) => patch({ chainId: Number(e.target.value) })}
              data-testid="burn-chain"
            >
              {CHAINS.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          <label className="trd-send__field">
            <span>{t('trading.send.wallet')}</span>
            <select
              className="mac-input"
              value={walletRow?.address ?? ''}
              onChange={(e) => patch({ wallet: e.target.value })}
              data-testid="burn-wallet"
            >
              {wallets.map((w) => (
                <option key={w.address} value={w.address}>
                  {walletLabel(w)}
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* Picked, never typed: the token is the one thing that must not be
            a paste when the result cannot be undone. */}
        <label className="trd-send__field" data-error={error === 'token' || undefined}>
          <span>{t('trading.burn.token')}</span>
          <select
            className="mac-input"
            value={form.token}
            onChange={(e) => patch({ token: e.target.value })}
            data-testid="burn-token"
          >
            <option value="">
              {isPending && !balances.length
                ? t('trading.tools.reading')
                : options.length
                  ? t('trading.burn.token.placeholder')
                  : t('trading.burn.token.empty')}
            </option>
            {options.map((b) => (
              <option key={`${b.chainId}:${b.token.address}`} value={b.token.address}>
                {b.token.symbol} · {formatAmount(b.amount)}
                {b.valueUsd != null ? ` · ${formatUsd(b.valueUsd)}` : ''}
                {b.hidden ? ` · ${t('trading.burn.token.junk')}` : ''}
              </option>
            ))}
          </select>
          {picked ? (
            <small className="trd-mono" data-testid="burn-token-address">
              {shortAddress(picked.token.address, 10, 8)}
              {picked.token.verified ? '' : ` · ${t('trading.burn.token.unverified')}`}
            </small>
          ) : null}
        </label>

        {/* The talk-them-out-of-it line. It is not a warning buried in prose:
            when the thing has value, selling it is the headline. */}
        {valuable ? (
          <p className="trd-burn__valuable" role="note" data-testid="burn-valuable">
            <TriangleAlert className="size-3.5" strokeWidth={2} aria-hidden />
            <span>
              {t('trading.burn.valuable').replace('{value}', formatUsd(held?.valueUsd ?? 0))}
            </span>
          </p>
        ) : null}

        <div className="trd-send__row">
          <label
            className="trd-send__field"
            data-error={error === 'amount' || error === 'balance' || undefined}
          >
            <span>{t('trading.burn.amount')}</span>
            <input
              className="mac-input trd-num"
              inputMode="decimal"
              value={form.amount}
              placeholder="0"
              disabled={!held}
              onChange={(e) => setForm((f) => ({ ...f, amount: e.target.value }))}
              onBlur={() => setTouched(true)}
              data-testid="burn-amount"
            />
            {held ? (
              <small>
                {t('trading.send.balance')}: {formatAmount(held.amount)} {held.symbol}
                {' · '}
                <button
                  type="button"
                  className="trd-burn__all app-no-drag"
                  onClick={() => setForm((f) => ({ ...f, amount: held.amount }))}
                  data-testid="burn-all"
                >
                  {t('trading.burn.all')}
                </button>
              </small>
            ) : null}
          </label>
          <label className="trd-send__field trd-send__field--wide">
            <span>{t('trading.send.note')}</span>
            <input
              className="mac-input"
              value={form.note}
              placeholder={t('trading.burn.note.placeholder')}
              onChange={(e) => setForm((f) => ({ ...f, note: e.target.value }))}
              data-testid="burn-note"
            />
          </label>
        </div>

        {/* Where it goes, stated as a fact rather than offered as a field. */}
        <p className="trd-burn__to" data-testid="burn-to">
          <span>{t('trading.burn.to')}</span>
          <b className="trd-mono">{BURN_ADDRESS}</b>
          <em>{t('trading.burn.to.help')}</em>
        </p>

        {/* Gate one. Only asked once a token and an amount are in place, so
            it never reads as busywork before the real decision. */}
        <label
          className="trd-send__field"
          data-error={error === 'confirm' || undefined}
          data-testid="burn-confirm-field"
        >
          <span>
            {held
              ? t('trading.burn.confirm').replace('{symbol}', held.symbol)
              : t('trading.burn.confirm.idle')}
          </span>
          <input
            className="mac-input trd-mono"
            value={form.confirm}
            placeholder={held?.symbol ?? ''}
            disabled={!held || !form.amount.trim()}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setForm((f) => ({ ...f, confirm: e.target.value }))}
            onBlur={() => setTouched(true)}
            data-testid="burn-confirm"
          />
        </label>

        {error ? (
          <p className="trd-send__error" role="alert" data-testid="burn-error">
            {t(`trading.burn.error.${error}`)}
          </p>
        ) : null}

        {armed && check.ok && held ? (
          <section className="trd-send__review" data-testid="burn-review" aria-live="polite">
            <h4>{t('trading.burn.review.title')}</h4>
            <dl>
              <div>
                <dt>{t('trading.send.chain')}</dt>
                <dd>{chainName(form.chainId)}</dd>
              </div>
              <div>
                <dt>{t('trading.send.wallet')}</dt>
                <dd>
                  {walletRow ? walletLabel(walletRow) : '—'}
                  {walletRow ? (
                    <span className="trd-mono"> · {shortAddress(walletRow.address)}</span>
                  ) : null}
                </dd>
              </div>
              <div>
                <dt>{t('trading.burn.amount')}</dt>
                <dd>
                  {formatAmount(form.amount.trim())} {held.symbol}
                  {burningAll ? ` · ${t('trading.burn.review.all')}` : ''}
                </dd>
              </div>
            </dl>
            <p>
              <span>{t('trading.burn.review.gone')}</span>
              <b className="trd-num">
                {held.valueUsd != null
                  ? formatUsd(held.valueUsd)
                  : t('trading.burn.review.unpriced')}
              </b>
            </p>
          </section>
        ) : null}
      </div>
    </Sheet>
  )
}
