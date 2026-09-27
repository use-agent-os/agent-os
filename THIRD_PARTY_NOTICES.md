# Third Party Notices

This file records third-party attribution for code and assets bundled with
AgentOS. It covers:

- Core runtime modules derived from OpenSquilla (Apache-2.0); see the
  first section below. Releases up to and including the v2026.7.19 release tag additionally
  shipped the V4 Phase 3 router engine module
  (`src/agentos/agentos_router/v4_phase3.py`) and the V4 Phase 3 model bundle
  (OpenSquilla-trained weights and inference code); both have since been
  removed from the tree — see the first section below for the historical
  record.
- The bundled skill descriptors under `src/agentos/skills/bundled/`, which
  include OpenClaw-derived MIT descriptors and AgentOS-original descriptors.
- The bundled pptx skill references the python-pptx and PptxGenJS libraries;
  AgentOS does not vendor those libraries, but the skill instructs the
  agent runtime to invoke them and is documented here for transparency.
- The bundled BGE (bge-small-zh-v1.5) ONNX export under
  `src/agentos/memory/models/bge_onnx/`, used by local memory embedding
  (historically also by the V4 Phase 3 router's BGE feature channel).
- The bundled all-MiniLM-L6-v2 INT8 ONNX export under
  `src/agentos/memory/models/embeddings/all-MiniLM-L6-v2-int8/`, used by the
  Pilot router's feature builder.
- The checked-in Pilot router golden evaluation set under
  `tests/test_agentos_router/data/pilot_golden.jsonl`, whose rows are derived
  from the WildChat-1M dataset (ODC-BY 1.0).
- The built-in tokenjuice tool-result projection backend and bundled
  reduction rules under `src/agentos/plugins/tokenjuice/`.
- The cron prompt-injection scanner was reviewed against Hermes Agent
  reference material; the MIT notice is reproduced below for conservative
  attribution.
- The production dependencies and font assets bundled into the React Control
  UI. Exact package versions and complete upstream license texts are generated
  from `frontend/package-lock.json` plus the checked-in font licenses into the shipped
  `static/dist/THIRD_PARTY_LICENSES.txt`.

## OpenSquilla-derived core modules

- Component: core runtime modules under `src/agentos/`, and (in releases up
  to and including the v2026.7.19 release tag) the V4 Phase 3 local ML router bundle (trained
  model weights and inference code) under
  `src/agentos/agentos_router/models/v4.2_phase3_inference/`.
- Upstream project: https://github.com/opensquilla/opensquilla
- License: Apache License 2.0
- Copyright notice: OpenSquilla contributors (the upstream project ships
  the stock Apache-2.0 text without a filled-in copyright line and no
  NOTICE file).

AgentOS is built on OpenSquilla. Parts of the AgentOS core were copied
from and then substantially modified relative to the upstream project.
The highest-overlap modules include:

- `src/agentos/application/approval_queue.py` and
  `src/agentos/gateway/approval_queue.py` — approval queue handling
- `src/agentos/cli/agent_cmd.py` — agent CLI command surface
- `src/agentos/channels/command_registry.py` — channel slash-command dispatch
- `src/agentos/gateway_client.py` and
  `src/agentos/cli/gateway_client.py` — gateway client plumbing
- `src/agentos/agentos_router/v4_phase3.py` — router phase logic (shipped in
  releases up to and including the v2026.7.19 release tag; since removed from
  the tree with the rest of the V4 Phase 3 router). Its tier-resolution helper
  survives in modified form as `src/agentos/agentos_router/tiers_util.py`,
  which remains OpenSquilla-derived.

### V4 Phase 3 router bundle (model weights and inference code) — historical

Releases up to and including the v2026.7.19 release tag bundled the local ML router assets
under `src/agentos/agentos_router/models/v4.2_phase3_inference/`. That bundle
is OpenSquilla's, carried over from upstream
`src/opensquilla/squilla_router/models/v4.2_phase3_inference/`. It is **not**
trained or authored by the AgentOS contributors. The bundle and the
OpenSquilla-derived engine module `src/agentos/agentos_router/v4_phase3.py`
have since been removed from the tree and no longer ship in the wheel (the
default router strategy is the AgentOS-trained `pilot-v1`). This notice is
retained for the releases that shipped them, which covered:

- `lgbm_main.bin` and `lgbm_aux.bin` — LightGBM boosters for the router heads.
- `mlp/model.onnx` and `mlp/scaler.joblib` — the PyTorch-exported MLP head and
  its scaler.
- `features/tfidf.pkl`, `features/svd.pkl`, `features/config.pkl`, and
  `features/bge_pca.joblib` — fitted scikit-learn/joblib feature artifacts.
- `runtime_src/src/router/**` — the inference core the router loads at runtime.
- `router.runtime.yaml`, `version.json`, `inference_manifest.json` — runtime
  configuration and inference metadata.

The weights were used byte-for-byte unmodified: `lgbm_main.bin` carried the
same Git LFS object as upstream
(`sha256:5f312db09577bbaf30f87358941974eef6edce7f1424d0e9de21cbd38a646d53`,
39684725 bytes). Modifications made by the AgentOS contributors were limited to
namespace/branding renames, and to `runtime_src/.../inference/artifacts.py`,
which resolved the BGE export from the shared
`src/agentos/memory/models/bge_onnx/` location so it shipped once instead of
twice. The bundle's `PROVENANCE.md` (present in the releases that shipped it,
and in this repository's git history) records the per-file detail.

Note that only the BGE embedding channel was third-party relative to
OpenSquilla (MIT; see the BAAI section below). The routing decision itself
came from OpenSquilla's own trained LightGBM and MLP heads.

Other modules across the runtime may also contain OpenSquilla-derived
code in modified form. In accordance with Section 4(b) of the Apache
License 2.0, this notice records that the derived files have been
modified by the AgentOS contributors. The entire AgentOS repository is
licensed under the Apache License 2.0 (see `LICENSE`), so the upstream
license terms apply uniformly; the full license text is included in the
`LICENSE` file at the repository root.

## OpenClaw-derived bundled skill descriptors

- Component: SKILL.md frontmatter and instruction text for these bundled skills:
  - `sub-agent`
  - `cron`
  - `github`
  - `nano-pdf`
  - `summarize`
  - `tmux`
  - `weather`
- Upstream project: https://github.com/openclaw/openclaw
- License: MIT
- Copyright notice: Copyright (c) 2025 Peter Steinberger

Note: the `sub-agent` descriptor retains OpenClaw upstream lineage and MIT
attribution.

The descriptor text instructs the agent runtime how to use built-in skill
surfaces and external tools; AgentOS does not redistribute third-party CLIs
through these descriptors. Per the MIT license, the upstream copyright and
permission notice are reproduced below in their entirety and apply to the
OpenClaw-derived bundled descriptor files.

```
MIT License

Copyright (c) 2025 Peter Steinberger

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## GMGN-derived bundled skill descriptors

- Component: SKILL.md instruction text for these bundled skills, plus
  `gmgn-holder-analysis/scripts/analyze.py`,
  `gmgn-wallet-analysis/scripts/analyze.py`, and
  `gmgn-wallet-score/scripts/score.py`:
  - `gmgn-cooking`
  - `gmgn-holder-analysis`
  - `gmgn-market`
  - `gmgn-portfolio`
  - `gmgn-swap`
  - `gmgn-token`
  - `gmgn-track`
  - `gmgn-wallet-analysis`
  - `gmgn-wallet-score`
- Upstream project: https://github.com/GMGNAI/gmgn-skills
- License: MIT
- Copyright notice: Copyright (c) 2025 GMGN

These descriptors drive the third-party `gmgn-cli` npm package, which AgentOS
does **not** redistribute — an operator installs it themselves and supplies
their own `GMGN_API_KEY`. Only the descriptor text and the helper scripts are
vendored here; AgentOS added the `provenance` and `metadata.agentos` frontmatter
blocks, re-pointed the helper-script paths at `{baseDir}`, folded the
`gmgn-wallet-score` description into a YAML block scalar so its frontmatter
parses, and lifted that skill's upstream inline analyzer into
`scripts/score.py` (argv in place of the `<FILL_IN_*>` placeholders), matching
what was already done for `gmgn-holder-analysis`. The two wallet skills'
`description` fields were also shortened to the length of their bundled
siblings; upstream's run roughly twice as long and did not fit the shipped
skills-prompt budget. Per the MIT license, the upstream copyright and permission notice
are reproduced below in their entirety and apply to the GMGN-derived bundled
files.

```
MIT License

Copyright (c) 2025 GMGN

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## AgentOS-original bundled skills

These bundled skill descriptors are authored and maintained by AgentOS and
are released under AgentOS's repository license (Apache-2.0; see `LICENSE`):

- `agentos`
- `cron`
- `cron-watchers`
- `deep-research`
- `docx`
- `git-diff`
- `github`
- `history-explorer`
- `html-to-pdf`
- `http-fetch`
- `memory`
- `multi-search-engine`
- `musebook`
- `musedin`
- `nano-pdf`
- `pdf-toolkit`
- `poolsdotfun-token-launcher`
- `pptx`
- `robinhood-agentic-trading`
- `robinhood-chain-stocks`
- `robinhood-rwa-addresses`
- `senior-unilp-manager`
- `stack-trace-generic-probe`
- `stack-trace-go-probe`
- `stack-trace-js-probe`
- `stack-trace-python-probe`
- `stack-trace-rust-probe`
- `sub-agent`
- `srt-from-script`
- `subtitle-burner`
- `summarize`
- `text-file-read`
- `title-card-image`
- `tmux`
- `video-still-animator`
- `token-burner`
- `wallet-trading`
- `weather`
- `xlsx`
- `advanced-dubbing-studio`
- `music-and-singing-studio`
- `voice-clone-lab`
- `voice-conversion-studio`
- `voiceover-studio`

### Vendored protocol specification in `musebook`

The `musebook` skill descriptor and its `scripts/muse.py` are AgentOS-original.
`references/muse.txt` is a verbatim copy of the onboarding musebook.lol
publishes for agents (https://musebook.lol/muse.txt), retrieved 2026-09-17. It
is redistributed unmodified as the authoritative description of a public wire
protocol and carries no license header of its own.

### Vendored protocol specification in `musedin`

The `musedin` skill descriptor and its `scripts/musedin.py` are AgentOS-original.
`references/muse.txt` is a verbatim copy of the onboarding musedin.com
publishes for agents (https://musedin.com/muse.txt), retrieved 2026-09-26,
included with the permission of MusedIn's owner. It is redistributed
unmodified as the authoritative description of a public wire protocol and
carries no license header of its own.

## tokenjuice adapted reduction rules

- Component: built-in tokenjuice tool-result projection backend and bundled
  reduction rules under `src/agentos/plugins/tokenjuice/`.
- Upstream project: https://github.com/vincentkoc/tokenjuice
- License: MIT
- Copyright notice: Copyright (c) 2026 Vincent Koc

AgentOS includes a Python adaptation of tokenjuice's rule-driven reducer
and bundles reduction rules derived from the upstream project. AgentOS does
not depend on the upstream tokenjuice npm package at runtime. Additional
provenance is recorded in
`src/agentos/plugins/tokenjuice/PROVENANCE.md`; the MIT license text is
also shipped with that package as `LICENSE.tokenjuice`.

```
MIT License

Copyright (c) 2026 Vincent Koc

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Hermes Agent derived material

- Components:
  - `src/agentos/tools/builtin/x_search.py` — adapted from
    `tools/x_search_tool.py`. The tool's request shape, client-side date
    validation, citation extraction, and degraded-result semantics follow the
    upstream implementation; the HTTP client, retry budgeting, and AgentOS
    wiring are original.
  - `src/agentos/xai_oauth.py` — adapted from the xAI OAuth section of
    `hermes_cli/auth.py`. The device-code flow, OIDC discovery, endpoint and
    inference-origin pinning, refresh-skew heuristics, tier-denial (HTTP 403)
    handling, and dead-token quarantine follow the upstream implementation;
    the token store, async refresh path, and network-free availability check
    are original.
  - `src/agentos/tools/browser_supervisor.py` — adapted from
    `tools/browser_supervisor.py`. The CDP dialog-interception model
    (must_respond / auto_dismiss / auto_accept), the pending-dialog state
    machine, console capture, and the per-session supervisor registry follow the
    upstream implementation; the pluggable transport, WebSocket loop, and
    AgentOS wiring are original.
  - `src/agentos/tools/browser_eval_policy.py` — adapted from the eval-policy
    helpers in `tools/browser_tool.py` (`_enforce_browser_eval_policy`,
    `_risky_browser_eval_reason`, `_sensitive_browser_eval_token_reason`,
    `_expression_targets_private_url`, `_redact_browser_output`). The risky-
    primitive denylist, string-literal deobfuscation, URL-literal SSRF pre-scan,
    and output redaction follow the upstream implementation.
  - `src/agentos/tools/agent_browser.py` and
    `src/agentos/tools/builtin/browser.py` — original AgentOS code that reuses
    the upstream lessons on subprocess env-scrubbing (GHSA-m4m8-xjp4-5rmm),
    CDP-URL redaction, first-open timeouts, and the session model; the engine
    invoked (`agent-browser`) is the same one Hermes uses.
  - Cron prompt-injection scanner reference material (reviewed, not copied).
- Upstream project: https://github.com/NousResearch/hermes-agent
- License: MIT
- Copyright notice: Copyright (c) 2025 Nous Research

AgentOS does not redistribute Hermes Agent as a whole.

```
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## ClawHub-derived bundled skill descriptors

- Component: SKILL.md frontmatter and instruction text for these bundled skills:
  - `ai-video-script`
  - `deep-research`
  - `docx`
  - `html-to-pdf`
  - `multi-search-engine`
  - `nano-banana-pro`
  - `pdf-toolkit`
  - `pptx`
  - `seedance-2-prompt`
  - `video-merger`
  - `xlsx`
- Upstream registry: https://clawhub.ai
- License: MIT-0 (Public-domain-equivalent; no attribution required, but
  each skill records its specific upstream slug in its own
  `THIRD_PARTY_NOTICES.md` for transparency)

These bundled skills record their ClawHub source slug in SKILL.md frontmatter
and, when present, the skill-local `THIRD_PARTY_NOTICES.md`. ClawHub's MIT-0
default license permits unlimited use, modification, and redistribution without
attribution.

```
MIT No Attribution

Copyright <YEAR> <COPYRIGHT HOLDER>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## BAAI bge-small-zh-v1.5 / FlagEmbedding

- Component: BAAI/bge-small-zh-v1.5 embedding model and tokenizer assets.
- Upstream model: https://huggingface.co/BAAI/bge-small-zh-v1.5
- Upstream project: https://github.com/FlagOpen/FlagEmbedding
- License: MIT
- Copyright notice: Copyright (c) 2022 staoxiao

The bundled local memory embedding assets contain an ONNX export and tokenizer
files derived from the BAAI bge-small-zh-v1.5 model. The upstream Hugging Face
model card marks
the model as MIT licensed and states that the released models can be used for
commercial purposes free of charge.

MIT License

Copyright (c) 2022 staoxiao

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## sentence-transformers/all-MiniLM-L6-v2

- Component: sentence-transformers/all-MiniLM-L6-v2 embedding model and
  tokenizer assets, bundled as an INT8 ONNX export under
  `src/agentos/memory/models/embeddings/all-MiniLM-L6-v2-int8/`, used by the
  Pilot router's feature builder.
- Upstream model: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
- Upstream project: https://github.com/UKPLab/sentence-transformers
- License: Apache License 2.0
- Copyright notice: sentence-transformers contributors (the upstream model
  card and library ship under the stock Apache-2.0 text without a
  filled-in copyright line).

The bundled export is an INT8 dynamic quantization (onnxruntime,
avx512_vnni, per-channel) of the upstream FP32 ONNX weights, produced by
`scripts/pilot_router/export_embedder.py`. It carries forward the exact
upstream Hugging Face revision and tokenizer recorded in the export's own
`export_meta.json`:

- HF revision: `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`
- Tokenizer (`tokenizer.json`) sha256:
  `da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0`

The entire AgentOS repository is licensed under the Apache License 2.0 (see
`LICENSE`), so the upstream license terms apply uniformly; the full license
text is included in the `LICENSE` file at the repository root.

## WildChat-1M (Pilot router golden evaluation set)

- Component: the Pilot router's checked-in golden evaluation set,
  `tests/test_agentos_router/data/pilot_golden.jsonl`. Its rows are derived from
  the WildChat-1M dataset.
- Upstream dataset: https://huggingface.co/datasets/allenai/WildChat-1M
- Publisher: Allen Institute for AI (AI2)
- License: ODC-BY 1.0 (Open Data Commons Attribution License)
- License text: https://opendatacommons.org/licenses/by/1-0/

The WildChat-1M dataset is released by AI2 under the Open Data Commons
Attribution License (ODC-BY) 1.0. The Pilot router training corpus is sampled
from WildChat-1M, and the corpus rows themselves are **never** committed to this
repository (they live only under the git-ignored `scripts/pilot_router/data/`;
see `scripts/pilot_router/DATA.md` for the full corpus provenance and license
gate). The one WildChat-derived data file that **is** checked in is the golden
evaluation set `tests/test_agentos_router/data/pilot_golden.jsonl` — a small set
of user-turn rows drawn from WildChat and used as a routing-quality regression
fixture.

ODC-BY 1.0 is a permissive open-data license whose substantive obligation is
attribution: any public use of the database or a work produced from it must keep
the attribution notice and the ODC-BY notice intact. It imposes no share-alike /
copyleft requirement and no restriction on commercial use. This notice, together
with the ODC-BY reference above, provides that attribution for the checked-in
WildChat-derived golden-set rows.

## React Control UI production dependencies

The browser bundle is built from the lockfile-pinned packages below. The
release build generates `static/dist/THIRD_PARTY_LICENSES.txt` from the
installed package metadata, upstream package license files, and the checked-in
font licenses. That generated file is included in every wheel and source
distribution alongside the minified browser assets, so the exact copyright
and permission notices shipped by each upstream project remain available even
when multiple packages use the same license.

- `@radix-ui/react-compose-refs@1.1.3` — MIT
- `@radix-ui/react-slot@1.3.0` — MIT
- `@tabler/icons@3.45.0` — MIT
- `@tabler/icons-react@3.45.0` — MIT
- `@tanstack/query-core@5.101.2` — MIT
- `@tanstack/react-query@5.101.2` — MIT
- `class-variance-authority@0.7.1` — Apache-2.0
- `clsx@2.1.1` — MIT
- `cookie@1.1.1` — MIT
- `dompurify@3.4.12` — MPL-2.0 or Apache-2.0
- `fancy-canvas@2.1.0` — MIT
- `framer-motion@12.42.2` — MIT
- `highlight.js@11.11.1` — BSD-3-Clause
- `lightweight-charts@5.2.0` — Apache-2.0
- `lucide-react@1.25.0` — ISC
- `marked@18.0.7` — MIT
- `motion@12.42.2` — MIT
- `motion-dom@12.42.2` — MIT
- `motion-utils@12.39.0` — MIT
- `qrcode-generator@1.5.2` — MIT
- `react@19.2.7` — MIT
- `react-dom@19.2.7` — MIT
- `react-router@7.18.1` — MIT
- `scheduler@0.27.0` — MIT
- `set-cookie-parser@2.7.2` — MIT
- `sonner@2.0.7` — MIT
- `tailwind-merge@3.6.0` — MIT
- `tslib@2.8.1` — 0BSD
- `zustand@5.0.14` — MIT

The type-only `@types/*` and `csstype` packages installed to compile the
TypeScript source do not enter the browser bundle and are intentionally
omitted from this production list.

### Vendored dependency licenses

`fancy-canvas@2.1.0` (a dependency of `lightweight-charts`) publishes a `files`
allowlist covering only its compiled JavaScript and type declarations, so its
MIT text is absent from the npm tarball. The upstream license from
https://github.com/tradingview/fancy-canvas is kept verbatim at
`frontend/vendor-licenses/fancy-canvas-LICENSE.txt`, and the Control UI builder
appends it to the generated ledger marked as vendored.

`qrcode-generator@1.5.2` (the Control UI's local QR renderer) likewise
publishes only its sources, README and type declarations. The MIT text from
https://github.com/kazuhikoarase/qrcode-generator (Copyright (c) 2009 Kazuhiko
Arase) is kept verbatim at
`frontend/vendor-licenses/qrcode-generator-LICENSE.txt` and appended the same
way. Packages that ship no license and have no vendored copy still fail the
build.

### React Control UI fonts

- Inter Variable — Copyright (c) 2016 The Inter Project Authors
  (https://github.com/rsms/inter), SIL Open Font License 1.1.
- JetBrains Mono Variable — Copyright 2020 The JetBrains Mono Project Authors
  (https://github.com/JetBrains/JetBrainsMono), SIL Open Font License 1.1.

The complete upstream notices and OFL 1.1 texts are kept in
`frontend/src/assets/fonts/Inter-LICENSE.txt` and
`frontend/src/assets/fonts/JetBrainsMono-LICENSE.txt`. The shared Control UI
builder appends both files verbatim to the license ledger shipped beside the
font binaries.
