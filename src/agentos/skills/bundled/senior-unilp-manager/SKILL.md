---
name: senior-unilp-manager
description: "Query and manage Uniswap V4 liquidity on Base (8453) and Robinhood Chain (4663). Use when the user asks how much liquidity a token has, wants a pool's reserves or market-cap bands, asks who launched a token or whether its LP is locked, wants to inspect an LP position or a wallet's positions, or wants to mint, increase, decrease, collect fees from, or burn a V4 position, or wants to create (initialize) a new hook-less V4 pool. NOT for: Uniswap v2/v3 positions, token swaps, or price quotes."
homepage: https://docs.uniswap.org/contracts/v4/overview
triggers: [uniswap v4, liquidity position, "check LP", pool id, collect fees, robinhood chain]
provenance:
  origin: agentos-original
  license: MIT
  maintained_by: AgentOS
metadata:
  agentos:
    emoji: "🦄"
    category: crypto
    risk: high
    capabilities: [network-read, network-write, signing]
    requires:
      anyBins: ["python3", "python"]
      env:
        - name: RPC_BASE_URL
          description: JSON-RPC endpoint for Base mainnet (chain id 8453).
          url: https://docs.base.org/chain/network-information
          secret: false
        - name: RPC_ROBINHOOD_URL
          description: JSON-RPC endpoint for Robinhood Chain (chain id 4663).
          url: https://robinhood.com
          secret: false
        - name: UNIV4_LP_PRIVATE_KEY
          description: Signing key for LP writes. Read-only commands use it only to derive
            your own wallet address, and cannot sign with it.
          secret: true
---

# Senior UniLP Manager — Uniswap V4 pools and positions

Three scripts, Python 3 stdlib only, no install step.

**`lp_read.py` can never sign**; every attended write goes through
`lp_write.py`, which is a dry run unless it is given both `--broadcast` and the
`--confirm <PLAN_HASH>` printed by that same dry run. `ratchet.py` is the one unattended
path — see Part C — and it is separate precisely so that boundary is visible.

That is structural, not a convention: `lp_read.py` touches `UNIV4_LP_PRIVATE_KEY` in one
place only — deriving "which wallet is mine" for `positions` without `--owner` — and it
imports `unilp/account.py`, which has address derivation and no `sign_digest`. Signing lives
in `unilp/secp256k1.py`, which imports *that*. The dependency runs one way, so no argument to
`lp_read.py` can reach a signature.

**Part A (read) needs no confirmation. Part B (write) must never broadcast until the user has
seen the parameter table and explicitly said yes. Part C broadcasts on a schedule, under a
mandate the user approved once, by hash.**

```bash
# Set this once per shell, and keep the quotes — the path can contain spaces.
S="{baseDir}/scripts"

python3 "$S"/lp_read.py  <command> [flags]   # default chain: robinhood
python3 "$S"/lp_write.py <command> [flags]   # add --chain base for Base
python3 "$S"/ratchet.py  <command> [flags]   # unattended take-profit, Part C
```

Every command below is written `python3 "$S"/<script>`. Use that form verbatim — a literal
`<S>` is not a variable, and in a shell it reads as a redirect from a file named `S`.

**The signing key is not in your shell.** AgentOS holds `UNIV4_LP_PRIVATE_KEY` back from
every command you run and passes it only to a **direct** run of one of these three scripts:
one command — `S="{baseDir}/scripts"; python3 "$S"/lp_write.py ...`, or the same with the
full path spelled out — with no pipe, redirect (`2>&1` is fine), `cd`, `VAR=` prefix, `env=`
override or second command. AgentOS runs its own packaged copy of the script for that
command. Any other shape runs without the key and stops with "held back from the agent's
shell"; rewrite it as one direct command — never look for the key elsewhere.

Chain endpoints come from `RPC_ROBINHOOD_URL` and `RPC_BASE_URL`; `--rpc <url>` overrides.
Both must be `http://` or `https://`; any other scheme is refused, because the
JSON-RPC transport would otherwise read local files through `file://`.

---

# Part A — Read

## 1. How much liquidity does a token have?

```bash
python3 "$S"/lp_read.py pools --token <addr> [--include-v3]
python3 "$S"/lp_read.py pools --token <addr> --chain base
```

Finds the token's v4 pools, computes exact reserves, prices them, and prints the market-cap
band of every liquidity range. `--include-v3` also probes the Uniswap **v3** factory across
fee tiers 100/500/3000/10000. Dust pools collapse into a one-line count (`--all-pools` shows
them); `--json` for machine-readable output.

Read the **`self-check`** line under the range table: it compares liquidity summed over
ranges straddling the current tick against `StateView.getLiquidity`. If it does not say `OK`,
the numbers are wrong — say so rather than reporting them.

The last block is **`recommended pool`** — the deepest by TVL, with its id in full, whether
it needs `--allow-hooked`, and the next command. A launched token routinely has dozens of
pools of which one has real depth and the rest are dust at punitive fee tiers, so take the
recommendation unless the user asked for a specific pool.

**Discovery goes through the launchpad registries, not logs — on both chains.** Neither RPC
serves a wide `eth_getLogs` range (Base caps hard; the Robinhood endpoint refuses anything
over 100k blocks, and the v4 contracts sit at block ~9k, so a full scan is ~660 requests). On
Robinhood, Doppler publishes no registry, so the command derives the pool from the **hook the
skill already knows** (`0x4e34…a544`, the AGENTOS launch hook) and confirms it with one
`getSlot0`. If nothing claims the token it probes for **hook-less pools** (see below) before
giving up; only if that finds nothing does it exit 2 rather than hanging. Use `pool --id` with
the PoolKey, or `--scan-logs` to force the chunked scan (minutes, not seconds).

