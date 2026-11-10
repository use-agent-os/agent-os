# X (Twitter) Search

The `x_search` tool searches X posts, profiles, and threads. It is backed by
xAI's server-side `x_search` tool on the Responses API at
`https://api.x.ai/v1/responses`: Grok runs the search against X's index and
returns a synthesized answer with citations to the posts it used.

Reach for it instead of `web_search` when you want current discussion,
reactions, or claims **on X**. General web pages still belong to `web_search`
and `web_fetch`.

## What it is not

- **Not a search provider.** `agentos search list` covers the providers behind
  `web_search`, which return ranked pages. `x_search` returns an answer plus
  citations, so it is a separate tool and cannot be selected as a `web_search`
  backend.
- **Not a write path.** It cannot post, reply, like, DM, upload media, delete,
  or read your authenticated X account. AgentOS ships no authenticated X
  surface; an `x_search` answer is never evidence that anything was written.
- **Not model-agnostic.** Only xAI has access to X's post index. Your agent can
  run on any provider — the tool makes its own call to xAI regardless.
- **Not the X developer platform.** `developer.x.com` sells access to the X API
  (raw posts, writes) and bills separately. This tool talks to `x.ai`.

## Credentials

Two paths. **OAuth wins when both are present**, because it spends a
subscription you already pay for instead of API credit.

| Path | How | `credential_source` |
| --- | --- | --- |
| SuperGrok / X Premium+ | `agentos auth login xai` | `xai-oauth` |
| xAI API key | `XAI_API_KEY` in `~/.agentos/.env`, or `agentos configure x-search --api-key <key>` | `xai` |

### Signing in with a subscription

```sh
agentos auth login xai      # device-code flow: open a URL, enter a code
agentos auth status         # never prints a token
agentos auth logout xai
```

For a caller that cannot hold a terminal open for minutes — a chat agent, a
script — the same flow splits in two:

```sh
agentos auth login xai --no-wait --json 2>/dev/null   # -> loginId, verificationUri, userCode
agentos auth login xai --resume --json 2>/dev/null    # exit 0 done, 3 not yet, 1 failed
```

`--resume` picks the newest pending login unless given `--login-id`. Pending
logins live in the token store, so the half that starts one and the half that
finishes it can be different processes.

The login is a device-code grant against `auth.x.ai`. Tokens land in
`~/.agentos/auth.json` (owner-only, `0600`) and refresh themselves; you are not
asked to paste anything into a prompt.

Two things worth knowing:

- **A subscription is not automatically entitled to API access.** xAI restricts
  API/OAuth use to certain SuperGrok tiers, and an account outside them gets
  HTTP 403 on refresh even though the in-app subscription is active. AgentOS
  reports that as a tier problem rather than telling you to log in again,
  because logging in again cannot fix it.
- **A broken login is reported, not skipped.** If OAuth is configured but
  unusable, `x_search` says so instead of quietly falling back to an API key
  you may not have set.

### Client identity

The device-code flow uses xAI's public Grok CLI client id — the OAuth scope
literally reads `grok-cli:access` — because xAI publishes no self-service client
registration. It is a public client with no secret. Override it with
`AGENTOS_XAI_OAUTH_CLIENT_ID` if you have your own registration.

### Browser sign-in

Settings › Search can run the same flow: **X search → Sign in with xAI**. It shows the approval link and the code, then polls until
you approve. Once signed in the same control becomes **Sign out of xAI**. The
card reports which credential is currently in play, so a key configured
alongside a login does not look like the one being used.

Signing out only forgets the local tokens; nothing is revoked at xAI, and
`x_search` falls back to `XAI_API_KEY` if one is set.

Without a reachable credential the tool is removed from the model's schema
entirely. That is deliberate: every tool schema is fixed overhead on every
provider call, so an install with no xAI key should not pay for this one. It
also means "the agent says it has no x_search tool" is the expected behaviour
before setup, not a bug.

`agentos context` still lists and prices `x_search`. That command reports what
each *profile* would cost across the whole registry and does not model
credential gating — `image_generate` appears there the same way without an
image provider. The live per-turn surface is the one that drops it.

## Cost

`x_search` bills your xAI account directly. It does not pass through the
AgentOS provider layer, so the spend does **not** appear in `agentos cost` or
the Usage view. Watch it in the xAI console.

## Configure

```sh
agentos onboard catalog x-search
agentos configure x-search --api-key-env XAI_API_KEY
agentos configure x-search --x-search-model grok-4.5 --x-search-reasoning-effort low
agentos configure x-search --no-x-search-enabled
```

Settings › Search carries the same fields under X search, with an **Enable X
search** switch and an **API key environment variable** field.
Saving applies immediately; no gateway restart.

