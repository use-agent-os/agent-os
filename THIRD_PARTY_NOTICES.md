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
- The vendored front-end JavaScript libraries served from
  `src/agentos/gateway/static/vendor/` for the Web UI console (Markdown
  rendering, HTML sanitization, and code-block syntax highlighting).

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

## AgentOS-original bundled skills

These bundled skill descriptors are authored and maintained by AgentOS and
are released under AgentOS's repository license (Apache-2.0; see `LICENSE`):

- `agentos`
- `cron`
- `deep-research`
- `docx`
- `git-diff`
- `github`
- `history-explorer`
- `html-to-pdf`
- `http-fetch`
- `memory`
- `multi-search-engine`
- `nano-pdf`
- `pdf-toolkit`
- `pptx`
- `robinhood-rwa-addresses`
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
- `weather`
- `xlsx`
- `advanced-dubbing-studio`
- `music-and-singing-studio`
- `voice-clone-lab`
- `voice-conversion-studio`
- `voiceover-studio`

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

## Hermes Agent reference material

- Component: cron prompt-injection scanner reference material.
- Upstream project: https://github.com/NousResearch/hermes-agent
- License: MIT
- Copyright notice: Copyright (c) 2025 Nous Research

AgentOS does not redistribute Hermes Agent. This notice records conservative
attribution for reference material reviewed while hardening AgentOS's cron
prompt scanner.

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

## Vendored Web UI JavaScript libraries

- Component: `marked.min.js` (Markdown parser).
- Upstream project: https://github.com/markedjs/marked
- Version bundled: v15.0.7
- License: MIT
- Copyright notice: Copyright (c) 2011-2025, Christopher Jeffrey.

- Component: `purify.min.js` (DOMPurify, HTML sanitizer).
- Upstream project: https://github.com/cure53/DOMPurify
- Version bundled: 3.2.5
- License: Apache License 2.0 and Mozilla Public License 2.0 (dual-licensed;
  upstream permits either).
- Copyright notice: Copyright (c) Cure53 and other contributors.

- Component: `prism-core.min.js`, `prism-autoloader.min.js`, and the
  per-language grammars under `prism-langs/` (Prism, syntax highlighting).
- Upstream project: https://github.com/PrismJS/prism
- License: MIT
- Copyright notice: Copyright (c) 2012 Lea Verou.

These libraries are served as static assets to the browser for the Web UI
console (`src/agentos/gateway/templates/index.html`); AgentOS does not modify
their source and loads the upstream-minified builds as-is.