### Finding pools with **no hook**

```bash
python3 "$S"/lp_read.py pools --token <addr> --no-hook
python3 "$S"/lp_read.py pools --token <addr> --no-hook --quote <addr> --chain base
```

A hook-less pool has no registry to look it up in — but with `hooks` pinned to the zero
address only `fee` and `tickSpacing` are free, so its poolId is derived directly at the
conventional tiers (0.01%/1, 0.05%/10, 0.30%/60, 1.00%/200) and confirmed with **one
multicall**. No log scan, same speed on every chain.

This is the fastest way to answer "does a plain, hook-free pool exist for this token?" — the
question to ask before minting, since a hooked pool is refused by default (§8). It is a fast
probe, not an exhaustive index: it covers only **known quote currencies** at **conventional
tiers**, so a negative result means "not found at the usual shapes", not "does not exist".
`--quote <addr>` probes a different pairing currency; drop `--no-hook` for full discovery.

## 2. One pool in depth

```bash
python3 "$S"/lp_read.py pool --id <poolId> [--ranges 10]
python3 "$S"/lp_read.py pool --id <poolId> --token <addr> --chain base
```

Prints the full PoolKey, dynamic-fee status, decoded hook permissions (labelled with the
launchpad when known), a `poolId recompute` line that must say `OK`, and every liquidity
range with its market-cap band and owner.

**On Base the PoolKey must be recoverable some other way.** A poolId is a keccak hash and
cannot be inverted, so it normally comes from the pool's `Initialize` log — which Base will
not serve. Three routes, cheapest first:

| flag | what it does | cost |
|---|---|---|
| *(nothing)* | a pool `pools`, `pool` or `ticks` has already confirmed is remembered in `state/unilp/pools/<chain>.json` and found by id | free |
| `--currency0 --currency1 --fee --tick-spacing [--hooks]` | give the PoolKey outright — works for **any** pool on any chain | free |
| `--token <addr>` | derive it from the launchpad registry, or from the hook-less fee tiers | 1 multicall |
| `--scan-logs` | scan `Initialize` logs anyway | very slow on Base |

The explicit PoolKey is checked by recomputing the poolId and requiring it to match `--id`, so
a typo errors out instead of silently addressing the wrong pool. `--hooks` is optional and
**defaults to the zero address** — spelling out a PoolKey is the general escape hatch for a
hook-less pool that discovery cannot reach. Pass `--fee 0x800000` for a dynamic-fee pool (hex
is accepted); note that is the PoolKey fee, not the live `lpFee` from slot0.

| `--mode` | `logs` | `ticks` (default on both chains) |
|---|---|---|
| source | replays every `ModifyLiquidity` event | walks `StateView.getTickBitmap` |
| rows are | individual LP positions, **with owner** | merged segments, no owner |
| cost | Base: thousands of requests — unusable; Robinhood: ~660 chunks, ~4 min | ~36 calls, ~150 ms |

Totals are exact either way — cross-checked on the AGENTOS pool: identical `amount0`, within
1 wei on `amount1`. If a `ticks` read had to be truncated it prints which bitmap words were
skipped; do not report those totals as complete.

## 3. A single position

```bash
python3 "$S"/lp_read.py position --token-id <id>
```

Ticks, the market-cap band the position was added across, principal at the current price,
in/out of range, uncollected fees, USD values. The owner line names known contracts and flags
**LP is LOCKED** when it is a launchpad locker. Works on Base with no log scan.

## 4. A wallet's positions

```bash
python3 "$S"/lp_read.py positions [--owner <address>] [--include-empty]
```

**Never ask the user for their own address.** Leave `--owner` off and it resolves the wallet
`UNIV4_LP_PRIVATE_KEY` derives; `--signer-env <VAR>` reads a different variable. Pass
`--owner` only when the question is about *someone else's* wallet — that path never touches
the key, so it works on a machine with no key configured at all.

If you need the address itself rather than the positions — to show the user which wallet is
in use, or to feed another command — `lp_write.py address` prints it. No chain, no plan,
nothing sent:

```bash
python3 "$S"/lp_write.py address [--json]
```

The v4 PositionManager has no enumerable index, so this scans inbound ERC-721 `Transfer` logs
and re-checks `ownerOf` — a tokenId that was received and later sold will not appear. Not
available on Base; use `position --token-id` or `launcher --token` there.

## 5. Who launched this token, and is the LP locked?

```bash
python3 "$S"/lp_read.py launcher --token <addr> --chain base
```

Supported on Base: **Clanker v4 / v4.1**, **Liquid Protocol**, **Bankr / Doppler**. Each
publishes an on-chain registry mapping token → hook, so the poolId is derived with one keccak
and confirmed with one `getSlot0` — no log scan, ~1–2 s.

- Clanker and Liquid lock the LP as PositionManager NFTs held by their locker. The command
  lists every locked tokenId; feed one to `position --token-id`.
- Doppler mints liquidity directly on the PoolManager, so there is **no NFT** — pool-level
  reads are the only view.
- Trading fees for all three accrue to a separate fee-locker contract, **not** to the
  position. This skill does not read or claim those.

## 6. Turn a market-cap target into ticks

```bash
python3 "$S"/lp_read.py ticks --pool <poolId> --mcap-lower 2000000 --mcap-upper 3000000
python3 "$S"/lp_read.py ticks --pool <poolId> --mcap-lower 156000 --mcap-upper 200000 --from-current
```

Snaps outward to `tickSpacing` and reports whether the range is single-sided. Feed the
printed `--tick-lower` / `--tick-upper` straight into `mint`.

The two `--mcap` flags may be given in either order — which one is the larger number
depends on whether the target sits above or below where the token trades now.

