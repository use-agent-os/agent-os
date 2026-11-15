---
name: poolsdotfun-token-launcher
description: "Launch a token on pools.fun (Robinhood Chain, 4663) through the PartyFactory, and manage the creator fees afterwards. Use when the user wants to create, launch, or deploy a memecoin or token on pools.fun, asks what a launch would cost or what price it would open at, wants to simulate or dry-run a launch, wants to mine a launch salt, wants to attach an image or logo to a token they are launching, or wants to collect/claim creator fees or change the fee recipient for a token they launched. NOT for: buying or selling existing tokens, Uniswap v3/v4 LP positions, or launches on any other chain or launchpad."
homepage: https://pools.fun/
triggers: [pools.fun, poolsdotfun, launch token, create token, memecoin launch, robinhood chain, party factory, dev buy, token logo]
provenance:
  origin: agentos-original
  license: MIT
  maintained_by: AgentOS
metadata:
  agentos:
    emoji: "🦩"
    category: crypto
    risk: high
    capabilities: [network-read, network-write, signing]
    requires:
      anyBins: ["python3", "python"]
      # Only genuinely required variables belong here: `requires.env` is a hard
      # eligibility gate (see skills/eligibility.py::_has_env), so anything listed
      # makes the skill unavailable while it is unset. PINATA_JWT is deliberately
      # NOT listed — it is optional, and a launch without a token image must work
      # on a machine where it was never configured.
      env:
        - name: POOLSFUN_PRIVATE_KEY
          description: Signing key for launches and fee claims. Read-only commands use it
            only to derive your wallet address, and cannot sign with it.
          secret: true
---

# pools.fun token launcher

Launches tokens through the pools.fun `PartyFactory` on Robinhood Chain
(`0x626C3d09B65bF5d1D40E0D5F25e19fa49783B3D4`), and manages creator fees on the
`PartyLocker` afterwards.

**Set this once per shell**, then every command below is copy-pasteable:

```bash
S="{baseDir}/scripts"
```

There is no RPC to configure. The chain is fixed (Robinhood Chain, 4663) and the
endpoint is built in.

**The signing key is not in your shell.** AgentOS holds `POOLSFUN_PRIVATE_KEY` back from
every command you run and passes it only to a **direct** run of `pools_read.py` or
`pools_write.py`: one command — `S="{baseDir}/scripts"; python3 "$S/pools_write.py" ...` (each
command starts a fresh shell, so keep the `S=` on the same line), or the full path spelled
out — with no pipe, redirect (`2>&1` is fine), `cd`, `VAR=` prefix or second command. AgentOS
runs its own packaged copy of the script. Any other shape runs without the key and stops with
"held back from the agent's shell"; rewrite it as one direct command.

**Environment.** `POOLSFUN_PRIVATE_KEY` is required to send anything; read
commands work without it if you pass `--from 0x…`. `PINATA_JWT` is **optional**
and needed only to attach a token image — set it in the agent environment (or
`~/.agentos/.env`) if you want `--image`. It is intentionally not declared as a
requirement, so the skill stays available when it is unset.

## What a launch actually does

One transaction, and all of it is irreversible:

1. CREATE2-deploys a fixed-supply ERC20 — **1,000,000,000 tokens**, no owner, no
   mint function, no transfer hooks.
2. Creates a SushiSwap V3 pool at **1% fee** and initialises it at the protocol's
   launch tick.
3. Mints the **entire supply** as one single-sided full-range position and sends
   the LP NFT **to the locker, permanently**. The launcher never holds the LP.
4. Optionally performs a **dev buy** — your own first purchase, executed as the
   pool's first swap inside the same transaction, so it cannot be front-run.

There is no launch fee. A launch costs gas (~6.1M) plus whatever you choose to
dev-buy.

## Three things you cannot choose

Say these plainly when a user asks for them, because the flags do not exist:

* **The opening price / market cap.** Every pools.fun token opens at the
  protocol's `initialFdvUsd` — currently **$10,000 FDV**. The factory reads its
  own Chainlink feed to derive the tick, and `expectedStartTick` in the ABI is a
  race guard, not an input: a mismatch reverts `StartTickChanged()`. Changing it
  requires `setInitialFdvUsd`, which is `onlyOwner` (the pools.fun team). A dev
  buy only moves the price **up**, after the open.
* **The supply, fee tier, or LP range.** All hard-coded in the factory.
* **Keeping the LP.** It goes to the locker on launch, always.

## Part A — Read (no key, no funds, nothing sent)

