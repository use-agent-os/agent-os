#!/usr/bin/env bash
# install.sh - AgentOS release installer for Linux and macOS.
#
# This script is safe to pipe from the public install URL. It installs uv if
# needed, installs a release wheel with uv tool, then prints the explicit next
# steps. It does not run onboarding or start the gateway.
#
# It is also the engine installer behind the macOS desktop app, which drives it
# stage by stage over a small JSON protocol (modelled on the Hermes Agent
# bootstrap installer) so the app can show real progress and retry one step:
#
#   bash install.sh --manifest                 # one JSON line: the stage list
#   bash install.sh --stage uv --json          # run ONE stage; last stdout line is
#                                              #   {"ok":true|false,"stage":"uv",
#                                              #    "skipped":bool[,"reason":"…"]}
#
# Each --stage call is a separate process, so every stage re-derives what it
# needs (uv path, versions) and is safe to re-run. A plain invocation runs all
# stages in order, exactly as before.

set -euo pipefail

default_version="v2026.9.11"
repo_slug="${AGENTOS_REPOSITORY:-use-agent-os/agent-os}"
python_version="${AGENTOS_PYTHON_VERSION:-3.12}"
original_path="${PATH:-}"
protocol_version=1

cli_version=""
cli_profile=""
cli_extras=""
manifest_mode=0
stage_name=""
json_mode=0
non_interactive=0

usage() {
    cat <<HELP
Usage: bash install.sh [--version v2026.9.11|latest] [--profile recommended|core] [--extras name[,name]]
       bash install.sh --manifest
       bash install.sh --stage <name> [--json] [--non-interactive]

Environment equivalents:
  AGENTOS_VERSION=v2026.9.11
  AGENTOS_INSTALL_PROFILE=recommended|core
  AGENTOS_INSTALL_EXTRAS=document-extras
  AGENTOS_INSTALL_DRY_RUN=1
HELP
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            cli_version="${2:?install.sh: --version requires a value}"
            shift 2
            ;;
        --version=*)
            cli_version="${1#*=}"
            shift
            ;;
        --profile)
            cli_profile="${2:?install.sh: --profile requires a value}"
            shift 2
            ;;
        --profile=*)
            cli_profile="${1#*=}"
            shift
            ;;
        --extras)
            cli_extras="${2:?install.sh: --extras requires a value}"
            shift 2
            ;;
        --extras=*)
            cli_extras="${1#*=}"
            shift
            ;;
        --manifest)
            manifest_mode=1
            shift
            ;;
        --stage)
            stage_name="${2:?install.sh: --stage requires a value}"
            shift 2
            ;;
        --stage=*)
            stage_name="${1#*=}"
            shift
            ;;
        --json)
            json_mode=1
            shift
            ;;
        --non-interactive)
            non_interactive=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "install.sh: unknown argument '$1'." >&2
            usage >&2
            exit 1
            ;;
    esac
done

release_selector="${cli_version:-${AGENTOS_VERSION:-${default_version}}}"
profile="${cli_profile:-${AGENTOS_INSTALL_PROFILE:-recommended}}"
dry_run="${AGENTOS_INSTALL_DRY_RUN:-0}"

is_release_version() {
    [[ "$1" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?(\.post[0-9]+)?$ ]]
}

valid_extras=" document-extras "
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
for extra in ${raw_extras[@]+"${raw_extras[@]}"}; do
    [[ -n "${extra}" ]] || continue
    if [[ "${valid_extras}" != *" ${extra} "* ]]; then
        echo "install.sh: unsupported extra '${extra}'." >&2
        echo "install.sh: supported extras:${valid_extras}" >&2
        exit 1
    fi
    duplicate=0
    for existing in ${install_extras[@]+"${install_extras[@]}"}; do
        if [[ "${existing}" == "${extra}" ]]; then
            duplicate=1
            break
        fi
    done
    if [[ "${duplicate}" -eq 0 ]]; then
        install_extras+=("${extra}")
    fi
done

case "${profile}" in
    core|minimal)
        profile="core"
        target_extras=()
        ;;
    recommended)
        target_extras=(recommended)
        ;;
    *)
        echo "install.sh: unsupported AGENTOS_INSTALL_PROFILE='${profile}'." >&2
        echo "install.sh: supported profiles: core, recommended" >&2
        exit 1
        ;;
esac