**`--from-current` when one end of the band is today's price.** Outward snapping would push
that end across the current tick, and the range comes back `two-sided: needs both
currencies` — not what "from this price to X" means. `--from-current` pulls the near edge
back to the correct side of the current tick, keeping the larger part of the band you asked
for, so the result is genuinely single-sided.

The `position type` line names the currency the range takes. **Read it rather than working
the direction out yourself** — it is the same rule `mint` enforces, so if they disagree the
mint is what is wrong (§8). USD columns need a price for the quote currency; if the indexer
is rate-limiting, the error says so and the fix is to wait, not to change pool (§ Error
handling).

## 6b. What is a token worth in USD?

```bash
python3 "$S"/lp_read.py price --tokens <addr>[,<addr>,...] [--chain base] [--json]
```

One indexer call for any number of tokens, and the same cache the other commands share.

**Use this rather than deriving a price from a pool** — and never compute one by hand from
two other prices. A pool with no depth still quotes a tick, so reading a price off a dust
pool returns a number that looks plausible and is not the market. That number then decides
the starting tick of a new pool (§8b) or the band of a mint, neither of which can be undone
cheaply. If the token is not indexed the command says so, which is the answer — ask the
user for the price instead of inferring one.

# Part B — Write

**IMPORTANT: do NOT broadcast any transaction until the user explicitly confirms.**

## Recipe — add a single-sided LP position

The common request: *"add N of my token as LP, from the current price to a market cap of
X"*. Run these five commands in order. Do not design your own sequence, and do not go
looking for a different pool between steps — every deviation below cost a real run ten
minutes of wandering.

```bash
# 1. Which pool? Take the one it prints as "recommended pool" — deepest TVL. This also
#    remembers the pool's PoolKey on disk, so steps 2–5 need only the poolId.
python3 "$S"/lp_read.py pools --token <addr>

# 2. Ticks for the band. --from-current keeps it single-sided; the two --mcap flags go in
#    either order, so "from here to 200k" is the same whether 200k is above or below.
python3 "$S"/lp_read.py ticks --pool <poolId> \
  --mcap-lower <current mcap> --mcap-upper <target mcap> --from-current

# 3. Dry run. Read `position type` from step 2: it names the currency, so use --amount0 for
#    currency0 and --amount1 for currency1. Add --allow-hooked if step 1 said to.
#    "All of it" is `--amount1 max`: the wallet balance is read for you and the slippage
#    buffer is capped at it — do not query balanceOf yourself.
python3 "$S"/lp_write.py mint --pool <poolId> \
  --tick-lower <t> --tick-upper <t> --amount1 <n|max> [--allow-hooked]

# 4. If step 3 exits 2 with "blocked on approvals" — approve, then repeat step 3.
#    "blocked: the wallet cannot cover the amounts" is a sizing problem, not an approval
#    one: use `--amount1 max` or a smaller amount, and do not run approve.
python3 "$S"/lp_write.py approve --token <addr>

# 5. Show the user the table from step 3, ask, and only then:
python3 "$S"/lp_write.py mint ...same flags... --broadcast --confirm <PLAN_HASH>
```

Five ways this turns into a loop, all seen in practice. Each is a deviation from the five
steps above, and the fix is always to go back to them:

- **Choosing the side yourself** instead of reading `position type` from step 2 — backwards
  half the time (§8).
- **Abandoning a pool because it has a hook** without checking its flags first (§8).
- **Skipping `approve`** — `mint` exiting 2 on approvals means the parameters were fine and
  the allowance was not, so it is not a reason to try another pool (§7).
- **Omitting `--from-current`** when one end of the band is today's price (§6).
- **Recomputing ticks by hand in Python** — `ticks` already does it, against the same math
  the mint uses.

## The protocol — follow it exactly

1. Run the command **without** `--broadcast`. It simulates and prints a parameter table, the
   simulated wallet deltas, and a `PLAN_HASH`.
2. Show the user that table and use **AskUserQuestion** to get an explicit yes/no.
3. Only on an explicit yes, re-run the **identical** command plus
   `--broadcast --confirm <PLAN_HASH>`. AgentOS then asks the user to approve that exact
   command in its own approval prompt before the key is released — `/elevated`, `bypass` and
   auto-approve do not answer it. If the result is `approval_pending`, wait for the user;
   never retry in another form.
4. If the hash changed in between, the parameters changed — go back to step 1. Never paste a
   hash the script did not just print.

The `PLAN_HASH` binds the broadcast to the plan that was simulated; it is not the approval.
The approval is the user's answer to AgentOS's prompt, which no command can produce.

`PLAN_HASH` covers chain, contract, subcommand, tokenId/poolId, ticks, liquidity, slippage
bounds, recipient, signer, and the guards the table showed you: the Permit2 expiration, the
tick-drift bound and the deadline offset. It excludes the *absolute* deadline and the gas
price, so a re-run a few minutes later still matches. Anything that changes what the
transaction does changes the hash — if you find a flag that does not, that is a bug, and
`selftest.py` Tier 6 is where it gets pinned.

## Signing

The key is read from **`UNIV4_LP_PRIVATE_KEY` in the environment and nowhere else**.
**Never put a private key on a command line** — it would land in shell history and in the
agent transcript. Redaction masks the key's value once the gateway knows it (set it via
Settings / `env.set` / `.env`), but a key only ever on a command line is never registered, so
keep it out of argv. Override the
variable name with `--signer-env <VAR>`. Only the derived address is ever printed —
`lp_write.py address` prints just that, and is the way to answer "which wallet am I using".
Signing is pure Python and therefore **not constant-time**: use a hot LP key, not a treasury
key.

