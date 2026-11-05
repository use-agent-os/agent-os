# syntax=docker/dockerfile:1.6
#
# S20 — AgentOS container image.
#
# Safety contract:
#   * Inside the container the gateway binds to 0.0.0.0 because the Docker
#     network namespace needs a wildcard bind for `-p host:container`
#     publishing to work. The defense-in-depth lives at the HOST-SIDE `-p`
#     binding: the documented default `docker run -p 127.0.0.1:18791:18791`
#     keeps the gateway reachable only from the host's loopback.
#   * Network exposure is opt-in via `-p 0.0.0.0:18791:18791` — see the
#     "Network exposure" section in README.md for the warning.
#   * The S19 boot WARNING (`gateway.bind.public`) fires on every container
#     start because the in-container bind is a wildcard by design — that is
#     the intended signal to operators running the image.

FROM node:22-bookworm-slim AS control-ui-builder

WORKDIR /build

# The shared builder is Python stdlib-only. The console's own node_modules stay
# in this stage; the runtime image takes only node and npm from it, below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

COPY frontend/ ./frontend/
COPY scripts/build_control_ui.py ./scripts/build_control_ui.py

RUN python3 scripts/build_control_ui.py build


FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# --- safety default ---------------------------------------------------------
# AGENTOS_LISTEN=0.0.0.0 is required inside the container so the gateway can
# be reached via Docker port publishing. Do NOT flip this to 127.0.0.1 —
# that would make the container reachable only from itself. The safe
# default for HOST-side exposure lives at `docker run -p`, not here.
ENV AGENTOS_LISTEN=0.0.0.0 \
    AGENTOS_GATEWAY_PORT=18791

WORKDIR /app

# Build tooling for optional C-extension deps (jieba FTS5 tokenizer, etc.),
# plus the tools the agent's git tools, shell and skills expect on PATH.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        gh \
        git \
        jq \
        ripgrep \
        tmux \
    && rm -rf /var/lib/apt/lists/*

# Node and npm for skills, MCP servers and coding-agent harnesses. Debian
# bookworm's nodejs is Node 18, so take the version the console is built with.
COPY --from=control-ui-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=control-ui-builder \
    /usr/local/lib/node_modules/npm/ \
    /usr/local/lib/node_modules/npm/
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Copy minimal build context — everything else is in .dockerignore.
COPY pyproject.toml README.md README.release.md LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY --from=control-ui-builder \
    /build/src/agentos/gateway/static/dist/ \
    ./src/agentos/gateway/static/dist/

RUN python - <<'PY'
from pathlib import Path

root = Path("src/agentos/memory/models/bge_onnx")
pilot = Path("src/agentos/agentos_router/models/pilot_v1")
minilm = Path("src/agentos/memory/models/embeddings/all-MiniLM-L6-v2-int8")
required = [
    root / "model.onnx",
    # pilot-v1 router bundle + its MiniLM encoder: without these every turn
    # silently degrades to the default tier.
    pilot / "model.onnx",
    pilot / "manifest.json",
    minilm / "model.onnx",
]
pointer = "version https://git-lfs.github.com/spec/v1"
missing = [str(path) for path in required if not path.is_file()]
pointers = []
for path in required:
    if not path.is_file():
        continue
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if lines and lines[0].strip() == pointer:
        pointers.append(str(path))
if missing or pointers:
    raise SystemExit(
        "Bundled BGE embedding or Pilot router assets are unavailable in this "
        'build context. Run `git lfs pull --include="src/agentos/memory/models/**"` '
        'and `git lfs pull --include="src/agentos/agentos_router/models/**"` '
        f"before docker build. Missing={missing} Pointers={pointers}"
    )
PY

RUN pip install ".[recommended]" "uv==0.13.7"

# Persisted state root. The gateway writes config, state, logs, and the
# workspace under AGENTOS_STATE_DIR — mounting a volume here (see
# compose.yaml) is what makes a container's setup survive a recreate.
ENV AGENTOS_STATE_DIR=/var/lib/agentos

# Tools installed at runtime have to survive that recreate too, and the user
# below cannot write to /usr/local. Home lives on the volume, so `npm install -g`
# and `uv tool install` land in ~/.local/bin and a tool's own login or config
# stays with it. That directory comes last on PATH: an image upgrade replaces
# what the image ships, and nothing on the volume can shadow it.
ENV NPM_CONFIG_PREFIX=/var/lib/agentos/home/.local \
    PATH=$PATH:/var/lib/agentos/home/.local/bin

# Run as a non-root user — avoids shipping root creds into production.
# The state root is created and owned by that user before the USER drop,
# so a freshly initialized volume inherits writable, non-root ownership.
RUN mkdir -p /var/lib/agentos \
    && useradd --create-home --home-dir /var/lib/agentos/home \
        --uid 10001 --shell /usr/sbin/nologin agentos \
    && chown -R agentos:agentos /app /var/lib/agentos
USER agentos

EXPOSE 18791

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:18791/healthz || exit 1

# A volume created by an older image has no home directory yet, and Docker only
# seeds a volume from the image when it is empty. A failure here must not stop
# the gateway: an unwritable bind mount is reported by the gateway itself.
ENTRYPOINT ["/bin/sh", "-c", "mkdir -p \"$HOME\" 2>/dev/null; exec agentos \"$@\"", "agentos"]
CMD ["gateway", "run"]
