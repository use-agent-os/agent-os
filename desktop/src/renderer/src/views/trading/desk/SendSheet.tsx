import { ChevronDown, SendHorizontal, Sparkles, TriangleAlert } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { isTouchIdDeclined } from '~/lib/biometric-gate'
import { useBalances, useSend, type SendRecipient } from '~/stores/trading'
import {
  checksumMismatch,
  chainName,
  compareAmounts,
  errorText,
  formatAmount,
  formatUsd,
  fromRaw,
  parseAmount,
  sameAddress,
  shortAddress,
  toRaw,
  walletLabel,
} from '../logic'
import { Sheet } from '../parts'
import { CHAINS, type Balance, type Wallet } from '../types'
import {
  composeSendPrompt,
  parseRecipientLines,
  validateSend,
  type SendError,
  type SendForm,
  type SendRecipientForm,
} from './desk-logic'

const ARM_RESET_MS = 4000

function linesOf(rows: readonly SendRecipientForm[]): string {
  return rows.map((r) => (r.amount ? `${r.address}=${r.amount}` : r.address)).join('\n')
}

/** The typed token against what the wallet holds on this chain: symbol or address. */
function heldToken(balances: readonly Balance[], chainId: number, token: string): Balance | null {
  const q = token.trim().toLowerCase()
  if (!q) return null
  return (
    balances.find(
      (b) =>
        b.chainId === chainId &&
        (b.token.symbol.toLowerCase() === q || b.token.address.toLowerCase() === q),
    ) ?? null
  )
}

/** Every leg's amount in the token, summed; null while a leg is priced in USD. */
function totalAmount(form: SendForm, decimals: number): string | null {
  let sum = 0n
  for (const r of form.recipients) {
    if (!r.address.trim()) continue
    const own = parseAmount(r.amount)
    const shared = parseAmount(form.amount)
    const leg = own ?? shared
    if (leg === null) return null
    sum += toRaw(leg, decimals)
  }
  return fromRaw(sum, decimals)
}

/**
 * Send one token to one or many addresses. Two ways out, on purpose:
 * "Ask the desk" posts an exact prompt into the chat, the agent runs one
 * `agentos trade send`, and the batch comes back here as a card for your
 * approval — the path that leaves a record of the reasoning. "Send now"
 * signs and broadcasts as you, immediately, and arms first because a send
 * cannot be undone.
 */