`--from <address>` is **planning mode**: simulate as any address with no key present. It can
never broadcast. Use it to answer "would this even work" before anyone approves tokens.

## 7. Approvals (Permit2, two legs)

```bash
python3 "$S"/lp_write.py approve --token <erc20>
```

v4 pulls tokens through Permit2, so both legs are needed: `ERC20.approve(Permit2, max)` and
`Permit2.approve(token, PositionManager, amount, expiration)`. The command reports current
allowances and skips whichever leg is already satisfied. Expiration defaults to 30 days.
Native ETH needs no approval.

## 8. Mint a new position

```bash
python3 "$S"/lp_write.py mint --pool <poolId> --tick-lower <t> --tick-upper <t> \
  --amount1 <humanAmount> [--slippage-bps 100] [--recipient <addr>]
```

Size with exactly one of `--amount0`, `--amount1`, or `--liquidity` (give both for a
two-sided position). Ticks snap outward to `tickSpacing`. `--amount0 max` / `--amount1 max`
(or `all`) sizes from the signer's ERC-20 balance and caps the slippage-padded maximum at
that balance, so "deposit everything" is one flag and never trips the balance check; it is
refused for the native currency, which has to keep gas.

**Which currency a single-sided range takes — the one rule this section exists for.** A range
entirely **above** the current price takes **currency0 only**; entirely **below** takes
**currency1 only**. The wrong side is rejected with an explanation rather than minting
nothing.

Never derive that from the market cap. The mapping flips with which currency the token is:
when the token is `currency1`, a *higher* market cap is a *lower* tick, so "from here up to a
higher mcap" is a range **below** the current tick and takes the token itself. When it is
`currency0` it is the other way round. `ticks` (§6) prints the answer on its `position type`
line — pass its `--tick-lower` / `--tick-upper` straight through.

Written out, because "a sell sits above the price" is true for exactly half of all pools:

| intent | token is | you deposit | range vs price | fills as the tick |
|---|---|---|---|---|
| limit **sell** the token | currency0 | currency0 (the token) | **above** | **rises** |
| limit **sell** the token | currency1 | currency1 (the token) | **below** | **falls** |
| limit **buy** the token | currency0 | currency1 (the quote) | **below** | **falls** |
| limit **buy** the token | currency1 | currency0 (the quote) | **above** | **rises** |

Sort order is not something the user chooses — it falls out of the two addresses. So never
key logic on "sell" or "buy"; key it on **which currency the range takes**, which the
geometry fixes on its own.

**Hook policy: this skill mints into hook-less pools by default.** A pool whose `hooks` is not
the zero address is refused outright unless you pass `--allow-hooked`, because a hook can
revert the add or take a delta out of it. So plain `mint` is already the "add LP with no hook"
path — the parameter table prints `hooks: none` when that is what you got.

**A hook is not by itself a reason to walk away from a pool.** Which one it is decides that:

| hook | flags | third-party LP adds |
|---|---|---|
| Doppler / Bankr | `0x2544` | **accepted** — no `BEFORE_ADD_LIQUIDITY` gate, so nothing turns the add away up front. Use `--allow-hooked` |
| Clanker / Liquid | `0x28cc` | rejected — `BEFORE_ADD_LIQUIDITY` turns them away |
| anything else | — | unknown; the dry run is the cheap way to find out |

Read the `hook flags` line that `pool` prints rather than the hook address: `BEFORE_ADD_LIQUIDITY`
in the decoded list is the one that refuses outsiders. `AFTER_ADD_LIQUIDITY` — which Doppler
does set — runs after the add and can still revert it, which is exactly what the dry run
catches.

`--allow-hooked` lets the attempt through, it does not make it work — but the dry run costs
one call and never broadcasts, so on a Doppler pool run it rather than hunting for a
hook-less alternative. The deepest pool for a launched token is usually the launch pool, and
on Robinhood Chain that is normally a Doppler pool, while the hook-less pools for the same
token are frequently dust at punitive tiers. Compare `TVL` before switching (§1).

**Targeting the pool.** `mint` takes a `--pool <poolId>` that must already exist. Find one
first with `pools --token <addr> --no-hook` (§1); if none exists at the tier you want, open
one with `create-pool` (§8b). On Base the PoolKey behind that id still has to be recovered,
so pass `--token <addr>`, or spell the key out with
`--currency0 --currency1 --fee --tick-spacing` (§2) — those flags work here too.

## 8b. Create a pool

```bash
python3 "$S"/lp_write.py create-pool --token0 <addr|native> --token1 <addr> \
  --fee 3000 --tick-spacing 60 (--tick <t> | --price <n>) [--allow-odd-tier]
```

Only reach for this when `pools --token <addr>` genuinely found nothing usable. Opening a
second pool for a token that already has one splits its liquidity and leaves yours as the
shallow side.

**The starting price cannot be changed, ever.** A pool is initialized once. If the tick you
pick is off the real market, the first swap or mint against it is an arbitrage at your
expense — and the only fix is to open a different pool. Treat the price line in the table as
the thing the user is approving, not the fee tier.

- `--price <n>` is **currency1 per one whole currency0**, decimals already handled, i.e. the
  same direction the table prints. The two tokens are sorted into PoolKey order for you, so
  which one you typed as `--token0` does not decide anything — read the printed
  `currency0`/`currency1` rows and check the price is the right way up. Both directions are
  printed for exactly this reason.
- `--price` lands on the nearest tick (the 1.0001 grid, ~1 bp), so the effective price in the
  table is what gets initialized, not the number typed. `--tick <t>` sets it exactly. The
  initial tick does **not** have to be a multiple of `tickSpacing` — only position boundaries
  do — so nothing is snapped here.