```bash
python3 "$S/pools_read.py" preflight              # can I launch, and at what price?
python3 "$S/pools_read.py" assets                 # the paired-asset allowlist
python3 "$S/pools_read.py" mine-salt --name "My Token" --symbol MYT --deployer 0xYou
python3 "$S/pools_read.py" simulate --name "My Token" --symbol MYT --dev-buy 0.001 --from 0xYou
python3 "$S/pools_read.py" token 0xTokenAddress   # a launched token's pool, price, LP
python3 "$S/pools_read.py" fees  0xTokenAddress   # creator fee position
python3 "$S/pools_read.py" find-image             # locate a chat-attached logo
```

Start with `preflight`. It answers the three questions that decide whether a
launch can happen at all — is the factory paused, is the price feed live, and is
Pinata configured for an image — plus the exact FDV the launch will open at.

Every read command takes `--json`. All of them work with no environment
variables set, as long as you pass `--from`/`--deployer`.

## Part B — Launch

A launch needs **a name, a symbol, and optionally an image**. Everything else is
defaulted and every default is printed in the plan, labelled `[default]`.

```bash
python3 "$S/pools_write.py" launch --name "My Token" --symbol MYT --image ./logo.png
```

That prints a plan and sends nothing. To execute, re-run with the hash it
printed:

```bash
python3 "$S/pools_write.py" launch --name "My Token" --symbol MYT --image ./logo.png \
  --broadcast --confirm 0x3f9a2c11
```

Show the user the plan and get an explicit yes first. AgentOS then asks the user to
approve the `--broadcast` command itself, in its own approval prompt, before the key is
released; `/elevated`, `bypass` and auto-approve do not answer that prompt. On
`approval_pending`, wait for the user — never retry it in another form. The same holds for
every write below (`claim`, `set-fee-recipient`, ...).

**Never invent a PLAN_HASH.** It comes from the dry run and covers every field
that determines what lands on-chain. If the price feed moved between the two
steps the hash changes and the broadcast refuses — that is the mechanism working,
not a bug. Re-read the new plan and confirm that. The hash binds the broadcast to the
plan; it is not the approval — the user's answer to AgentOS's prompt is.

### The defaults

| Setting | Default | Override |
|---|---|---|
| paired asset | WETH | `--paired usdg` |
| dev buy | **0** | `--dev-buy 0.001` (ETH) or `--dev-buy-asset 5` (paired ERC20) |
| fee recipient | you | `--fee-recipient 0x…` |
| deadline | +1200s | `--deadline-secs 600` |
| slippage | 1% | `--slippage-bps 300` |
| salt | mined for you | `--salt 0x…` |

Identity flags: `--description`, `--website`, `--twitter`, `--metadata-uri`,
`--pin-metadata`.

### Token image and metadata

Three paths, picked automatically:

1. `--metadata-uri ipfs://…` — you already host it. Never touches Pinata.
2. `--image <path>` — pins the logo and the metadata JSON to IPFS.
   **Requires `PINATA_JWT`.** `<path>` is a real file on this machine — see
   "Getting the image" below, because a chat attachment usually is not one.
3. Neither — the metadata JSON is inlined as a `data:application/json;base64,…`
   URI. **No secret, no network.** This is the default and it works everywhere.

If `--image` is passed without `PINATA_JWT` the command **fails** rather than
launching without the picture. That is deliberate: the token's identity is
immutable the moment the transaction lands, and there is no second chance to
attach a logo.

### Getting the image when the user attached it in chat

**Read this before hunting for a path.** When a user attaches an image and says
"use this as the logo", you receive the *pixels* — an image content block. You
are never told a filename or a path, and anything under about 2 MB (most logos)
is never written to the filesystem at all.

The bytes are not lost, though: every attachment is recorded in the transcript
database. Run:

```bash
python3 "$S/pools_read.py" find-image
```

It reads the transcript, lists image attachments **newest message first** with
age and session, extracts the newest to a real file, and prints the
`launch --image …` line to use:

```
  WHEN         AGE  NAME        TYPE     SIZE  SOURCE  SESSION
  08-13 14:27   8m  image.png   png    210 KB  inline  twan0934
  08-10 07:42   3d  shot.png    png   3088 KB  staged  9ep5wkxp

  newest written to: /tmp/poolsfun-logos/1fac1c53dc6af1cd.png
```

**Always open the extracted file and confirm it is the right picture before
pinning it.** A token's logo is immutable once the transaction lands.

