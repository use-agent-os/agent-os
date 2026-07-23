from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).parent.parent


def _load_compose() -> dict:
    return yaml.safe_load((_ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_compose_no_version_field() -> None:
    data = _load_compose()
    assert "version" not in data, (
        "compose.yaml must not have a top-level 'version:' field (use Compose v2 syntax)"
    )


def test_compose_gateway_port_is_loopback() -> None:
    data = _load_compose()
    ports = data["services"]["gateway"]["ports"]
    assert any(
        str(p) == "127.0.0.1:18791:18791" for p in ports
    ), f"Expected '127.0.0.1:18791:18791' in ports, got: {ports}"


def test_compose_gateway_healthcheck_exists() -> None:
    data = _load_compose()
    hc = data["services"]["gateway"].get("healthcheck")
    assert hc is not None, "services.gateway.healthcheck must be defined"


def test_compose_gateway_environment_has_openrouter_key() -> None:
    data = _load_compose()
    env = data["services"]["gateway"].get("environment", {})
    # environment can be a dict or a list of "KEY=VAL" strings
    if isinstance(env, dict):
        assert "OPENROUTER_API_KEY" in env, (
            f"OPENROUTER_API_KEY missing from environment dict: {env}"
        )
    else:
        keys = [item.split("=")[0] for item in env]
        assert "OPENROUTER_API_KEY" in keys, (
            f"OPENROUTER_API_KEY missing from environment list: {env}"
        )


def _load_dockerfile() -> str:
    return (_ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_dockerfile_builds_control_ui_in_node_stage() -> None:
    dockerfile = _load_dockerfile()
    dockerignore = (_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert re.search(
        r"FROM node:22(?:-[^\s]+)? AS control-ui-builder",
        dockerfile,
    )
    assert "RUN python3 scripts/build_control_ui.py build" in dockerfile
    assert (
        "COPY --from=control-ui-builder \\\n"
        "    /build/src/agentos/gateway/static/dist/ \\\n"
        "    ./src/agentos/gateway/static/dist/"
    ) in dockerfile
    assert dockerfile.index("RUN python3 scripts/build_control_ui.py build") < dockerfile.index(
        'RUN pip install ".[recommended]"'
    )
    assert (
        "COPY pyproject.toml README.md README.release.md LICENSE NOTICE "
        "THIRD_PARTY_NOTICES.md ./"
    ) in dockerfile

    assert "frontend/node_modules" in dockerignore
    assert "!scripts/build_control_ui.py" in dockerignore


def test_dockerfile_gateway_port_matches_compose() -> None:
    """Dockerfile's container gateway port must match compose's 18791.

    Drift here — e.g. the Dockerfile keeping EXPOSE 18790 while compose
    publishes 18791 — makes the documented `docker compose` path
    unreachable.
    """
    dockerfile = _load_dockerfile()
    compose = _load_compose()

    ports = [str(p) for p in compose["services"]["gateway"]["ports"]]
    assert any("18791:18791" in p for p in ports), (
        f"compose.yaml must publish 18791:18791, got: {ports}"
    )

    assert "18790" not in dockerfile, (
        "Dockerfile still references the stale 18790 gateway port; "
        "it must use 18791 to match compose.yaml"
    )
    assert "AGENTOS_GATEWAY_PORT=18791" in dockerfile
    assert "EXPOSE 18791" in dockerfile
    assert "http://127.0.0.1:18791/healthz" in dockerfile


def test_compose_persists_state_via_named_volume() -> None:
    """Gateway config and state must persist via a Docker named volume
    mounted at the image's AGENTOS_STATE_DIR.

    The container runs as a non-root user, so a host bind mount to
    /root/.agentos never receives anything the gateway writes — config
    and state would silently vanish on every container recreate.
    """
    compose = _load_compose()
    dockerfile = _load_dockerfile()

    match = re.search(r"AGENTOS_STATE_DIR=(\S+)", dockerfile)
    assert match, "Dockerfile must pin AGENTOS_STATE_DIR for a stable volume target"
    state_dir = match.group(1)
    assert state_dir.startswith("/"), (
        f"AGENTOS_STATE_DIR must be an absolute path, got: {state_dir!r}"
    )

    # This is a static shape test: it pins the Dockerfile/compose text, not a
    # built image. The mkdir/chown assertions are coupled to the current
    # command spelling — update them alongside any Dockerfile rewording.
    # The image pre-creates the state root owned by the non-root user so a
    # freshly initialized named volume inherits writable ownership.
    assert f"mkdir -p {state_dir}" in dockerfile, (
        f"Dockerfile must create the state root {state_dir}"
    )
    assert re.search(
        rf"chown\b[^\n]*agentos:agentos[^\n]*{re.escape(state_dir)}", dockerfile
    ), f"Dockerfile must chown {state_dir} to the non-root agentos user"

    gateway_volumes = [str(v) for v in compose["services"]["gateway"]["volumes"]]
    assert f"agentos-state:{state_dir}" in gateway_volumes, (
        f"gateway must mount the 'agentos-state' named volume at {state_dir}, "
        f"got: {gateway_volumes}"
    )
    assert not any("/root/" in v for v in gateway_volumes), (
        "compose must not mount into /root — the container runs as a non-root user"
    )

    assert "agentos-state" in compose.get("volumes", {}), (
        "compose must declare the top-level 'agentos-state' named volume"
    )