- `hooks` is fixed at the zero address. There is no `--hooks` flag: initializing a hooked pool
  runs that hook's `beforeInitialize`/`afterInitialize`, which is out of scope for this skill.
- `--fee`/`--tick-spacing` off the conventional tiers (100/1, 500/10, 3000/60, 10000/200) are
  refused unless you pass `--allow-odd-tier`, because `pools` only searches those tiers and
  you would not find the pool again afterwards. A dynamic fee (`0x800000`) is refused
  outright — it requires a hook.

The transaction moves no tokens and needs no approvals, so there is nothing to `approve`
first; it still goes through the same dry-run → `PLAN_HASH` → `--broadcast --confirm` gate as
everything else. It prints the `poolId`, which is what `mint` then takes. If the pool already
exists the command says so, prints its current tick, and exits without planning anything.

## 9. Increase / decrease / collect / burn

```bash
python3 "$S"/lp_write.py increase --token-id <id> --amount1 <n> [--slippage-bps 100]
python3 "$S"/lp_write.py decrease --token-id <id> --pct 50 [--recipient <addr>]
python3 "$S"/lp_write.py collect  --token-id <id> [--recipient <addr>]
python3 "$S"/lp_write.py burn     --token-id <id> [--recipient <addr>]
```

`--recipient` defaults to the signer everywhere. On `decrease`, `collect` and `burn` it is
where the tokens come out. On `increase` it is only the `SWEEP` target — the unspent part of
`msg.value` on a native-currency0 pool — and the table says which of the two it is.

`decrease` also sweeps accrued fees — `TAKE_PAIR` returns principal and fees together. There
is no `collect()` in v4: it is `DECREASE_LIQUIDITY` with `liquidity = 0` plus `TAKE_PAIR`.
`BURN_POSITION` reverts on a non-empty position, so `burn` emits `DECREASE(100%)` +
`BURN_POSITION` + `TAKE_PAIR` in one call — the NFT is destroyed, confirm carefully.

---

## Reading the simulation

Every write dry-run runs `eth_simulateV1` with `traceTransfers`. `modifyLiquidities` returns
nothing, so that trace is the only way to know what a call really moves.

**When the trace disagrees with the computed table, the trace is right** — the script prints
a `WARNING` above 0.5% divergence. Causes and how to read a revert:
`{baseDir}/assets/v4-reference.md`.

---

# Part C — Ratchet (unattended take-profit)

`ratchet.py` watches a one-sided position fill and takes profit at milestones without anyone
watching. At each milestone it exits the whole position, keeps whatever has already
converted, and redeploys **only the unconverted remainder** into a narrower range running
from the current price to the original far edge.

It ratchets — converted value never goes back to work, and a price retracement leaves the
position sitting still rather than unwinding. That is the trade: a plain one-sided position
would buy the token back if the price came down, and this deliberately gives that up.

```bash
python3 "$S"/ratchet.py arm --token-id <id> [--steps 30,60,100]
python3 "$S"/ratchet.py arm --token-id <id> --steps 30,60,100 --confirm <MANDATE_HASH>
python3 "$S"/ratchet.py tick --all --broadcast --json --alert-only   # what cron runs
python3 "$S"/ratchet.py status --id <m>   |   list   |   disarm --id <m>
```

**Milestones are measured against the ORIGINAL principal, not what is left.** 100k AGENTOS
with `--steps 30,60,100` fires when 70k, then 40k, then 0 remains — three fires, then the
mandate completes. Re-basing onto the remainder would never reach zero.

**One position, one live mandate.** Re-running `arm` on a position that already has one is
safe and idempotent: identical terms print `already armed as <id>` and change nothing, exit
0. Differing terms (`--steps`, principal, far edge, signer) are refused — two mandates would
each try to burn the same NFT — so `disarm` the old one first if the new terms are the ones
you want. Do not work around this by re-labelling: `--label` is only a display string.

Disarming settles the mandate but leaves any cron monitor and its files standing. If nothing
else needs the ticker, take those down too — see **Tearing one down**.

## One transaction, and why that matters

A fire is a single `modifyLiquidities` call:

    DECREASE_LIQUIDITY(all) → BURN_POSITION → MINT_POSITION(remainder) → TAKE_PAIR

There is **no SETTLE leg**. Deltas accumulate per currency across the unlock, and the mint
always redeploys strictly less of the principal than the decrease just credited (and zero of
the other currency, since the new range is one-sided), so both net deltas are still positive
when `TAKE_PAIR` runs. Nothing is pulled from the wallet.

Two consequences that a two-transaction design would not have:

- **Permit2 is not involved.** An allowance that lapsed mid-mandate cannot strand anything.
- **There is no window with loose tokens and no position.** The fire either happened or it
  did not, and the burned NFT proves which — which is what makes unattended recovery a
  boolean rather than a guess.

## The mandate is the approval

`arm` is attended and hash-confirmed, exactly like a write: it prints the full table and a
`MANDATE_HASH` you must echo back, and `arm ... --confirm <MANDATE_HASH>` waits for the
user's approval in AgentOS's prompt. AgentOS records each mandate armed that way, with its
bounds; a tick refuses (`action: refused`) any mandate that is not on that record or whose
bounds changed on disk since — including mandates armed before this rule existed, which need
`disarm` and a fresh `arm`. `clear-attention` waits for the same approval. What the mandate then buys is the right to replay *that*
approval against a plan proven to fall inside it. Every fire is checked against the pinned
`chainId`, PositionManager, poolId (which is `keccak(PoolKey)`, so the hook address is pinned
with it), tokenId, signer, recipient, slippage floors, milestone index and — for a re-arm —
the fixed far edge plus a re-derived near edge and a zero cap on the harvested currency.

