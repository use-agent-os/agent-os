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

# The shared builder is Python stdlib-only. Keeping the Node toolchain in this
# stage ensures the final runtime image contains neither npm nor node_modules.
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

# Build tooling for optional C-extension deps (jieba FTS5 tokenizer, etc.).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

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

RUN pip install ".[recommended]"

# Persisted state root. The gateway writes config, state, logs, and the
# workspace under AGENTOS_STATE_DIR — mounting a volume here (see
# compose.yaml) is what makes a container's setup survive a recreate.
ENV AGENTOS_STATE_DIR=/var/lib/agentos

# Run as a non-root user — avoids shipping root creds into production.
# The state root is created and owned by that user before the USER drop,
# so a freshly initialized volume inherits writable, non-root ownership.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin agentos \
    && mkdir -p /var/lib/agentos \
    && chown -R agentos:agentos /app /var/lib/agentos
USER agentos

EXPOSE 18791

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:18791/healthz || exit 1

ENTRYPOINT ["agentos"]
CMD ["gateway", "run"]