if (( ${#install_extras[@]} > 0 )); then
    target_extras+=("${install_extras[@]}")
fi

if (( ${#target_extras[@]} > 0 )); then
    package_name="use-agent-os[$(IFS=,; echo "${target_extras[*]}")]"
else
    package_name="use-agent-os"
fi

if [[ "${release_selector}" != "latest" && "${release_selector}" != "stable" ]] && ! is_release_version "${release_selector}"; then
    echo "install.sh: unsupported AGENTOS_VERSION='${release_selector}'." >&2
    echo "install.sh: the release installer only supports latest, stable, or release versions like v2026.8.2." >&2
    echo "install.sh: use git clone plus scripts/install_source.sh for main, dev, branch, or source installs." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Stage protocol (--manifest / --stage). Kept jq-free: the installer runs
# before anything else is on the machine.
# ---------------------------------------------------------------------------

# name|title|category|needs_user_input — the order is the install order.
stage_table() {
    cat <<'STAGES'
prerequisites|Check this Mac|runtime|false
uv|Install the uv package manager|runtime|false
python|Install Python 3.12|runtime|false
package|Install the AgentOS engine|runtime|false
path|Put agentos on your PATH|runtime|false
complete|Finish|runtime|false
STAGES
}

json_escape() {
    # Escapes backslashes, double quotes, and control characters (as spaces).
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n\r\t' '   '
}

emit_manifest() {
    local first=1 line name title category needs
    printf '{"protocol_version":%s,"stages":[' "${protocol_version}"
    while IFS='|' read -r name title category needs; do
        [[ -n "${name}" ]] || continue
        if [[ "${first}" -eq 0 ]]; then printf ','; fi
        first=0
        printf '{"name":"%s","title":"%s","category":"%s","needs_user_input":%s}' \
            "$(json_escape "${name}")" "$(json_escape "${title}")" \
            "$(json_escape "${category}")" "${needs}"
    done < <(stage_table)
    printf ']}\n'
}

emit_stage_json() {
    local ok="$1" stage="$2" skipped="$3" reason="${4:-}"
    if [[ -n "${reason}" ]]; then
        printf '{"ok":%s,"stage":"%s","skipped":%s,"reason":"%s"}\n' \
            "${ok}" "$(json_escape "${stage}")" "${skipped}" "$(json_escape "${reason}")"
    else
        printf '{"ok":%s,"stage":"%s","skipped":%s}\n' "${ok}" "$(json_escape "${stage}")" "${skipped}"
    fi
}

stage_known() {
    local name
    while IFS='|' read -r name _; do
        [[ "${name}" == "$1" ]] && return 0
    done < <(stage_table)
    return 1
}

stage_needs_user_input() {
    local name needs
    while IFS='|' read -r name _ _ needs; do
        if [[ "${name}" == "$1" ]]; then
            [[ "${needs}" == "true" ]] && return 0
            return 1
        fi
    done < <(stage_table)
    return 1
}

# Progress lines go to stderr so a --json caller can keep stdout for the frame.
log() {
    if [[ "${json_mode}" -eq 1 ]]; then
        echo "install.sh: $*" >&2
    else
        echo "install.sh: $*"
    fi
}

fail() {
    echo "install.sh: $*" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Release resolution
# ---------------------------------------------------------------------------

fetch_url() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        fail "curl or wget is required."
    fi
}

resolve_release() {
    case "${release_selector}" in
        latest|stable)
            local latest_tag
            latest_tag="$(
                fetch_url "https://api.github.com/repos/${repo_slug}/releases/latest" \
                    | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
                    | head -n 1
            )"
            if ! is_release_version "${latest_tag}"; then
                fail "failed to resolve latest release tag for ${repo_slug}."
            fi
            release_version="${latest_tag#v}"
            display_version="${latest_tag}"
            ;;
        v*)
            release_version="${release_selector#v}"
            display_version="${release_selector}"
            ;;
        *)
            release_version="${release_selector}"
            display_version="v${release_version}"
            ;;
    esac
    wheel_url="https://github.com/${repo_slug}/releases/download/${display_version}/use_agent_os-${release_version}-py3-none-any.whl"
    install_spec="${package_name} @ ${wheel_url}"
}

# ---------------------------------------------------------------------------
# uv
# ---------------------------------------------------------------------------

install_uv() {
    # Download, then run: `curl | sh` masks curl failures (sh exits 0 on an
    # empty stdin) and mixes network errors with installer errors.
    local tmp
    tmp="$(mktemp "${TMPDIR:-/tmp}/uv-install.XXXXXX.sh")"
    if ! fetch_url https://astral.sh/uv/install.sh > "${tmp}"; then
        rm -f "${tmp}"
        fail "could not download the uv installer (check your network)."
    fi
    # Keep PATH edits to the user's shell files off: the desktop app never
    # depends on them and the `path` stage handles the terminal case.
    UV_NO_MODIFY_PATH="${UV_NO_MODIFY_PATH:-1}" sh "${tmp}"
    local code=$?
    rm -f "${tmp}"
    return "${code}"
}

resolve_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    if [[ -f "${HOME}/.local/bin/env" ]]; then
        # shellcheck disable=SC1091
        . "${HOME}/.local/bin/env"
    fi
    export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH:-}"
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    return 1
}

