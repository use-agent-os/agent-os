"""File-read surfaces must mask credentials, denylist bypassed or not.

The sensitive-path denylist is switched off under elevated-full mode, so it
cannot be the only thing between ``~/.aws/credentials`` and the transcript.
These tests pin the redaction layer that stays on behind it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentos.redact import redact_file_output
from agentos.sandbox.sensitive_paths import sensitive_path_marker
from agentos.tools.builtin import filesystem as fs
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

OPENAI_KEY = "sk-proj-" + "A" * 24
GITHUB_PAT = "ghp_" + "B" * 20
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
DB_PASSWORD = "sup3rSecretPassw0rdValue"
#: Split so the repo's own secret-shape hygiene check does not flag the fixture.
PEM_BEGIN = "-----" + "BEGIN"


@contextmanager
def tool_context(*, elevated: str | None = None) -> Iterator[None]:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            elevated=elevated,
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)


def test_redact_file_output_uses_the_non_reusable_sentinel() -> None:
    out = redact_file_output(f"api_key: {GITHUB_PAT}")

    assert GITHUB_PAT not in out
    assert "«redacted:" in out


def test_redact_file_output_passes_empty_text_through() -> None:
    assert redact_file_output("") == ""


@pytest.mark.asyncio
async def test_read_file_masks_a_vendor_key(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(f'api_key = "{OPENAI_KEY}"\n', encoding="utf-8")

    with tool_context():
        out = await fs.read_file(str(target))

    assert OPENAI_KEY not in out
    assert "«redacted:" in out
    assert out.startswith("1\t")


@pytest.mark.asyncio
async def test_read_file_leaves_ordinary_source_untouched(tmp_path: Path) -> None:
    target = tmp_path / "settings.py"
    target.write_text('MAX_TOKENS = 4096\nAPI_KEY = "test-value"\n', encoding="utf-8")

    with tool_context():
        out = await fs.read_file(str(target))

    assert out == '1\tMAX_TOKENS = 4096\n2\tAPI_KEY = "test-value"\n'


@pytest.mark.asyncio
async def test_read_file_redacts_a_sensitive_path_under_elevated_full(tmp_path: Path) -> None:
    """Elevation drops the hard block; the mask behind it must still apply."""
    target = tmp_path / ".env"
    target.write_text(f"OPENAI_API_KEY={OPENAI_KEY}\n", encoding="utf-8")
    assert sensitive_path_marker(str(target)) is not None

    with tool_context(elevated="full"):
        out = await fs.read_file(str(target))

    assert OPENAI_KEY not in out
    assert "«redacted:" in out


@pytest.mark.asyncio
async def test_read_spreadsheet_masks_a_vendor_key(tmp_path: Path) -> None:
    target = tmp_path / "keys.csv"
    target.write_text(f"name,value\nopenai,{OPENAI_KEY}\n", encoding="utf-8")

    with tool_context():
        out = await fs.read_spreadsheet(str(target))

    assert OPENAI_KEY not in out
    assert "«redacted:" in out


@pytest.mark.asyncio
async def test_grep_search_masks_a_vendor_key_in_the_matched_line(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text(f"token: {GITHUB_PAT}\n", encoding="utf-8")

    with tool_context():
        out = await fs.grep_search("token", path=str(target))

    assert GITHUB_PAT not in out
    assert "«redacted:" in out


def test_redact_file_output_sentinels_an_auth_header_too() -> None:
    """Every mask on the file path is non-reusable, headers included."""
    out = redact_file_output("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", path="notes.txt")

    assert "abcdefghijklmnopqrstuvwxyz" not in out
    assert "Authorization: Bearer «redacted:" in out


def test_redact_file_output_sentinels_a_dsn_password() -> None:
    """``***`` reads as a value worth writing back; the sentinel does not."""
    out = redact_file_output("postgres://app:s3cretpassword@db/app", path="/app/.env")

    assert "s3cretpassword" not in out
    assert "postgres://app:«redacted»@db/app" == out


# Source lines that a name-only guard mangles: the value half is an identifier,
# a type annotation or a literal, and rewriting it hands the model broken code.
CODE_LINES = [
    'return {"xi-api-key": api_key}',
    '    "Authorization": f"Bearer {self._api_key}",',
    '    "requiresApiKey": False,',
    "def sign(authorization: MandateAuthorization | None = None) -> None:",
    "    client = Client(api_key=self._api_key)",
]


@pytest.mark.parametrize("line", CODE_LINES)
def test_redact_file_output_leaves_source_code_intact(line: str) -> None:
    assert redact_file_output(line, path="src/agentos/provider/openai.py") == line


@pytest.mark.asyncio
async def test_read_file_does_not_rewrite_source_code(tmp_path: Path) -> None:
    """A mangled read is an edit the agent can no longer make."""
    target = tmp_path / "provider.py"
    body = "\n".join(CODE_LINES) + "\n"
    target.write_text(body, encoding="utf-8")

    with tool_context():
        out = await fs.read_file(str(target))

    assert out == "".join(f"{i}\t{line}\n" for i, line in enumerate(CODE_LINES, 1))


@pytest.mark.asyncio
async def test_read_file_masks_an_opaque_secret_in_a_credentials_file(tmp_path: Path) -> None:
    """The headline case: the AWS secret carries no vendor prefix to match on."""
    target = tmp_path / "credentials"
    target.write_text(
        "[default]\n"
        "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"
        f"aws_secret_access_key = {AWS_SECRET}\n",
        encoding="utf-8",
    )

    with tool_context(elevated="full"):
        out = await fs.read_file(str(target))

    assert AWS_SECRET not in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out


@pytest.mark.asyncio
async def test_read_file_masks_an_opaque_password_in_a_dotenv(tmp_path: Path) -> None:
    target = tmp_path / ".env.local"
    target.write_text(f"DB_PASSWORD={DB_PASSWORD}\nMAX_TOKENS=4096\n", encoding="utf-8")

    with tool_context(elevated="full"):
        out = await fs.read_file(str(target))

    assert DB_PASSWORD not in out
    assert "MAX_TOKENS=4096" in out


@pytest.mark.asyncio
async def test_grep_search_judges_each_file_by_its_own_path(tmp_path: Path) -> None:
    """One search spans both kinds of file, and each gets its own policy."""
    (tmp_path / "app.ini").write_text(f"db_password = {DB_PASSWORD}\n", encoding="utf-8")
    (tmp_path / "db.py").write_text("db_password = settings.db_password\n", encoding="utf-8")

    with tool_context():
        out = await fs.grep_search("db_password", path=str(tmp_path))

    assert "db_password = «redacted:" in out
    assert DB_PASSWORD not in out
    assert "db_password = settings.db_password" in out


def test_edit_file_failed_match_hint_does_not_quote_a_secret(tmp_path: Path) -> None:
    """The closest-match hint quotes real file lines, so it is a read channel."""
    target = tmp_path / "credentials"
    original = f"aws_secret_access_key = {AWS_SECRET}\n"

    with pytest.raises(ValueError) as excinfo:
        fs._locate_edit(original, "aws_secret_access_key = NOPE", "x", path=str(target))

    message = str(excinfo.value)
    assert "Closest match:" in message
    assert AWS_SECRET not in message


# ── Passes chosen by path ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "text"),
    [
        # A type annotation, a keyword map and an f-string template: all shapes
        # the name-driven pass reads as ``name: secret``.
        ("api.py", "apiKey: NotRequired[str]"),
        ("lexer.py", "    'AUTHORIZATION': tokens.Keyword,"),
        ("db.py", 'DSN = f"postgres://{user}:{password}@{host}/db"'),
        # Two string literals, not one key: masking the span eats the code.
        (
            "ssh.py",
            f'_SK_START = b"{PEM_BEGIN} OPENSSH PRIVATE KEY-----"\n'
            '_SK_END = b"-----END OPENSSH PRIVATE KEY-----"',
        ),
        # A vocabulary entry, and a documentation placeholder.
        ("tokenizer.json", '"authorization": 20104,'),
        ("api.md", "Send it as `Authorization: Bearer <token>`."),
    ],
)
def test_redact_file_output_leaves_non_credentials_alone(path: str, text: str) -> None:
    assert redact_file_output(text, path=path) == text


def test_redact_file_output_masks_a_pem_block_line_by_line() -> None:
    """A collapsed block desyncs the line numbers read_file just emitted."""
    numbered = (
        f"1\t{PEM_BEGIN} RSA PRIVATE KEY-----\n"
        "2\tMIIEowIBAAKCAQEA0Y\n"
        "3\t-----END RSA PRIVATE KEY-----\n"
        "4\tafter\n"
    )

    out = redact_file_output(numbered, path="/home/u/.ssh/id_rsa")

    assert "MIIEowIBAAKCAQEA0Y" not in out
    assert [line.split("\t")[0] for line in out.splitlines()] == ["1", "2", "3", "4"]
    assert out.endswith("4\tafter\n")


def test_redact_file_output_does_not_match_a_prefix_inside_base64() -> None:
    """``AKIA`` mid-blob is a font, not a key; masking it corrupts the blob."""
    blob = "AAAwAaAKIAABCDEFGHIJKLMNOAQAAAAAABQA8"

    assert redact_file_output(blob, path="ImageFont.py") == blob


def test_redact_file_output_masks_userinfo_in_a_git_credentials_file() -> None:
    out = redact_file_output(
        "https://user:opaquepassword123@github.com", path="/home/u/.git-credentials"
    )

    assert "opaquepassword123" not in out


def test_redact_file_output_masks_a_secret_dir_config() -> None:
    """``~/.kube/config`` is credential material whatever its name says."""
    out = redact_file_output('"password": "b3BhcXVlYmFzZTY0dmFs"', path="/home/u/.kube/config")

    assert "b3BhcXVlYmFzZTY0dmFs" not in out


def test_redact_terminal_output_covers_a_credential_file_read() -> None:
    """Blocking only read_file would just move the agent to the shell."""
    from agentos.redact import redact_terminal_output

    out = redact_terminal_output(f"aws_secret_access_key = {AWS_SECRET}", "cat ~/.aws/credentials")

    assert AWS_SECRET not in out


def test_redact_terminal_output_still_masks_a_short_header_value() -> None:
    """The file-read work must not un-mask what the terminal already masked."""
    from agentos.redact import redact_terminal_output

    out = redact_terminal_output("Authorization: Bearer abc123def45", "curl -v https://x")

    assert "abc123def45" not in out