export function SendSheet({
  wallets,
  primary,
  multi = false,
  onClose,
  onAsk,
}: {
  wallets: readonly Wallet[]
  primary: string | null
  /** Opened as "Multisend": the recipients box leads and says so. */
  multi?: boolean
  onClose: () => void
  /** Post the composed prompt into the desk chat. */
  onAsk: (prompt: string) => void
}) {
  const [form, setForm] = useState<SendForm>({
    chainId: 8453,
    wallet: primary,
    token: '',
    recipients: [],
    amount: '',
    usd: '',
    note: '',
  })
  const [text, setText] = useState('')
  const [touched, setTouched] = useState(false)
  const [showPrompt, setShowPrompt] = useState(false)
  const [armed, setArmed] = useState(false)
  const send = useSend()

  useEffect(() => {
    if (!armed) return
    const id = window.setTimeout(() => setArmed(false), ARM_RESET_MS)
    return () => window.clearTimeout(id)
  }, [armed])

  const patch = (p: Partial<SendForm>) => {
    setTouched(true)
    setForm((f) => ({ ...f, ...p }))
  }
  const onRecipients = (value: string) => {
    setText(value)
    patch({ recipients: parseRecipientLines(value) })
  }
  const baseCheck = validateSend(form)
  // A mixed-case address whose casing is wrong is a mangled paste, not a
  // typo the engine can catch: it is refused here, before either button.
  const badChecksum = form.recipients.some((r) => checksumMismatch(r.address))
  const check: { ok: boolean; error?: SendError | 'checksum' } =
    baseCheck.ok && badChecksum ? { ok: false, error: 'checksum' } : baseCheck
  const prompt = useMemo(() => composeSendPrompt(form, { wallets }), [form, wallets])
  const legs = form.recipients.filter((r) => r.address.trim())
  const count = legs.length
  const walletRow =
    wallets.find((w) => sameAddress(w.address, form.wallet)) ??
    wallets.find((w) => w.primary) ??
    wallets[0] ??
    null

  // What the paying wallet holds of the typed token, for a balance line and
  // an "insufficient" hint before the engine has to say so.
  const { balances } = useBalances(walletRow?.address, Boolean(walletRow))
  const held = heldToken(balances, form.chainId, form.token)
  const total = held ? totalAmount(form, held.token.decimals) : null
  const insufficient =
    held !== null && total !== null && compareAmounts(total, held.amount, held.token.decimals) > 0
  const totalUsd =
    form.usd.trim() && Number.isFinite(Number(form.usd))
      ? Number(form.usd) * count
      : total !== null && held?.priceUsd != null
        ? Number(total) * held.priceUsd
        : null

  function ask() {
    setTouched(true)
    if (!check.ok) return
    onAsk(prompt)
  }

  function sendNow() {
    setTouched(true)
    if (!check.ok) return
    if (!armed) {
      setArmed(true)
      return
    }
    setArmed(false)
    const recipients: SendRecipient[] = legs.map((r) => {
      const own = r.amount.trim()
      if (own) return { to: r.address.trim(), amount: own }
      if (form.amount.trim()) return { to: r.address.trim(), amount: form.amount.trim() }
      return { to: r.address.trim(), amountUsd: Number(form.usd) }
    })
    send.mutate(
      {
        chainId: form.chainId,
        ...(walletRow ? { wallet: walletRow.address } : {}),
        token: form.token.trim(),
        recipients,
        ...(form.note.trim() ? { note: form.note.trim() } : {}),
        // Names the send in the Touch ID sheet (Settings › Security); never sent.
        touchId: {
          what: [
            total !== null
              ? `${total} ${held?.token.symbol ?? ''}`
              : `$${form.usd.trim()} ×${count}`,
            '→',
            count === 1 && legs[0]
              ? shortAddress(legs[0].address.trim())
              : `${count} ${t('trading.send.count')}`,
          ].join(' '),
          valueUsd: totalUsd,
        },
      },
      {
        onSuccess: (res) => {
          const failed = res.orders.filter((o) => o.status === 'failed')
          if (failed.length === res.orders.length && failed[0]) {
            toast.error(`${t('trading.send.failed')}: ${failed[0].reason ?? ''}`, {
              id: 'trd-send',
            })
            return
          }
          if (failed.length > 0) {
            // Some legs went, some did not: the sheet stays so the list can
            // be corrected, and the toast says how many to look for in Orders.
            toast.warning(
              `${failed.length} ${t('trading.send.partial')} ${res.orders.length} ${t('trading.send.partial.failed')}`,
              { id: 'trd-send', description: failed[0]?.reason ?? undefined },
            )
            return
          }
          toast.success(t('trading.send.sent'), {
            id: 'trd-send',
            description: t('trading.send.sent.body'),
          })
          onClose()
        },
        onError: (err) => {
          // Declined: the gate has said why, and nothing was sent.
          if (isTouchIdDeclined(err)) return
          toast.error(`${t('trading.send.failed')}: ${errorText(err)}`, { id: 'trd-send' })
        },
      },
    )
  }

  const error = touched && !check.ok ? check.error : undefined
  const pending = send.isPending

  return (
    <Sheet
      title={multi ? t('trading.tool.multisend.name') : t('trading.send.title')}
      // A send in flight cannot be abandoned: Escape, the backdrop and the X
      // all wait for the answer.
      onClose={pending ? () => {} : onClose}
      wide
      note={
        <span className="trd-send__warn">
          <TriangleAlert className="size-3" strokeWidth={2} aria-hidden />
          {t('trading.send.irreversible')}
        </span>
      }
      foot={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            {t('trading.sheet.cancel')}
          </Button>
          <Button
            variant="secondary"
            disabled={send.isPending}
            onClick={sendNow}
            data-armed={armed || undefined}
            title={t('trading.send.now.help')}
            data-testid="send-now"
          >
            <SendHorizontal className="size-3.5" strokeWidth={2} aria-hidden />
            {send.isPending
              ? t('trading.send.sending')
              : armed
                ? t('trading.send.now.confirm')
                : t('trading.send.now')}
          </Button>
          <Button
            variant="primary"
            disabled={send.isPending}
            onClick={ask}
            title={t('trading.send.ask.help')}
            data-testid="send-ask"
          >
            <Sparkles className="size-3.5" strokeWidth={2} aria-hidden />
            {t('trading.send.ask')}
          </Button>
        </>
      }
    >
      <div className="trd-send" data-testid="send-sheet">
        <div className="trd-send__row">
          <label className="trd-send__field">
            <span>{t('trading.send.chain')}</span>
            <select
              className="mac-input"
              value={form.chainId}
              onChange={(e) => patch({ chainId: Number(e.target.value) })}
              data-testid="send-chain"
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
              data-testid="send-wallet"
            >
              {wallets.map((w) => (
                <option key={w.address} value={w.address}>
                  {walletLabel(w)}
                </option>
              ))}
            </select>
          </label>
          <label
            className="trd-send__field"
            data-error={error === 'token' || insufficient || undefined}
          >
            <span>{t('trading.send.token')}</span>
            <input
              className="mac-input"
              value={form.token}
              placeholder={t('trading.send.token.placeholder')}
              onChange={(e) => patch({ token: e.target.value })}
              data-testid="send-token"
            />
            {held ? (
              <small data-testid="send-balance" data-tone={insufficient ? 'danger' : undefined}>
                {t('trading.send.balance')}: {formatAmount(held.amount)} {held.token.symbol}
                {insufficient ? ` · ${t('trading.send.insufficient')}` : ''}
              </small>
            ) : null}
          </label>
        </div>

        <label
          className="trd-send__field"
          data-error={
            error === 'recipients' ||
            error === 'address' ||
            error === 'duplicate' ||
            error === 'checksum' ||
            undefined
          }
        >
          <span>
            {t('trading.send.recipients')}
            {count ? (
              <em className="trd-send__count">
                {count} {t('trading.send.count')}
              </em>
            ) : null}
          </span>
          <textarea
            className="mac-input trd-send__list trd-mono"
            rows={Math.min(12, Math.max(multi ? 6 : 3, count + 1))}
            autoFocus={multi}
            value={text}
            placeholder={t('trading.send.recipients.placeholder')}
            onChange={(e) => onRecipients(e.target.value)}
            onBlur={() => {
              // Normalise what was pasted so the preview and the send agree.
              if (form.recipients.length) setText(linesOf(form.recipients))
            }}
            spellCheck={false}
            data-testid="send-recipients"
          />
          <small>{t('trading.send.recipients.help')}</small>
        </label>

        <div className="trd-send__row">
          <label className="trd-send__field" data-error={error === 'amount' || undefined}>
            <span>{t('trading.send.amount')}</span>
            <input
              className="mac-input trd-num"
              inputMode="decimal"
              value={form.amount}
              placeholder="0"
              onChange={(e) => patch({ amount: e.target.value, usd: '' })}
              data-testid="send-amount"
            />
          </label>
          <label className="trd-send__field" data-error={error === 'amount' || undefined}>
            <span>{t('trading.send.usd')}</span>
            <input
              className="mac-input trd-num"
              inputMode="decimal"
              value={form.usd}
              placeholder="0"
              onChange={(e) => patch({ usd: e.target.value, amount: '' })}
              data-testid="send-usd"
            />
          </label>
          <label className="trd-send__field trd-send__field--wide">
            <span>{t('trading.send.note')}</span>
            <input
              className="mac-input"
              value={form.note}
              placeholder={t('trading.send.note.placeholder')}
              onChange={(e) => patch({ note: e.target.value })}
              data-testid="send-note"
            />
          </label>
        </div>

        {error ? (
          <p className="trd-send__error" role="alert" data-testid="send-error">
            {t(`trading.send.error.${error}`)}
          </p>
        ) : null}

        {/* Armed: the second click is a confirmation, so what it confirms is
            written out in full — every address, every amount, the total. */}
        {armed && check.ok ? (
          <section className="trd-send__review" data-testid="send-review" aria-live="polite">
            <h4>{t('trading.send.review.title')}</h4>
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
                <dt>{t('trading.send.token')}</dt>
                <dd>{held ? held.token.symbol : form.token.trim()}</dd>
              </div>
            </dl>
            <ul className="trd-mono">
              {legs.map((r) => {
                const amount = r.amount.trim() || form.amount.trim()
                return (
                  <li key={r.address}>
                    <span>{r.address.trim()}</span>
                    <b>
                      {amount
                        ? `${formatAmount(amount)} ${held ? held.token.symbol : ''}`
                        : `${formatUsd(Number(form.usd))} ${t('trading.send.review.each')}`}
                    </b>
                  </li>
                )
              })}
            </ul>
            <p>
              <span>{t('trading.send.review.total')}</span>
              <b className="trd-num">
                {total !== null ? `${formatAmount(total)} ${held ? held.token.symbol : ''}` : ''}
                {totalUsd !== null
                  ? `${total !== null ? ' · ' : ''}${formatUsd(totalUsd)}`
                  : total === null
                    ? '—'
                    : ''}
              </b>
            </p>
          </section>
        ) : null}

        <button
          type="button"
          className="trd-send__toggle app-no-drag"
          onClick={() => setShowPrompt((v) => !v)}
          aria-expanded={showPrompt}
          data-testid="send-preview-toggle"
        >
          <ChevronDown
            className="size-3"
            strokeWidth={2}
            aria-hidden
            style={{ rotate: showPrompt ? '180deg' : '0deg' }}
          />
          {t('trading.send.preview')}
        </button>
        {showPrompt ? (
          <pre className="trd-send__prompt trd-mono" data-testid="send-preview">
            {prompt}
          </pre>
        ) : null}
      </div>
    </Sheet>
  )
}