`lp_write.py --broadcast` is unchanged: it still requires a `--confirm <PLAN_HASH>`, waits
for the user's approval in AgentOS, and its CLI cannot construct a mandate authorization at
all.

**Optional bounds, off by default:** `--max-principal-per-fire`, `--max-fee-per-gas`,
`--expires-days`. Without them there is no ceiling on what a fire may move, so a math error
or a manipulated pool has no brake beyond the slippage floor. Say so when arming one.

## States

| state | meaning | what `tick` does |
|---|---|---|
| `ARMED` | position live, nothing in flight | evaluate; fire if a milestone is due |
| `FIRE_SENT` | hash and nonce on disk, outcome unknown | reconcile against the chain |
| `COMPLETE` | final milestone fired | nothing |
| `NEEDS_ATTENTION` | an ambiguity it refuses to resolve alone | nothing until `clear-attention` |
| `DISARMED` / `EXPIRED` | stopped by a human / by the clock | nothing |

Reconciliation is chain-first: `ownerOf` reverting means the fire landed, the nonce watermark
separates "still pending" from "dropped", and a log scan bounded by the journalled
`sentAtBlock` finds the replacement tokenId. If none of those give a total answer it goes to
`NEEDS_ATTENTION` rather than retrying — a duplicate burn is harmless, a duplicate mint is
not.

**"The node did not answer" is not "the NFT is gone".** `multicall` reports a transport
failure the same way it reports a revert, so `ownerOf` failing is only read as a burn when a
`nextTokenId` control call in the same request came back. If both fail the tick reports
`deferred`, changes nothing, and tries again next time — a five-minute cron meets a flaky
RPC eventually, and halting on one would make every blip cost a manual `clear-attention`.

A price gap that clears several milestones at once fires **only the deepest**; the ones it
skipped are satisfied by the same reading and recorded as fired.

### When `clear-attention` needs `--token-id`

One `NEEDS_ATTENTION` cause needs more than an acknowledgement: the fire landed, the old NFT
is burned, and neither the receipt nor the log scan could say which position replaced it.
Find it with `lp_read.py positions` — the mandate's signer is the configured wallet, so no
`--owner` is needed — and hand the new id back:

```
python3 "$S"/ratchet.py clear-attention --id <m> --token-id <new>
```

It is checked before adoption — same pool, same signer, live liquidity, same far edge, and
one-sided on the same side of the price — and the old position must really be burned.

Do **not** disarm and arm a fresh mandate instead. `arm` reads the position it is given as a
new original principal, so every milestone still to come is rebased onto the remainder: a
100k mandate that has already converted 60k would re-arm as a 40k mandate and fire its next
30% at 28k left, not at the 40k the original plan meant.

## State on disk

`$UNILP_STATE_DIR`, else `$AGENTOS_HOME/state/unilp`, else `~/.agentos/state/unilp`. Three
files per mandate: the mandate JSON (mode 0600, replaced atomically), an append-only
`.log.jsonl` write-ahead log, and a `.lock` for `flock`. Overlapping cron ticks are normal
and the loser exits 0.

The mandate file is now an **authorization artifact**. It is written 0600 and the runner
re-hashes it to its own filename on load, which catches corruption and hand-editing. It is
not a defence against someone who can write that directory — they can also read the dotenv
holding the signing key, and forging a mandate is the long way round from there. Only the
`immutable` block is covered by that hash; the mutable fields are guarded by the predicate
re-deriving them from the chain at the gate, not by the file.

Two rules the log follows, both because it is what restores an in-flight fire after a crash:

* Only the **last** line may be unreadable — that is a torn write from a power cut, and the
  record it lost had not been confirmed anyway. A bad line with good lines after it stops
  the runner, because silently skipping it could erase the only proof a transaction was
  signed.
* A `tx.sent` record carries the whole in-flight fire, not just the hash. A crash between
  that append and the mandate replace leaves the file reading `ARMED`; the next tick replays
  the record and finds the transaction rather than planning the same milestone again.

`--id` only ever accepts 32 lowercase hex characters — the shape `mandate_id` produces — and
is validated before any path is built, so it cannot walk out of the state directory.

## Wiring it to cron

**`$S` does not exist here.** It is a variable in your shell; a cron job runs in a fresh
isolated session, or as a file with no shell state at all. Anything that outlives this
conversation — a cron task string, a script on disk — has to carry the full path
`{baseDir}/scripts/ratchet.py`, spelled out. This is the one place the `"$S"` form above is
wrong.

Two shapes can carry it, and they are not equal. **If the user did not say which one they
want, schedule the script job.** Reach for `agent_turn` only when they explicitly ask for a
model in the loop.

`tick` does the reconcile and the fire in one process on purpose: no agent judgement sits
between deciding and sending — which is why the model in the loop is optional. Put the
command in a script under `~/.agentos/scripts/senior-unilp-manager/{job_id}/` (again with
the path spelled out) and the ticks cost no model call and no tokens; stdout is delivered
verbatim, and a tick that prints nothing stays silent:

```
cron(action="add", schedule={"kind": "every", "every_seconds": 600},
     job_kind="script", name="<something a human will recognise>",
     script="senior-unilp-manager/{job_id}/tick.sh", session_target="isolated")
```

`{job_id}` is written literally: `cron` replaces it with the id of the job it just created,
and reports the resolved location as `script_path`. Write `tick.sh` there straight away — the
job is live from the moment it is added, and five failing ticks retire it. Pass `name`, or
the job carries its script path as its name for the rest of its life, including in every
failure alert. **Setting up a monitor** below is the layout that path belongs to.

