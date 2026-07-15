#!/usr/bin/env bash
# install_source.sh - user-local AgentOS installer (no sudo).
#
# Installer contract:
#   - installs into a user-owned prefix (never /usr/local, /opt, or admin paths)
#   - prefers uv tool install; falls back to pip --user; errors clearly if neither exists
#   - defaults to the "recommended" runtime profile (memory + bundled BGE
#     embedding assets) and allows `AGENTOS_INSTALL_PROFILE=core` to opt back down
#   - prints a post-install banner documenting the default bind
#     (127.0.0.1:18791) and the explicit opt-in required to expose the gateway
#     on the network (--listen 0.0.0.0 or AGENTOS_LISTEN=0.0.0.0)
#   - adds an extra WARNING when the operator requested network exposure at
#     install time via AGENTOS_LISTEN=0.0.0.0
#
# Dry-run: export AGENTOS_INSTALL_DRY_RUN=1 to print the install plan + banner
# without touching the system.

set -euo pipefail

cli_profile=""
cli_extras=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)
            cli_profile="${2:?install_source.sh: --profile requires a value}"
            shift 2
            ;;
        --profile=*)
            cli_profile="${1#*=}"
            shift
            ;;
        --extras)
            cli_extras="${2:?install_source.sh: --extras requires a value}"
            shift 2
            ;;
        --extras=*)
            cli_extras="${1#*=}"
            shift
            ;;
        -h|--help)
            cat <<HELP
Usage: bash scripts/install_source.sh [--profile recommended|core] [--extras name[,name]]

Environment equivalents:
  AGENTOS_INSTALL_PROFILE=recommended|core
  AGENTOS_INSTALL_EXTRAS=matrix
  AGENTOS_INSTALL_DRY_RUN=1
HELP
            exit 0
            ;;
        *)
            echo "install_source.sh: unknown argument '$1'." >&2
            echo "install_source.sh: run 'bash scripts/install_source.sh --help' for usage." >&2
            exit 1
            ;;
    esac
done

# --- prefix resolution ------------------------------------------------------

if [[ -n "${AGENTOS_PREFIX:-}" ]]; then
    prefix="${AGENTOS_PREFIX}"
elif [[ -n "${XDG_DATA_HOME:-}" ]]; then
    prefix="${XDG_DATA_HOME}/agentos"
else
    prefix="${HOME}/.local"
fi

dry_run="${AGENTOS_INSTALL_DRY_RUN:-0}"
profile="${cli_profile:-${AGENTOS_INSTALL_PROFILE:-recommended}}"

valid_extras=" matrix matrix-e2e document-extras "
extras_csv="${AGENTOS_INSTALL_EXTRAS:-}"
if [[ -n "${cli_extras}" ]]; then
    extras_csv="${extras_csv}${extras_csv:+,}${cli_extras}"
fi
extras_csv="${extras_csv// /,}"
raw_extras=()
if [[ -n "${extras_csv}" ]]; then
    IFS=',' read -r -a raw_extras <<< "${extras_csv}"
