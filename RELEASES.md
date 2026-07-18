# AgentOS Releases

| Version | Tag | Date | Notes |
|---|---|---|---|
| 2026.7.18.post1 | v2026.7.18.post1 | 2026-07-18 | Release-hygiene re-cut: propagate the 2026.7.18 version across `uv.lock`, consistency/install tests, `RELEASES.md`, `CHANGELOG.md`, README install examples, and `install.sh`/`install.ps1`. No runtime code changes. |
| 2026.7.18 | v2026.7.18 | 2026-07-18 | Gateway: interactive auth provisioning on public bind, host/port CLI-only (#25); browser-threat hardening on loopback binds — CSWSH/DNS-rebinding guards (#24); rebrand to "Token-Efficient AI agent with on-device Pilot Router" |
| 2026.7.17.post1 | v2026.7.17.post1 | 2026-07-17 | `session_status` tool fix: resolve the calling session from the tool context instead of a `SessionManager` method that never existed |
| 2026.7.17 | v2026.7.17 | 2026-07-17 | Memory provider layer (mem0) + curated stores; v4_phase3 router bundle restored; Web UI transcript redesign; embedding-download redirect fix |
| 2026.7.15.post1 | v2026.7.15.post1 | 2026-07-15 | Partner-catalog skills system + Robinhood RWA address lookup skill (Bankr hub) |
| 2026.7.15 | v2026.7.15 | 2026-07-15 | Relicense to Apache-2.0 with `NOTICE` + OpenSquilla attribution; wheels ship license files |
| 2026.7.14.post1 | v2026.7.14.post1 | 2026-07-14 | PyPI distribution rename to `use-agent-os`; first PyPI release |
| 2026.7.14 | v2026.7.14 | 2026-07-14 | Release |
| 0.0.1 | v0.0.1 | 2026-07-05 | AgentOS baseline release |

Versions follow CalVer (`YYYY.M.D`). PEP 440 normalizes wheel filenames and drops
leading zeros, so tags must use the same non-padded form — tag `v2026.7.15`, not
`v2026.07.15`, or the wheel filename (`use_agent_os-2026.7.15-py3-none-any.whl`) will
not match the tag and the release smoke check fails.

Preview releases publish only versioned assets:

- `AgentOS-<version>-windows-x64-py312-recommended-portable.zip`
- `use_agent_os-<version>-py3-none-any.whl`
- `SHA256SUMS`

Non-preview releases additionally publish a version-independent alias for the
Windows portable zip `/releases/latest/download/` URL:

- `AgentOS-windows-x64-portable.zip`

GitHub source archives remain available for code review and developer
reference; source installs should use `git clone` plus Git LFS. Public
wheelhouse zips, macOS portable zips, and Linux portable zips are intentionally
not published for the 0.0.x line. macOS and Linux users install the same wheel
through the versioned `uv tool install` command documented in the README.
Python wheel filenames must remain versioned because installers validate the
version segment inside the wheel filename.

Preview releases are GitHub pre-releases. Their README install commands must
use tag-pinned URLs such as:

- `https://github.com/use-agent-os/agent-os/releases/download/v0.0.1rc1/AgentOS-0.0.1rc1-windows-x64-py312-recommended-portable.zip`
- `https://github.com/use-agent-os/agent-os/releases/download/v0.0.1rc1/use_agent_os-0.0.1rc1-py3-none-any.whl`

0.0.1 install commands use versioned wheel URLs because Python installers
validate wheel filenames. The Windows portable zip may use the
`/releases/latest/download/` alias after the non-pre-release GitHub Release
exists. Fully pinned URLs remain available:

- `https://github.com/use-agent-os/agent-os/releases/download/v0.0.1/AgentOS-0.0.1-windows-x64-py312-recommended-portable.zip`
- `https://github.com/use-agent-os/agent-os/releases/download/v0.0.1/use_agent_os-0.0.1-py3-none-any.whl`

## Release SOP

1. Verify `git status` is clean.
2. Update `CHANGELOG.md`: move entries from `[Unreleased]` to the release section; reopen empty `[Unreleased]`.
3. Bump `pyproject.toml` and `uv.lock` to the release version.
4. `git tag -a v0.0.1 -m "AgentOS 0.0.1"`
5. `git push origin v0.0.1` (this triggers `.github/workflows/wheelhouse-release.yml`)
6. Wait for the Windows release workflow → review the draft GitHub Release.
   For non-preview releases, confirm it contains versioned assets, latest
   aliases, `SHA256SUMS`, plus GitHub's generated source archives before
   publishing.
7. Confirm the draft GitHub Release is not marked as a pre-release.
8. Publish the GitHub Release, then run the post-publish tag URL checks:

   ```sh
   curl --fail --head --location https://github.com/use-agent-os/agent-os/releases/download/v0.0.1/AgentOS-0.0.1-windows-x64-py312-recommended-portable.zip
   curl --fail --head --location https://github.com/use-agent-os/agent-os/releases/download/v0.0.1/use_agent_os-0.0.1-py3-none-any.whl
   ```

9. Run the post-publish latest URL check:

   ```sh
   curl --fail --head --location https://github.com/use-agent-os/agent-os/releases/latest/download/AgentOS-windows-x64-portable.zip
   ```

10. For subsequent previews: bump `pyproject.toml`, `uv.lock`, `CHANGELOG.md`, and the tag to the next preview version, for example `0.0.2rc1` / `v0.0.2rc1`. Preview GitHub Releases must be marked as pre-releases and should use tag-pinned README URLs until the next non-preview release exists.

## GitHub-only release checks

These checks cannot be fully proven by local artifact generation:

- The tag exists on GitHub and matches `pyproject.toml`.
- The release workflow can fetch hydrated Git LFS router assets.
- Preview GitHub Releases contain the versioned assets and `SHA256SUMS` after
  `gh release upload --clobber`.
- Non-preview GitHub Releases contain the versioned assets, Windows latest alias, and
  `SHA256SUMS` after `gh release upload --clobber`.
- After a non-preview GitHub Release is published, the latest Windows portable
  URL resolves: `.../releases/latest/download/AgentOS-windows-x64-portable.zip`.
- After a preview GitHub Release is published, the tag-pinned release asset URLs
  resolve.
- Windows browser downloads may carry Mark-of-the-Web; SmartScreen,
  Smart App Control, enterprise policy, and unsigned binary reputation must be
  checked on a real Windows machine.

## Why preview package versions use rc

Release zips are distributed as built artifacts, so the package filename,
manifest, zip name, and tag should describe the same preview build. PEP 440
accepts `0.0.1rc1`, while the public GitHub Release title can use the friendlier
name "AgentOS 0.0.1 Preview 1".