Write `--alert-only` into `tick.sh`, alongside `--json`. Without it every tick delivers the
full result of every mandate, which on a healthy ratchet is a block of JSON saying nothing
happened, every ten minutes, until nobody reads it any more. With it a tick that found
nothing still prints the whole payload — the run history keeps it — but ends on the line
`{"wakeAgent": false}`, which the cron runner reads as "no news" and delivers nothing:

```bash
#!/bin/sh
exec python3 ~/.agentos/skills/senior-unilp-manager/scripts/ratchet.py \
  tick --all --broadcast --json --alert-only
```

A tick that fired, adopted a landed fire, halted, was rejected, expired, or built a plan on a
dry run prints a one-line summary above that JSON and is delivered as usual. So does a
mandate sitting in `NEEDS_ATTENTION`, on **every** tick until a human clears it: that state
reports the same `noop` as a healthy mandate, and going quiet on it would be the one silence
that costs money. `--alert-only` only changes `--json` output; run by hand without it and the
command prints exactly what it always did.

The other shape puts a model on every tick. Use it **only** when the user explicitly asks
for one — so it can summarize the result or escalate in its own words — and know what it
costs: a full turn every five minutes for a job that is almost always a no-op.

```
cron(action="add", schedule={"kind": "cron", "expr": "*/5 * * * *"},
     task="{python} {baseDir}/scripts/ratchet.py tick --all --broadcast --json — report the "
          "result, and raise the alarm if any state is NEEDS_ATTENTION",
     job_kind="agent_turn", session_target="isolated")
```

A script job runs the file itself and never starts an agent turn, so it takes **no**
`tool_policy` — `tool_policy.elevated` belongs to the `agent_turn` shape above and is
rejected here. Both shapes need an interactive CLI or Web caller; neither can be scheduled
from a chat channel. A script job does **not** inherit `UNIV4_LP_PRIVATE_KEY`: AgentOS runs
`tick.sh` through its signing gate only when the file is exactly the one `exec python3 ...
ratchet.py tick --all --broadcast --json --alert-only` line shown above (comments aside), so
keep it that way — anything more and the tick runs without the key. The same applies to the
`agent_turn` shape: the task must run that one command on its own. A tick with any other
flag (`--rpc`, `--deadline-secs`, ...) needs the user's approval, which cron cannot ask for.
The key itself must be set in Settings > Skills: a missing key fails every tick, and five
consecutive failures retire the job.

<!-- always -->
## Setting up a monitor

**Schedule the script shape unless the user asked for a model in the loop.** `job_kind="script"`
runs `tick.sh` itself and costs no model call and no tokens; `agent_turn` puts a whole turn on
every tick of a job that is almost always a no-op. **Wiring it to cron** above has both.

One monitor, one directory:

```
~/.agentos/scripts/senior-unilp-manager/{job_id}/
```

Everything that monitor needs lives there and nowhere else — `tick.sh`, any helper you write
for it, any scratch or output the run keeps. The directory is named by the cron **job** that
owns it, so the mapping is one-to-one and mechanical: delete the job, delete the directory.
It is the job id rather than a mandate id because a tick script runs `--all` — one job covers
every mandate, and naming its directory after one of them would be a lie about what it
watches. Never drop a file for a monitor at the top of `~/.agentos/scripts/`: that directory
is shared with every other skill's jobs, and a file with no owner in its path is one nothing
can ever safely clean up.

`{job_id}` is literal, and `cron` fills it in — see **Wiring it to cron** above for the one
`add` that creates the job and names its directory in the same call. `script=` is the one
path in this skill that is *not* spelled out in full: it is relative to
`~/.agentos/scripts/`, and an absolute or `~`-prefixed value is refused outright even when it
points inside that directory.

**Mandate state does not go here.** The mandate JSON, the `.log.jsonl` write-ahead log, and
the `.lock` stay where **State on disk** above puts them — `$UNILP_STATE_DIR`, else
`$AGENTOS_HOME/state/unilp`, else `~/.agentos/state/unilp` — because that is where
`ratchet.py` writes them and where it looks for them. A mandate written into the scripts
directory is a mandate the runner never finds.

### Tearing one down

A monitor's files outlive the mandate that motivated it, and nothing removes them for you.
When a mandate is disarmed and no other mandate needs the ticker, take the job and its
directory down together:

```bash
# cron(action="remove", job_id="<cron_id>") first, then:
rm -rf ~/.agentos/scripts/senior-unilp-manager/<cron_id>
```

Leave the state directory alone — `disarm` has already settled the mandate there, and the log
is the record of what the monitor did.

## Before arming anything real

Run `tick` **without** `--broadcast` first — it is a full dry run of the transaction,
simulation trace and `PLAN_HASH` included, and sends nothing. Then rehearse one real fire by
hand with dust, on **both** a range above the price and one below: the two directions take
different branches at every layer, and a hook can answer differently on each.

## Error handling

