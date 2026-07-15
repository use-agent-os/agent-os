---
name: robinhood-rwa-addresses
description: "Look up Robinhood tokenized-stock (RWA) contract addresses and tickers. Use when: the user asks for the Robinhood token, on-chain contract address, ticker/symbol, or chain of a real-world stock or ETF (e.g. 'what is Apple's ticker', 'mã cổ phiếu Apple là gì', 'Robinhood contract address for Tesla', 'Microsoft token address'). Resolves a company name OR ticker to the on-chain token on Robinhood Chain. NOT for: live stock prices, trade execution, or non-Robinhood tokens. No API key needed."
homepage: https://robinhood.com
provenance:
  origin: agentos-original
  license: MIT
  maintained_by: AgentOS
metadata:
  {
    "agentos":
      {
        "emoji": "🪶",
        "homepage": "https://robinhood.com",
        "requires": { "anyBins": ["python3", "python"] },
      },
  }
entrypoint:
  command: python3 {baseDir}/scripts/rwa_lookup.py
  args:
    - --query
    - "{{ with.query | default(inputs.user_message) }}"
    - --limit
    - "{{ with.limit | default(5) }}"
    - --timeout
    - "{{ with.timeout | default(10) }}"
  parse: json
  timeout: 20
---

# Robinhood RWA Contract Addresses

Resolve a real-world stock or ETF to its **Robinhood tokenized-asset (RWA)**
on-chain token: ticker/symbol, contract address, chain id, and decimals.

Data source (public, no key): `https://tokens.coingecko.com/robinhood/all.json`
— the official CoinGecko token list for Robinhood Chain (chainId `4663`). Names
in that list carry a "• Robinhood Token" suffix; this skill strips it so plain
company names match.

## When the user asks

Questions this skill answers — in any language:

- "What is Apple's ticker / stock symbol?" → `AAPL`
- "mã cổ phiếu Apple là gì" → `AAPL`, contract `0xaf3d…93f9`
- "Robinhood contract address for Tesla" → the `TSLA` token address
- "What's the on-chain address of Microsoft on Robinhood?" → the `MSFT` address

Answer with the **symbol** and the **contract address**, and mention it lives on
Robinhood Chain (chainId 4663). Include the address verbatim.

## Run it

```bash
# By company name
python3 {baseDir}/scripts/rwa_lookup.py --query "Apple"

# By ticker
python3 {baseDir}/scripts/rwa_lookup.py --query "AAPL"

# Limit matches
python3 {baseDir}/scripts/rwa_lookup.py --query "Tesla" --limit 1
```

### Output (JSON)

```json
{
  "query": "Apple",
  "source": "https://tokens.coingecko.com/robinhood/all.json",
  "total_tokens": 228,
  "matches": [
    {
      "name": "Apple",
      "symbol": "AAPL",
      "address": "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9",
      "chainId": 4663,
      "decimals": 18,
      "logoURI": "https://assets.coingecko.com/..."
    }
  ]
}
```

## Matching rules

The lookup ranks matches so the best answer comes first:

1. Exact ticker match (`AAPL`) — highest.
2. Exact company-name match (`Apple`).
3. Whole-word name match, then substring on name/symbol.

If nothing matches, `matches` is empty and an `error` explains why. On a network
failure the script still returns JSON with an `error` field (never crashes).

## Notes

- No API key required; the token list is public and cached by CoinGecko.
- Addresses are on **Robinhood Chain** (chainId `4663`) — not Ethereum mainnet.
- The list covers ~228 tokenized stocks/ETFs and a few Robinhood-native tokens.