```toml
[x_search]
enabled = true
# Any Grok model with access to xAI's server-side x_search tool.
model = "grok-4.5"
base_url = "https://api.x.ai/v1"
api_key = ""
api_key_env = "XAI_API_KEY"
# "", low, medium, high, or xhigh. Empty uses the model's own default;
# xhigh is only accepted by models that document it.
reasoning_effort = ""
# One attempt. A complex X search runs 60-120s.
timeout_seconds = 180.0
# Hard wall for the whole call including retries.
total_timeout_seconds = 300.0
# Retried on 5xx, timeout, and connection errors only.
retries = 2
```

`base_url` exists for an HTTPS proxy that speaks xAI's Responses API. A
non-HTTPS URL, or one pointing at a cloud metadata endpoint, is rejected and
the default is used — the request carries a bearer token.

## Tool parameters

| Parameter | Type | Description |
| --- | --- | --- |
| `query` | string (required) | What to look up on X. |
| `allowed_x_handles` | string[] | Include these handles exclusively (max 10). A leading `@` is stripped. |
| `excluded_x_handles` | string[] | Exclude these handles (max 10). Cannot be combined with `allowed_x_handles`. |
| `from_date` | string | `YYYY-MM-DD` start date. |
| `to_date` | string | `YYYY-MM-DD` end date. |
| `enable_image_understanding` | boolean | Ask xAI to analyze images attached to matching posts. |
| `enable_video_understanding` | boolean | Ask xAI to analyze videos attached to matching posts. |

Dates are checked before the HTTP call. A malformed value, an inverted range,
or a `from_date` in the future fails immediately rather than spending a
billable call that could only return nothing. A future `to_date` is allowed —
"from yesterday to tomorrow" is a legitimate way to catch posts as they arrive.

## Result

```json
{
  "success": true,
  "provider": "xai",
  "credential_source": "xai",
  "tool": "x_search",
  "model": "grok-4.5",
  "query": "reactions to the new Grok image features",
  "answer": "...",
  "citations": ["https://x.com/..."],
  "inline_citations": [{ "url": "...", "title": "...", "start_index": 0, "end_index": 42 }],
  "degraded": false,
  "degraded_reason": null
}
```

### `degraded` is the field that matters

xAI answers with HTTP 200 and a fluent, confident answer even when its X index
matched nothing for your filters. That answer comes from the model's training
data and is indistinguishable from a real one by shape alone.

`degraded` is `true` when a narrowing filter (`allowed_x_handles`,
`excluded_x_handles`, `from_date`, `to_date`) was active **and** both citation
channels came back empty. Treat that answer as unsourced: it is not something
found on X. A broad query with no citations is not degraded — it is just an
answer.

Common causes:

- A typo in a handle, or an account that does not exist.
- A date range too narrow, or sliding past the posts you wanted.
- An index gap. Some active accounts intermittently fail to surface even when
  they post regularly; retry after a few minutes.

## Policy and limits

- Member of `group:web`, so `deny = ["group:web"]` cuts the route to api.x.ai
  along with the other network tools.
- On the cron allowlist, next to `web_fetch` and `web_search` — a scheduled job
  watching X is the obvious use, and the tool can only read.
- Classified `external` for result budgeting, so a long answer is trimmed under
  the same per-turn ceiling as other network results.
- Runs under the `web.fetch` sandbox action kind.

## Troubleshooting

**The agent says it has no `x_search` tool.** No credential resolved. Check
`agentos auth status`, `agentos env list` for `XAI_API_KEY`, or that
`[x_search] api_key` is set, and that `enabled` is not `false`.

**`xai_oauth_tier_denied`.** The OAuth grant is valid but xAI will not let the
account use the API. Logging in again will not help — upgrade the subscription,
or set `XAI_API_KEY` and use the key path.

**The tool is offered but every call fails after a long break.** The stored
refresh token was revoked or already used. AgentOS clears dead tokens when the
refresh returns 400/401 so the next call fails locally instead of over the
wire; run `agentos auth login xai` again.

**`x_search is not enabled for this model`.** The configured `model` lacks
access to xAI's server-side tool. Switch back to `grok-4.5` or another Grok
model that documents it.

**Timeouts.** `timeout_seconds` bounds one attempt; `total_timeout_seconds`
bounds the whole call. Retries stop when the remaining budget cannot fit
another attempt, so raising `retries` alone does nothing unless the total goes
up too.

## See also

- [`search.md`](search.md) — general web search providers.
- [`tools-and-sandbox.md`](tools-and-sandbox.md) — the built-in tool catalog.
- [`configuration.md`](configuration.md) — every config key.