| Symptom | Cause / fix |
|---|---|
| `no RPC url for …` | Set `RPC_ROBINHOOD_URL` / `RPC_BASE_URL`, or pass `--rpc <url>` |
| `invalid --rpc scheme …` | The endpoint must be `http://` or `https://` |
| `env var UNIV4_LP_PRIVATE_KEY is not set` | Set it in the agent environment — never as a flag |
| `eth_getLogs: range over 100000 blocks is not supported …` | The Robinhood RPC caps log ranges. Do not switch RPCs or grep for the pool: `pools --token <addr>` derives it from the known hook, or give the PoolKey directly (`--currency0 --currency1 --fee 0x800000 --tick-spacing 200 --hooks <hook>`) |
| `… Temporary internal error. Please retry …` | The RPC hiccuped; the script already retried 3×. Re-run the same command once |
| `Base cannot serve a wide eth_getLogs range …` | Give the PoolKey directly (`--currency0 --currency1 --fee --tick-spacing`), or `--token <addr>` to derive it, or `--scan-logs` |
| `could not derive the PoolKey for … from token …` | Not a launch pool, and not a hook-less pool at a conventional tier. Spell the PoolKey out, or `--scan-logs` |
| `the PoolKey given on the command line does not describe …` | Recompute guard did its job. Check fee (`0x800000` for dynamic), tickSpacing, `--hooks`, and that `currency0 < currency1` |
| `No known launchpad on Base deployed …, no hook-less pool …` | Token predates the registries, used another launcher, or its pool pairs with an unlisted currency — try `--quote <addr>` |
| `NOTE: scanned N of M bitmap words` | A `--mode ticks` read was truncated — the totals are incomplete |
| `poolId recompute … MISMATCH` | PoolKey does not hash to the id — usually the live `lpFee` was used instead of the `0x800000` dynamic flag |
| `self-check … MISMATCH` | Log decode or tick math is off. Do not report the reserves |
| `mint — blocked: the wallet cannot cover the amounts` | Approvals are fine; the balance is short of the slippage-padded maximum. `--amount0/--amount1 max`, a smaller amount, or a lower `--slippage-bps`. Do not run `approve` |
| `pool has hook 0x…` | Intentional gate. Re-run with `--allow-hooked` after telling the user what the hook can do |
| `range is entirely above/below the current price` | Wrong currency for a single-sided range; swap `--amount0` / `--amount1`. Above takes currency0, below takes currency1 — or just re-read `position type` from `ticks` |
| `the price indexer is rate-limiting us …` | Temporary, not a property of the token. Wait ~60s and re-run the same command; a successful fetch is cached for 60s and shared across commands. Do not switch pools over it |
| `the band is narrower than one tickSpacing …` | `--from-current` had no room on either side of the current tick. Widen the mcap band, or pass `--tick-lower` / `--tick-upper` |
| `AllowanceExpired` / `InsufficientAllowance` | Run `approve` first — the pool accepted the add |
| `MaximumAmountExceeded` / `MinimumAmountInsufficient` | Raise `--slippage-bps` |
| `--confirm mismatch` | Parameters changed after approval. Re-plan — never force it |
| `pool moved: tick X → Y` | Drift past `--max-tick-drift` between plan and send. Re-plan |
| USD columns show `n/a` | GeckoTerminal has not indexed the token. Amounts are still exact |
| `tokenId … has an empty PoolKey` | The position was burned, or that id was never minted on this chain. `getPoolAndPositionInfo` is a mapping read and does not revert for a burned id, so this check is what stands in for one |
| `range … straddles the current tick` (arm) | A ratchet needs a range entirely above or entirely below the price. Two-sided positions have no "unconverted remainder" to redeploy |
| `mandate refuses the plan: …` | The unattended bounds check rejected the fire. Not a retry — read which bound and why |
| `position #N is already armed as <id> … differs in: …` | A second mandate on one position would burn the same NFT twice. `disarm --id <id>` if the new terms are the ones you want. Identical terms are not an error at all — they print `already armed` and exit 0 |
| `another tick holds the lock` | A concurrent cron tick, normal during a fire. Exits 0; nothing to do |
| `mandate … does not hash to its own filename` | The state file was edited or truncated. Do not repair it by hand — `disarm` and arm a fresh mandate |
| ratchet state `NEEDS_ATTENTION` | It refused to resolve an ambiguity alone. `status --id <m>` for the record, then `clear-attention --id <m>` once the position is confirmed — add `--token-id <new>` if the note says the replacement could not be identified |
| ratchet action `deferred` | The node could not be read, so nothing was decided and nothing changed. Normal on a flaky RPC; investigate only if it repeats for hours |
| a `--alert-only` cron job has said nothing for hours | Expected — that is the flag working. The runs are still there: Cron → Run History holds the full JSON of every tick. A job that had actually stopped would show no runs at all, not quiet ones |
| `journal holds an unreplayed … record` | The log has an event this build does not know. Almost always a downgrade — run the version that wrote it |
| `… line N is corrupt … not a torn tail` | The write-ahead log was damaged mid-file. `status --id <m> --json` still reads the mandate; the log needs a human before the runner will move |
| `… is not a mandate id` | `--id` takes the 32 hex characters `list` prints, nothing else |

## Verifying a change

```bash
python3 "$S"/selftest.py     # offline assertions only, no network
```

Covers the keccak / EIP-55 / ABI-codec primitives, secp256k1 signing, TickMath, the AGENTOS
reserve figures, market-cap bands, the three Base launchpad poolIds, every `unlockData`
blob, and the planning helpers `--from-current` leans on. Run it after touching anything in
`scripts/unilp/`; the live fixtures it pins are listed in its module docstring.

Tier 7 covers the ratchet, and runs its assertions across all four combinations of
{token is currency0, token is currency1} × {range above the price, range below it}, because
a bug on one side is invisible from the other. It also pins the five independent reasons
`lp_write.py`'s CLI cannot construct a mandate authorization; if you touch `run_plan`'s gate,
that tier is the one that has to stay green.

Its state-machine block drives every crash and network outcome a cron runner meets against
a stub chain — a send journalled but never saved, an unreachable node, an unidentifiable
replacement, a retry budget that has to survive on the mandate rather than on the fire.
Those rows exist because each one was a real bug at some point; they are cheap to assert
and close to impossible to keep right by reading the code.

Deeper background — the v4 Actions table, hook permission bits, and the Clanker / Liquid /
Doppler registry architecture: `{baseDir}/assets/v4-reference.md`.