fi
install_extras=()
if (( ${#raw_extras[@]} > 0 )); then
    for extra in "${raw_extras[@]}"; do
        [[ -n "${extra}" ]] || continue
        if [[ "${valid_extras}" != *" ${extra} "* ]]; then
            echo "install_source.sh: unsupported extra '${extra}'." >&2
            echo "install_source.sh: supported extras:${valid_extras}" >&2
            exit 1
        fi
        duplicate=0
        if (( ${#install_extras[@]} > 0 )); then
            for existing in "${install_extras[@]}"; do
                if [[ "${existing}" == "${extra}" ]]; then
                    duplicate=1
                    break
                fi
            done
        fi
        if [[ "${duplicate}" -eq 0 ]]; then
            install_extras+=("${extra}")
        fi
    done
fi

case "${profile}" in
    core|minimal)
        profile="core"
        target_extras=()
        ;;
    recommended)
        target_extras=(recommended)
        ;;
    *)
        echo "install_source.sh: unsupported AGENTOS_INSTALL_PROFILE='${profile}'." >&2
        echo "install_source.sh: supported profiles: core, recommended" >&2
        exit 1
        ;;
esac
if (( ${#install_extras[@]} > 0 )); then
    target_extras+=("${install_extras[@]}")
fi
if (( ${#target_extras[@]} > 0 )); then
    joined_extras="$(IFS=,; echo "${target_extras[*]}")"
    install_target=".[${joined_extras}]"
else
    install_target="."
fi

check_embedding_assets() {
    local mode="${1:-strict}"
    if [[ "${profile}" != "recommended" ]]; then
        return 0
    fi

    local model_root="src/agentos/memory/models/bge_onnx"
    local pointer_line="version https://git-lfs.github.com/spec/v1"
    local required=(
        "${model_root}/model.onnx"
    )
    local missing=()
    local pointers=()
    local path=""
    for path in "${required[@]}"; do
        if [[ ! -f "${path}" ]]; then
            missing+=("${path}")
            continue
        fi
        if LC_ALL=C grep -q -m 1 -F -x "${pointer_line}" "${path}" 2>/dev/null; then
            pointers+=("${path}")
        fi
    done
    if (( ${#missing[@]} > 0 || ${#pointers[@]} > 0 )); then
        if [[ "${mode}" == "warn" ]]; then
            echo "install_source.sh: dry-run note — real recommended install would fail until the bundled BGE embedding assets are available in this checkout." >&2
        else
            echo "install_source.sh: bundled BGE embedding assets are unavailable in this checkout." >&2
        fi
        if (( ${#missing[@]} > 0 )); then
            echo "install_source.sh: missing assets: ${missing[*]}" >&2
        fi
        if (( ${#pointers[@]} > 0 )); then
            echo "install_source.sh: Git LFS pointer files detected: ${pointers[*]}" >&2
        fi
        echo 'install_source.sh: run `git lfs install` once, then:' >&2
        echo 'install_source.sh:   git lfs pull --include="src/agentos/memory/models/**"' >&2
        echo 'install_source.sh: or retry with AGENTOS_INSTALL_PROFILE=core for the minimal runtime.' >&2
        if [[ "${mode}" == "warn" ]]; then
            return 0
        fi
        exit 1
    fi
}

# --- installer selection ----------------------------------------------------

installer=""
install_args=()
if command -v uv >/dev/null 2>&1; then
    installer="uv"
    install_args=(uv tool install --force --reinstall-package use-agent-os "${install_target}")
elif command -v python3 >/dev/null 2>&1; then
    installer="pip"
    install_args=(python3 -m pip install --user "${install_target}")
else
    echo "install_source.sh: neither 'uv' nor 'python3' is available on PATH." >&2
    echo "install_source.sh: install uv (https://docs.astral.sh/uv/) or Python 3.12+ and retry." >&2
    exit 1
fi
install_cmd="${install_args[*]}"

# --- banner -----------------------------------------------------------------

print_banner() {
    cat <<BANNER
----------------------------------------------------------------------------
AgentOS installed via ${installer} -> ${prefix} (profile: ${profile})
Extras: $(if (( ${#install_extras[@]} > 0 )); then IFS=,; echo "${install_extras[*]}"; else echo "none"; fi)

Default gateway bind: 127.0.0.1:18791 (loopback only)
Network exposure is opt-in only. To expose the gateway on the network you
must use one of:
  - CLI flag:  agentos gateway run --listen 0.0.0.0
  - Env var:   AGENTOS_LISTEN=0.0.0.0 agentos gateway run

Reminder: only expose 0.0.0.0 behind a trusted reverse proxy or VPN. The
gateway's first-class auth assumes loopback-scope by default.
----------------------------------------------------------------------------
BANNER
}

print_listen_warning() {
    cat <<WARNING
WARNING: you have selected network-exposed default - ensure you
   understand the blast radius. The gateway will bind to 0.0.0.0 and be
   reachable from every interface on this host.
WARNING
}

if [[ "${dry_run}" = "1" ]]; then
    echo "install_source.sh: dry-run — would run: ${install_cmd}"
    echo "install_source.sh: dry-run — prefix: ${prefix}"
    check_embedding_assets warn
    print_banner
    if [[ "${AGENTOS_LISTEN:-}" = "0.0.0.0" ]]; then
        print_listen_warning
    fi
    exit 0
fi

# --- execute ---------------------------------------------------------------

check_embedding_assets

echo "install_source.sh: installing via ${installer} into prefix ${prefix}"
echo "install_source.sh: running: ${install_cmd}"
"${install_args[@]}"

print_banner
if [[ "${AGENTOS_LISTEN:-}" = "0.0.0.0" ]]; then
    print_listen_warning
fi
