# Artifacts and Media

AgentOS can create and deliver files as part of agent work: reports, HTML
files, PDFs, slide decks, spreadsheets, generated images, and other artifacts.
Use artifacts when the output is too large, visual, structured, or important to
leave only in chat text.

## Artifacts

Artifacts are user-visible files created during a session. In Web UI chat they
appear as artifact cards when the runtime publishes them. In CLI runs, artifact
events can include file names, ids, and download URLs.

Common use cases:

- generate a report;
- create a standalone HTML prototype;
- build a CSV/XLSX workbook;
- create a PDF briefing;
- produce a slide deck;
- package generated output for channel delivery.

Ask directly:

```text
Create a one-page HTML dashboard from this data and publish it as an artifact.
```

```text
Generate a PDF briefing with sources and publish the final file.
```

### Download URLs

Clients fetch an artifact or an attached file through a ticketed URL: the
`artifacts.download` RPC returns `/api/v1/artifacts/download/<conn_id>/<ticket>`,
good for five minutes and for as long as the WebSocket connection that asked
for it stays open. The URL needs no header, serves byte ranges and a PNG
thumbnail (`?variant=thumbnail`, still images up to 1200 px), and is served
with a sandboxing `Content-Security-Policy` and `Content-Disposition:
attachment`, so a generated HTML file never runs as a page of the gateway.
The older `/api/v1/artifacts/{id}?sessionKey=` and
`/api/v1/attachments/{sha256}?sessionKey=` URLs still work for this release
(`[artifacts] legacy_session_key_urls`); see
[HTTP API → Ticketed downloads](http-api.md#ticketed-downloads).

## When to Use Artifacts Instead of Chat

Use artifacts for:

- files the user should download or share;
- tables or reports that need layout;
- generated apps, dashboards, or prototypes;
- long output that would be awkward in chat;
- channel delivery where the platform supports file upload.

Use chat text for short answers, decisions, and next steps.

## Inline Charts and Cards

Most artifacts render in Web UI chat as a download chip; images and audio render
inline. Two more mimes render inline:

| Mime | Rendered as |
|------|-------------|
| `application/vnd.agentos.chart+json` | Candlestick chart with a volume histogram |
| `application/vnd.agentos.cards+json` | Responsive grid of record cards |

There is nothing to register: any skill gets one by publishing a file with that
mime. Two steps, and both have a failure mode worth knowing.

**`exec_command` publishes these two mimes for you.** When a command's stdout
contains a line that is exactly

```
publish_artifact path=<file> mime=application/vnd.agentos.<something>+json nonce=<nonce>
```

where `<nonce>` is the value `exec_command` put in that command's
`AGENTOS_INLINE_ARTIFACT_NONCE` environment variable, the artifact is published
automatically and the marker is replaced with a note
telling the model not to publish it again. Print it with
`agentos.skill_stdio.inline_artifact_marker(path, mime)`, which adds the nonce.
The nonce is fresh for every command, so a marker-shaped line inside data the
command merely prints — a token symbol read off a chain, an API response —
cannot trigger a publish; a marker without this command's nonce stays plain
text. Before this, any matching line published, which let an attacker-chosen
ERC-20 symbol push a workspace file at the user as a rendered card.

Auto-publishing is deliberate: leaving the call
to the model meant the file got written and the UI never drew, because the model
would answer with a hand-written table instead. Only the
`application/vnd.agentos.` family auto-publishes — a plain file still needs an
explicit `publish_artifact` call, so ordinary command output cannot push a
workspace file at the user. Path containment is enforced by `publish_artifact`
itself, unchanged.

Write the marker on its own line: prose that merely mentions it is ignored.

**1. Write the JSON inside the workspace.** Scripts run with the workspace as
their working directory, so a bare filename lands in the right place.
`publish_artifact` rejects anything outside the workspace, so an absolute path
like `/tmp/chart.json` fails *after* the data has already been fetched.

**2. Publish it with the mime spelled out.**

```
publish_artifact path=bonk-1h.chart.json mime=application/vnd.agentos.chart+json
```

Passing `mime` is what makes the chart appear. Omit it and the filename guess
returns `application/json`, which classifies as a data artifact and renders as
an ordinary download chip — no error, just no chart. Tell the model to pass it
explicitly in your SKILL.md.

The body is:

```json
{
  "type": "candlestick",
  "title": "BONK · 1h",
  "subtitle": "SOL · 1h",
  "candles": [
    { "time": 1754380800, "open": 1.2e-6, "high": 1.5e-6, "low": 1.1e-6,
      "close": 1.4e-6, "volume": 12500.5 }
  ]
}
```

`time` is a Unix timestamp in **seconds** and `volume` is optional; every other
candle field is required. Rows may arrive in any order and may repeat a
timestamp — the renderer sorts them and keeps the last entry per timestamp.
`title` and `subtitle` are display-only text, never markup.

### Card grids

Use cards when the answer is a handful of small records rather than a series —
a token lookup, a holder list, a search result. They stay readable where a
markdown table would not: a 42-character contract address puts a table into a
horizontal scroll, while a card gives it its own line and a copy button.

```json
{
  "type": "cards",
  "title": "Robinhood Chain — Apple",
  "subtitle": "optional caveat carried under the heading",
  "cards": [
    {
      "title": "AAPL",
      "subtitle": "Apple",
      "logo": "https://assets.example/aapl.png",
      "badge": "verified",
      "badgeTone": "positive",
      "fields": [
        { "label": "Address", "value": "0xaf3d…93f9", "copyable": true },
        { "label": "Chain", "value": "4663" }
      ]
    }
  ]
}
```

Only `cards` and each card's `title` are required. `badgeTone` is one of
`positive` / `warning` / `danger` / `neutral`, and anything else falls back to
`neutral`, so a skill can ship a new status without waiting on a frontend
release. `copyable` renders the value in monospace with a copy button. `logo`
must be an `http(s)` URL — other schemes are dropped rather than sanitised.

At most 24 cards render; the rest are counted and reported under the grid
instead of being dropped silently. Every string is display-only text, never
markup — see `frontend/src/views/chat/transcript/cards.ts`.

`robinhood-rwa-addresses` is the worked example: `scripts/rwa_cards.py` reads
the lookup's JSON on stdin and writes this payload.

Hovering a candle reveals its open, high, low, close, volume, and the move from
open to close as a percentage, so there is no need to repeat those numbers in
the reply.

The `gmgn-market` and `gmgn-token` skills each ship `scripts/kline_chart.py`,
which converts GMGN kline output into this shape; use it as a worked example
when adding charts to another skill. Note that they carry a copy each rather
than sharing one: `{baseDir}` resolves to the skill that is running, so a skill
cannot reach into another skill's `scripts/` directory — and the skill it
pointed at may not even be enabled. Give your skill its own copy.

Charts render in the Web UI only. Other surfaces receive the artifact as a
normal JSON file, so keep a short text summary in the reply alongside the chart.

## Document Skills

AgentOS includes skills for common document formats:

- `docx` for Word documents;
- `pptx` for PowerPoint decks;
- `xlsx` for Excel workbooks;
- `pdf-toolkit` for structured PDF work;
- `html-to-pdf` for styled PDF rendering.

Discover them:

```sh
agentos skills search pdf
agentos skills view pptx
agentos skills view xlsx
```

Some document features require optional native/system dependencies. Use
`agentos skills list` and `agentos doctor` to check readiness.

## Image Input and Generation

In terminal chat, send an image for analysis:

```text
/image /path/to/screenshot.png Describe what is wrong with this UI.
```

Configure image generation:

```sh
agentos configure image-generation
```

Then ask for images in chat:

```text
Generate a clean product mockup image for this landing page.
```

Image provider support depends on configured provider credentials, optional
dependencies, and runtime policy.

## Text to Speech and Media Helpers

The media tool family includes image, PDF, and TTS helpers. Availability can
depend on provider config, optional dependencies, and runtime policy.

Use media helpers when the requested output is naturally a file or asset rather
than a plain text answer.

## Channel Delivery

Channels differ in file-size limits, threading behavior, and upload APIs. If a
channel cannot deliver an artifact directly, use the Web UI artifact card or
session export as the recovery surface.

For channel setup, see [`channels.md`](channels.md).

## Troubleshooting

If an artifact does not appear:

1. Check the chat or CLI output for artifact events.
2. Open the Web UI session and inspect artifact cards.
3. Export the session if you need durable evidence:

   ```sh
   agentos sessions export <session-key>
   ```

4. Run `agentos doctor` if a document or media dependency appears missing.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