Two things that make this trustworthy, and which a directory listing cannot do:

* Ordering is by the **message's own timestamp**, so "newest" means "most
  recently sent", not "most recently written to disk".
* Each row carries its **session**. A candidate from another session, or one
  hours old, is flagged — it is almost certainly not what was just sent.

Do not fall back to scanning `~/.agentos/media` by hand. Blobs there are
content-addressed with no extension and no link back to a message; picking the
newest one has already produced a completely unrelated image.

**If `find-image` reports nothing**, in order of preference:

1. **Ask the user for the file path** on their machine. In the CLI they can run
   `! ls ~/Downloads/*.png`, or drag the file into the terminal. This is not a
   failure — it is the reliable route.
2. If the logo is already hosted, skip Pinata: `--metadata-uri ipfs://…` or an
   `https://…` URL pointing at the metadata JSON.
3. Launch with no logo. The metadata still inlines and the token still
   launches — but say so explicitly first, because **a logo cannot be added
   afterwards**.

Never fabricate a path, never pin an image you have not looked at, and never
silently launch without the image when the user asked for one.

## Part C — Creator fees

The locker holds the LP forever, but trading fees accrue to you.

```bash
python3 "$S/pools_read.py"  fees 0xToken              # what is there
python3 "$S/pools_write.py" collect-and-claim 0xToken # sweep + pay out
python3 "$S/pools_write.py" set-fee-recipient 0xToken --recipient 0xNew
```

`collect` sweeps fees from the LP position into the locker; `claim` pays out your
share of what the locker holds; `collect-and-claim` does both in one transaction
and is usually what you want. Each takes the same `--broadcast --confirm` gate.

## Error handling

| Symptom | Cause | Fix |
|---|---|---|
| `TokenNotToken0` | The salt no longer produces an address below the paired asset | Re-run without `--salt` to mine a fresh one |
| `StartTickChanged` | The ETH price feed updated between planning and sending | Re-run; confirm the new hash |
| `DeployFailed` | A token already exists at that salt's address | Change name/symbol/metadata, or use a different `--salt` |
| `CreatorNotCaller` | `creator` must equal the sending wallet | Drop `--fee-recipient` confusion; creator is always the signer |
| `AmbiguousDevBuy` | Both `--dev-buy` and `--dev-buy-asset` given | Pick one |
| `DevBuyWethOnly` | Native ETH dev buy on a USDG pair | Use `--dev-buy-asset` after `approve` |
| `DevBuyTooLittle` | Fill came in under the slippage floor | Raise `--slippage-bps` |
| `PairedAssetNotAllowed` | Asset is not on the allowlist | `pools_read.py assets` |
| `Paused` | The factory is paused | Wait; nothing else to do |
| `--broadcast needs --confirm` | Dry run not confirmed | Re-run with the printed hash |
| `plan changed since it was printed` | Feed moved, or a flag differs from the dry run | Re-read the plan, confirm the new hash |
| `PINATA_JWT is not set` | `--image` without Pinata | Set it, or drop `--image` |
| `image not found: …` | Guessed a path for a chat attachment | Run `find-image`; if empty, ask the user for the real path |
| `no qualifying salt in N attempts` | Unlucky search | Raise `--max-salt-attempts` |
| `env var POOLSFUN_PRIVATE_KEY is not set` | No signing key | Set it, or pass `--from 0x…` to plan only |

Set `POOLSFUN_DEBUG=1` for full tracebacks.

## Why the salt has to be mined

The factory mints single-sided liquidity and requires the launched token to be
`token0` of the pair, so the CREATE2 address must sort numerically **below** the
paired asset or `launch` reverts `TokenNotToken0()`. Against WETH
(`0x0Bd7…`) about **4.6%** of salts qualify, so the skill searches — batched
`computeTokenAddress` calls, usually resolved in one round trip.

A salt is bound to the exact `(deployer, name, symbol, metadataUri)`. Change any
of them and it must be re-mined. This is why `--metadata-uri` is resolved
*before* mining, not after.

## Verifying a change

```bash
python3 "$S/selftest.py"          # offline, no network, no key
python3 "$S/pools_read.py" preflight
python3 "$S/pools_write.py" launch --name "Test" --symbol TST   # plan only
```

The selftest pins the launch calldata against the real reference transaction
(`0x0978ee72…`), so an encoding regression fails there rather than on-chain.

See `assets/partyfactory-reference.md` for the full ABI, the decoded reference
launch, and the locker's fee-split mechanics.
