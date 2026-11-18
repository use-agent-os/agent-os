# PartyFactory reference

Everything below was read from the verified source on
[Etherscan](https://robin.etherscan.io/address/0x626C3d09B65bF5d1D40E0D5F25e19fa49783B3D4)
and confirmed with live `eth_call`s. Read this when the skill's behaviour needs
explaining or when the factory is redeployed and constants have to be re-checked.

## Deployment

| Contract | Address |
|---|---|
| PartyFactory | `0x626C3d09B65bF5d1D40E0D5F25e19fa49783B3D4` |
| PartyLocker | `0x35E41f84d3fD61d4648F0c8B41a1E7d301bCd75E` |
| SushiSwap V3 factory | `0xE51960f1B45f1C9FB6D166E6a884F866fC70433B` |
| NonfungiblePositionManager | `0x51d0e5188afe12d502e29D982d20C190e7816107` |
| WETH | `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73` |
| USDG | `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` |
| ETH/USD feed | `0x78F3556b67E17Df817D51Ef5a990cDaF09E8d3A9` (8 dp, 25h heartbeat) |
| Factory owner | `0xd86EC279AD4871483f6c3D7ce54AD00067f120E9` |

Chain: Robinhood Chain, id **4663**, RPC `https://rpc.mainnet.chain.robinhood.com/`,
explorer `https://robin.etherscan.io`. Solidity 0.8.25, viaIR, 200 runs.

## `launch`

```solidity
function launch(
    string  calldata name,
    string  calldata symbol,
    string  calldata metadataUri,
    bytes32 salt,
    address pairedAsset,
    int24   expectedStartTick,
    uint256 deadline,
    address creator,
    address feeRecipient,
    uint256 devBuyAmountIn,
    uint256 devBuyMinOut
) external payable returns (address token, address pool, uint256 devBuyOut);
```

Selector `0xce61a35c`. Eleven flat arguments — not a struct.

What the factory does, in order (source lines ~300–330):

1. `paused` / `locker` / `deadline` checks.
2. `creator != msg.sender` → `CreatorNotCaller()`. `feeRecipient == 0` becomes the creator.
3. `startTickFor(pairedAsset)` — reverts `PairedAssetNotAllowed()` for unlisted assets.
   `startTick != expectedStartTick` → `StartTickChanged()`. A fallback-priced tick
   emits `FallbackTickUsed`.
4. CREATE2-deploys the token; reverts `TokenNotToken0()` if `token >= pairedAsset`.
5. Creates and initialises the pool; reverts `PoolAlreadyInitialized()` if it already has a price.
6. Mints the whole supply as one band `[startTick, 887200]`, LP NFT to the locker.
7. Burns rounding dust to `0xdEaD`.
8. Registers with the locker.
9. Performs the dev buy, if any.
10. Emits `TokenLaunched`.

### Protocol constants

```
TOTAL_SUPPLY  1_000_000_000e18      FEE  10000 (1%)      TICK_SPACING  200
MAX_USABLE_TICK  887200             MIN/MAX_FDV_USD  100 / 1_000_000_000
MIN/MAX_PRICE_AGE  1 minute / 3 days
```

### Pricing is not an input

`expectedStartTick` reads like a knob and is not one. The factory computes its own
tick and reverts on any mismatch — the argument exists so a caller commits to the
price they were quoted, not so they can pick one.

The tick comes from `initialFdvUsd` (currently `10000`) via the Chainlink feed, and
`setInitialFdvUsd` is `onlyOwner`. At the time of writing `startTickFor(WETH)` is
`-190600`, ETH is $1,891.51, and the resulting FDV is **$9,990** against a $10,000
target. A dev buy only moves price upward, after the open.

### Dev buy funding

Exactly one of:

* `msg.value > 0` — wrapped to WETH by the factory. **WETH pairs only**
  (`DevBuyWethOnly()`).
* `devBuyAmountIn > 0` — pulled via `transferFrom`, so the factory needs an
  allowance. Works for any allowlisted pair; the unit is the paired asset.

Both at once → `AmbiguousDevBuy()`. Neither → no swap. Unspent input is refunded
exactly (never a balance sweep). The swap is the pool's first, in-transaction, so
`eth_call` with `devBuyMinOut = 0` returns the exact fill.

## Salt mining

```
effectiveSalt = keccak256(abi.encodePacked(deployer, salt))
token         = CREATE2(factory, effectiveSalt, keccak256(PartyToken initcode))
initcode      = PartyToken.creationCode ++ abi.encode(name, symbol, 1e27, factory, metadataUri)
```

The salt is bound to the deployer, so copied calldata can only deploy inside the
copyist's namespace. `computeTokenAddress(deployer, salt, name, symbol, metadataUri)`
predicts the address off-chain; the skill batches it to search.

The constraint is `token < pairedAsset`. Hit rates: **WETH ~4.6%**, **USDG ~37.4%**.

Verified vector — deployer `0x8a86E3927BD9E4200BC18DAD3A158CAa4806Ba51`, salt `7`,
name `Pools Fun`, symbol `POOL`, metadata
`ipfs://bafkreiavjuxoyk5yoglksbl5gty2mkg74fsao6g2blr5mmjf3wr2kyosma`
→ **`0x0762a4F683f0531b70bC7D6882781457d80F689a`**. `selftest.py` pins this.

## Custom errors

Every error PartyFactory declares, with its selector. **The code does not hardcode
these** — `factory.py::ERROR_SIGNATURES` is keyed by signature and computes the
selectors at import, because a hand-written constant can silently collide with a
different error and then report the wrong cause with full confidence. (An early
revision of this skill claimed `0x1f2a2005` for `DevBuyTooLarge`; that selector
actually belongs to `ZeroAmount()`.) The table below is for humans reading a raw
revert blob.

| Selector | Error |
|---|---|
| `0xb4f54111` | `DeployFailed` |
| `0x0f5ddbb1` | `StartTickChanged` |
| `0xd79cce06` | `TokenNotToken0` |
| `0x203d82d8` | `Expired` |
| `0x7607bc0d` | `CreatorNotCaller` |
| `0x6e4e2579` | `AmbiguousDevBuy` |
| `0xb6dc33e4` | `DevBuyWethOnly` |
| `0xedaf7b53` | `DevBuyTooLittle` |
| `0x82ce2bbd` | `DevBuyTooLarge` |
| `0x5a9b44ca` | `PairedAssetNotAllowed` |
| `0x9e87fac8` | `Paused` |
| `0x7983c051` | `PoolAlreadyInitialized` |
| `0xf619c36a` | `LockerUnset` |
| `0xce8ef7fc` | `InvalidTick` |
| `0xd92e233d` | `ZeroAddress` |

`TokenLaunched` topic0:
`0xd1844be5e646143a1c9e6841471e58911bac843c7d033e435d304cfeba2c2153`
(indexed: token, pool, creator).

## Paired-asset allowlist

Registration *is* the allowlist — an asset with no curve cannot launch.

| Asset | Feed | Heartbeat | Fallback tick |
|---|---|---|---|
| WETH | `0x78F3556b…` ETH/USD | 90000s (25h) | pinned |
| USDG | `0x61B7e565…` | 90000s (25h) | pinned |

`startTickFor` returns `(tick, live)`. `live == false` means the feed was stale or
the sequencer was down and the pinned fallback was used — the FDV will be off
target. The skill refuses to launch in that state without `--allow-fallback-tick`.

## The locker

The LP NFT goes to `PartyLocker` on launch and never comes back. Creator-facing
surface:

* `getPoolInfo(token)` → `(pairedAsset, pool, creator, feeRecipient, tokenIds)`
* `getPoolSplits(token)` → six uint16 bps values
* `collect(token)` — sweep fees from the LP into the locker
* `claim(token)` — pay out your share of what the locker holds
* `collectAndClaim(token)` — both
* `setFeeRecipient(token, addr)` — redirect future payouts

There is also a community-takeover flow (`queueCto`/`executeCto`/`cancelCto`),
deliberately out of scope for this skill.

## Reference launch

tx [`0x0978ee728eb9ae5e78c7f461fea96dc9372315b7eee5a69e91ba914d98d8d1c0`](https://robin.etherscan.io/tx/0x0978ee728eb9ae5e78c7f461fea96dc9372315b7eee5a69e91ba914d98d8d1c0)

```
name              Pools Fun          symbol   POOL
metadataUri       ipfs://bafkreiavjuxoyk5yoglksbl5gty2mkg74fsao6g2blr5mmjf3wr2kyosma
salt              0x…07              paired   WETH
expectedStartTick -190600            deadline 1786596532
creator/feeRecip  0x8a86E392…        devBuyAmountIn / MinOut  0 / 0
msg.value         0.001 ETH  -> native dev buy
```

Result: token `0x0762a4F6…`, pool `0x0512090d…`, LP NFT #4436 to the locker,
dev buy filled **187410.002243581166618109** POOL (0.0187% of supply), 6,096,288 gas.

Note `devBuyAmountIn = 0` *and* a dev buy happened — the 0.001 ETH `msg.value` is
the funding. That is the single most misread part of this ABI: `msg.value` is not
a launch fee, it is the dev buy.

## Metadata

`metadataUri` is one immutable string on the token. All three forms are live
on-chain today:

```
ipfs://Qmc9DCNH433Bqf22A6B7eMCngTe2XrGcnoajUcFxDMbgQp
ipfs://bafkreiavjuxoyk5yoglksbl5gty2mkg74fsao6g2blr5mmjf3wr2kyosma
data:application/json;base64,eyJuYW1lIjoiUG9vbHMiLCJzeW1ib2wiOiJQT09MUyIs…
```

The document shape pools.fun itself writes:

```json
{
  "name": "Pools",
  "symbol": "POOLS",
  "description": "Pools",
  "image": "https://gateway.pinata.cloud/ipfs/QmW1tmSiy9NZjvaSzKua39vtapNPDUdwrEs5knZkau3EnB",
  "website": "https://pools.fun/",
  "twitter": "https://x.com/pools_dot_fun"
}
```

`image` uses the HTTPS gateway form rather than `ipfs://` so wallets and explorers
render it without a resolver. The skill matches that.