require_uv() {
    uv_bin="$(resolve_uv || true)"
    if [[ -z "${uv_bin}" ]]; then
        fail "uv is not installed; the 'uv' stage must run first."
    fi
}

# ---------------------------------------------------------------------------
# Stage bodies. Each runs in its own process under --stage, so it must not
# depend on state another stage left in memory; disk state is the contract.
# ---------------------------------------------------------------------------

stage_prerequisites() {
    local os arch
    os="$(uname -s 2>/dev/null || echo unknown)"
    arch="$(uname -m 2>/dev/null || echo unknown)"
    log "platform: ${os} ${arch}"
    case "${os}" in
        Darwin|Linux) ;;
        *) fail "unsupported platform '${os}'; use install.ps1 on Windows." ;;
    esac
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        fail "curl or wget is required to download uv and the AgentOS wheel."
    fi
    resolve_release
    log "release: ${display_version} (${profile})"
    if [[ "${dry_run}" == "1" ]]; then
        return 0
    fi
    # Reachability of the two hosts every later stage needs; a stalled
    # download is the usual failure and never errors on its own.
    local host
    for host in https://github.com https://astral.sh; do
        if ! fetch_url "${host}" >/dev/null 2>&1; then
            fail "cannot reach ${host}; check your network connection and retry."
        fi
    done
}

stage_uv() {
    if [[ "${dry_run}" == "1" ]]; then
        log "dry-run - would install uv if missing"
        return 0
    fi
    uv_bin="$(resolve_uv || true)"
    if [[ -n "${uv_bin}" ]]; then
        log "uv already installed: ${uv_bin}"
        return 0
    fi
    log "uv not found; installing uv first."
    install_uv
    uv_bin="$(resolve_uv || true)"
    if [[ -z "${uv_bin}" ]]; then
        fail "uv was not found after installation. Restart your terminal or run '. \"\$HOME/.local/bin/env\"', then retry."
    fi
    log "uv installed: ${uv_bin}"
}

stage_python() {
    if [[ "${dry_run}" == "1" ]]; then
        log "dry-run - would ensure Python ${python_version} via uv"
        return 0
    fi
    require_uv
    if "${uv_bin}" python find "${python_version}" >/dev/null 2>&1; then
        log "Python ${python_version} available: $("${uv_bin}" python find "${python_version}")"
        return 0
    fi
    log "installing Python ${python_version} with uv"
    "${uv_bin}" python install "${python_version}"
}

stage_package() {
    resolve_release
    if [[ "${dry_run}" == "1" ]]; then
        echo "install.sh: dry-run - would install AgentOS ${display_version}"
        echo "install.sh: dry-run - would run: uv tool install --python ${python_version} --force --reinstall-package use-agent-os \"${install_spec}\""
        return 0
    fi
    require_uv
    log "installing AgentOS ${display_version} (${profile})"
    "${uv_bin}" tool install --python "${python_version}" --force --reinstall-package use-agent-os "${install_spec}"
    local bin_dir
    bin_dir="$("${uv_bin}" tool dir --bin 2>/dev/null || true)"
    if [[ -n "${bin_dir}" && -x "${bin_dir}/agentos" ]]; then
        # The wheel is on disk; make sure the entry point actually runs before
        # calling the stage done.
        if ! "${bin_dir}/agentos" --help >/dev/null 2>&1; then
            fail "agentos was installed at ${bin_dir}/agentos but does not start; run it in a terminal to see why."
        fi
        log "agentos installed: ${bin_dir}/agentos"
    else
        fail "uv reported success but ${bin_dir:-<uv tool dir --bin>}/agentos is missing."
    fi
}

