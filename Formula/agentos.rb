# frozen_string_literal: true

# AgentOS — microkernel Python agent runtime. See caveats for the loopback-safe
# gateway default and the documented `--listen 0.0.0.0` opt-in.
class Agentos < Formula
  include Language::Python::Virtualenv

  desc "Microkernel Python agent runtime with MCP tools and multi-channel messaging"
  homepage "https://github.com/use-agent-os/agent-os"
  url "https://github.com/use-agent-os/agent-os/archive/refs/tags/v0.0.1.tar.gz"
  # Placeholder: replace with the real sha256 of the v0.0.1 source tarball when
  # the release is published (e.g. `brew fetch --build-from-source ./Formula/agentos.rb`).
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"
  head "https://github.com/use-agent-os/agent-os.git", branch: "main"

  depends_on "python@3.13"

  # First-draft formula: pip_install_and_link resolves runtime deps from
  # PyPI at brew-install time. Once AgentOS ships a 0.0.1 tag, each runtime
  # dep will be pinned here as a `resource` block for audit-grade install.
  def install
    venv = virtualenv_create(libexec, "python3.13")
    venv.pip_install_and_link buildpath
  end

  def caveats
    <<~EOS
      AgentOS installed.

      Default gateway bind: 127.0.0.1:18791 (loopback only).
      Network exposure is opt-in only. To expose the gateway on the network:

        - CLI flag:  agentos gateway run --listen 0.0.0.0
        - Env var:   AGENTOS_LISTEN=0.0.0.0 agentos gateway run

      Reminder: only expose 0.0.0.0 behind a trusted reverse proxy or VPN.
      The gateway's first-class auth assumes loopback-scope by default.

      The Homebrew formula installs the core runtime. Local memory embedding
      also requires the hydrated Git LFS BGE ONNX asset, which GitHub source
      tarballs do not carry. If you want local semantic memory embedding, use a
      source checkout with Git LFS plus the `recommended` profile:

        git lfs pull --include="src/agentos/memory/models/**"
        uv sync --extra recommended

      Service units (launchd / systemd / Task Scheduler) ship in
      service-units/. For macOS, install the LaunchAgent:

        envsubst < service-units/launchd/dev.useagentos.gateway.plist \\
          > ~/Library/LaunchAgents/dev.useagentos.gateway.plist
        launchctl load ~/Library/LaunchAgents/dev.useagentos.gateway.plist

      See service-units/README.md for the per-platform install + opt-in
      walkthrough.
    EOS
  end

  test do
    assert_match "agentos", shell_output("#{bin}/agentos --help 2>&1")
  end
end
