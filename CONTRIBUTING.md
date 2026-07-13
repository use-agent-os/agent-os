# Contributing

Thanks for improving AgentOS. Keep pull requests small, focused, and covered
by tests that outside contributors can run without private access.

## Pull Requests

Open pull requests against `main`. Reference related issues in the
description with GitHub keywords (`Fixes #123`, `Refs #123`) when one exists.

When a squash or rebase collapses commits from several people, keep the final
commit attributable with `Co-authored-by:` trailers for every contributor
whose work is included.

## Default Checks

Install development dependencies:

```sh
uv sync --extra dev --extra recommended
```

Run the quality gate before opening a pull request:

```sh
uv run ruff check src tests
uv run pytest -q
uv build --wheel
```

Default tests must be offline, deterministic, credential-free, and safe for
forks. Do not add network, provider, browser, or channel requirements to the
default pull request path.

## Test Expectations

Add or update public regression tests for behavior changes and bug fixes.
Prefer focused unit or integration tests unless the behavior crosses the
gateway, browser UI, provider, or channel boundary. Live provider, browser,
and channel smoke tests are maintainer-only opt-in workflows
(`Live Release E2E` and `LLM E2E`).

## Private Materials

Private test suites, real provider transcripts, real channel identifiers,
local paths, credentials, and AI session artifacts must not be committed.
Local maintainer-only files may live under `tests/_private/`; it is excluded
from the public tree and default pytest collection.

## Third-Party Origins

Declare any third-party origin in the pull request (`none` if there is none):
`inspired-by`, `adapted/ported`, `vendored`, `direct dependency`, or
`modified upstream`. For adapted, vendored, or modified upstream material,
include the upstream URL, license, copyright notice, and any required changes
to `THIRD_PARTY_NOTICES.md` in the same pull request.

Permissive licenses (Apache-2.0, MIT, BSD, ISC) are usually acceptable. GPL,
AGPL, LGPL, SSPL, source-available, or unclear licenses require explicit
maintainer approval before merge.

## Security Reports

Do not include vulnerability details, exploit steps, credentials, or provider
tokens in public issues. Use the process in `SECURITY.md` for suspected
vulnerabilities.

## Community Standards

Keep discussion technical, specific, and respectful. Expected conduct is
documented in `CODE_OF_CONDUCT.md`.