# Appends the uv tool bin dir to the login shell's PATH once, grep-guarded,
# so `agentos` works in a new terminal. The desktop app does not need this
# (it searches ~/.local/bin itself); it is for the person, not the app.
stage_path() {
    if [[ "${dry_run}" == "1" ]]; then
        log "dry-run - would add the uv tool bin dir to the shell PATH"
        return 0
    fi
    require_uv
    local bin_dir
    bin_dir="$("${uv_bin}" tool dir --bin 2>/dev/null || true)"
    if [[ -z "${bin_dir}" ]]; then
        log "could not determine the uv tool bin dir; skipping PATH setup"
        return 0
    fi
    if [[ ":${original_path}:" == *":${bin_dir}:"* ]]; then
        log "${bin_dir} is already on PATH"
        return 0
    fi
    local marker="# Added by AgentOS install.sh"
    local line="export PATH=\"${bin_dir}:\$PATH\""
    local rc rcs=()
    case "$(basename "${SHELL:-/bin/zsh}")" in
        zsh) rcs=("${HOME}/.zshrc" "${HOME}/.zprofile") ;;
        bash) rcs=("${HOME}/.bashrc" "${HOME}/.bash_profile") ;;
        fish)
            mkdir -p "${HOME}/.config/fish/conf.d"
            rc="${HOME}/.config/fish/conf.d/agentos.fish"
            if [[ ! -f "${rc}" ]]; then
                printf '%s\nfish_add_path -g "%s"\n' "${marker}" "${bin_dir}" > "${rc}"
                log "added ${bin_dir} to PATH via ${rc}"
            fi
            return 0
            ;;
        *) rcs=("${HOME}/.profile") ;;
    esac
    for rc in "${rcs[@]}"; do
        if [[ -f "${rc}" ]] && grep -Fq "${line}" "${rc}"; then
            log "${rc} already exports ${bin_dir}"
            return 0
        fi
    done
    rc="${rcs[0]}"
    printf '\n%s\n%s\n' "${marker}" "${line}" >> "${rc}"
    log "added ${bin_dir} to PATH in ${rc} (open a new terminal to use it)"
}

stage_complete() {
    resolve_release
    if [[ "${dry_run}" == "1" ]]; then
        return 0
    fi
    require_uv
    local tool_bin_dir
    tool_bin_dir="$("${uv_bin}" tool dir --bin 2>/dev/null || true)"
    # Under --json the frame owns stdout; the human summary is stderr.
    local out=/dev/stdout
    if [[ "${json_mode}" -eq 1 ]]; then out=/dev/stderr; fi
    cat >"${out}" <<DONE
----------------------------------------------------------------------------
AgentOS installed from ${display_version}.

Next steps:
  agentos onboard
  agentos gateway run

Default gateway bind: 127.0.0.1:18791 (loopback only).
Do not expose the gateway on 0.0.0.0 unless it is behind a trusted reverse
proxy or VPN.
----------------------------------------------------------------------------
DONE
    if [[ -n "${tool_bin_dir}" && ":${original_path}:" != *":${tool_bin_dir}:"* ]]; then
        cat >"${out}" <<PATHNOTE

PATH note:
  Your current shell may not find 'agentos' until PATH is refreshed.
  Run one of these, then retry the next steps:

    . "\$HOME/.local/bin/env"
    # or open a new terminal

PATHNOTE
    fi
}

run_stage_body() {
    case "$1" in
        prerequisites) stage_prerequisites ;;
        uv) stage_uv ;;
        python) stage_python ;;
        package) stage_package ;;
        path) stage_path ;;
        complete) stage_complete ;;
        *) fail "unknown stage '$1'." ;;
    esac
}

run_stage_protocol() {
    local stage="$1"
    if [[ -z "${stage}" ]]; then
        emit_stage_json false "" false "missing stage name"
        exit 2
    fi
    if ! stage_known "${stage}"; then
        emit_stage_json false "${stage}" false "unknown stage"
        exit 2
    fi
    if [[ "${non_interactive}" -eq 1 ]] && stage_needs_user_input "${stage}"; then
        log "skipping ${stage} (non-interactive)"
        emit_stage_json true "${stage}" true
        return 0
    fi
    # The body runs in a subshell: the stage helpers exit on failure, and the
    # frame must still be emitted so the caller sees {"ok":false} rather than
    # a process that vanished without a result.
    local code=0
    ( run_stage_body "${stage}" ) || code=$?
    if [[ "${code}" -eq 0 ]]; then
        emit_stage_json true "${stage}" false
        return 0
    fi
    emit_stage_json false "${stage}" false "stage '${stage}' failed (exit ${code}); see the log above"
    exit "${code}"
}

main() {
    local name names=()
    while IFS='|' read -r name _; do
        [[ -n "${name}" ]] || continue
        names+=("${name}")
    done < <(stage_table)
    # Collected first so no stage body runs with the stage list as its stdin.
    for name in "${names[@]}"; do
        run_stage_body "${name}"
    done
}

if [[ "${manifest_mode}" -eq 1 ]]; then
    emit_manifest
elif [[ -n "${stage_name}" ]]; then
    run_stage_protocol "${stage_name}"
else
    main
fi
