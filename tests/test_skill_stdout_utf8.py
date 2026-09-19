"""Bundled skill scripts emit their JSON result as UTF-8, whatever the console code page.

``print()`` encodes through ``sys.stdout.encoding``. On Windows that is the
console code page (cp1252 here, cp936/cp932 on CJK systems), not UTF-8, so a
result carrying a character outside that page raised ``UnicodeEncodeError``
and the script died with a traceback instead of returning anything (#2334).
The ``--out`` branch of the same scripts was never affected because it passes
``encoding="utf-8"`` explicitly, which is what made the stdout path the odd
one out rather than a platform limitation.

The code page is **simulated**, not skipped: ``sys.stdout`` is replaced with a
cp1252-encoded text stream over a ``BytesIO``. A ``skipif(sys.platform !=
"win32")`` would run on only half of CI, and the defect is in the encoder
choice, not in Windows. The same substitution reproduces it on any host.

Three cases here pass on an unfixed tree by design and say so in their own
docstring: their payload is integers only, so it cannot carry a character the
code page rejects today. They are guards that the write path stays UTF-8 when
a string field is added to those payloads later.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"

#: Characters outside cp1252: CJK, and a Vietnamese vowel with two diacritics.
NON_ASCII = "日本語のテキスト cổ phiếu"


def _load(relative: str, name: str) -> Any:
    """Import a bundled script by path, the way the other skill tests do."""
    spec = importlib.util.spec_from_file_location(name, BUNDLED / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: a dataclass in the module resolves its own
    # annotations through ``sys.modules[cls.__module__]``, which is absent for
    # a module built straight from a spec.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CodePageStdout:
    """A ``sys.stdout`` whose text layer only accepts one legacy code page.

    Installed as a context manager **inside** the test body rather than from a
    fixture: pytest re-activates its own capture at the start of each test
    phase, so a ``sys.stdout`` swapped in during fixture setup is replaced
    again before the test runs.
    """

    def __init__(self, encoding: str = "cp1252") -> None:
        self.sink = io.BytesIO()
        self.stream = io.TextIOWrapper(self.sink, encoding=encoding, newline="")
        self._saved: Any = None

    def __enter__(self) -> CodePageStdout:
        self._saved = sys.stdout
        sys.stdout = self.stream
        return self

    def __exit__(self, *_exc: Any) -> None:
        sys.stdout = self._saved
        self.stream.flush()
        # Detach so the wrapper's finalizer cannot close the sink we still read.
        self.stream.detach()

    def text(self) -> str:
        return self.sink.getvalue().decode("utf-8")

    def payload(self) -> Any:
        return json.loads(self.text())


def _argv(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", list(args))


# ── docx ────────────────────────────────────────────────────────────────────


def _make_docx(path: Path, text: str) -> None:
    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))


def test_inspect_docx_emits_utf8_on_a_code_page_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inspect_docx = _load("docx/scripts/inspect_docx.py", "inspect_docx")
    source = tmp_path / "cjk.docx"
    _make_docx(source, NON_ASCII)
    _argv(monkeypatch, "inspect_docx.py", str(source))

    with CodePageStdout() as code_page_stdout:
        assert inspect_docx.main() == 0
    assert NON_ASCII in json.dumps(code_page_stdout.payload(), ensure_ascii=False)


def test_edit_docx_result_is_written_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard — passes either way today: the payload is ``{"applied": <int>}``.

    Pinned so the write path is already UTF-8 the day that payload grows a
    string field (a sheet name, a path, an error) and the defect would
    otherwise come back silently.
    """
    edit_docx = _load("docx/scripts/edit_docx.py", "edit_docx")
    source = tmp_path / "in.docx"
    _make_docx(source, NON_ASCII)
    ops = tmp_path / "ops.json"
    ops.write_text(
        json.dumps([{"op": "replace_text", "find": NON_ASCII, "replace": "x"}]),
        encoding="utf-8",
    )
    out = tmp_path / "out.docx"
    _argv(monkeypatch, "edit_docx.py", str(source), str(ops), "--out", str(out))

    with CodePageStdout() as code_page_stdout:
        assert edit_docx.main() == 0
    assert code_page_stdout.text().endswith("\n")
    assert "applied" in code_page_stdout.payload()


# ── pdf-toolkit ─────────────────────────────────────────────────────────────


def _make_pdf(path: Path, title: str) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": title})
    with path.open("wb") as handle:
        writer.write(handle)


def _make_form(path: Path) -> None:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(path), pagesize=LETTER)
    pdf.acroForm.textfield(name="full_name", x=72, y=700, width=200, height=20)
    pdf.showPage()
    pdf.save()


def test_pdf_extract_emits_utf8_on_a_code_page_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extract = _load("pdf-toolkit/scripts/extract.py", "extract")
    source = tmp_path / "cjk.pdf"
    _make_pdf(source, NON_ASCII)
    _argv(monkeypatch, "extract.py", str(source))

    with CodePageStdout() as code_page_stdout:
        assert extract.main() == 0
    assert NON_ASCII in json.dumps(code_page_stdout.payload(), ensure_ascii=False)


def test_form_fill_list_fields_emits_utf8_on_a_code_page_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    form_fill = _load("pdf-toolkit/scripts/form_fill.py", "form_fill")
    form = tmp_path / "form.pdf"
    _make_form(form)
    filled = tmp_path / "filled.pdf"
    form_fill.fill(form, {"full_name": NON_ASCII}, filled)
    _argv(monkeypatch, "form_fill.py", str(filled), "--list-fields")

    with CodePageStdout() as code_page_stdout:
        assert form_fill.main() == 0
    assert NON_ASCII in code_page_stdout.text()


def test_form_fill_result_is_written_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard — passes either way today: the payload is two integers."""
    form_fill = _load("pdf-toolkit/scripts/form_fill.py", "form_fill")
    form = tmp_path / "form.pdf"
    _make_form(form)
    data = tmp_path / "data.json"
    data.write_text(json.dumps({"full_name": NON_ASCII}), encoding="utf-8")
    out = tmp_path / "filled.pdf"
    _argv(monkeypatch, "form_fill.py", str(form), str(data), "--out", str(out))

    with CodePageStdout() as code_page_stdout:
        assert form_fill.main() == 0
    assert code_page_stdout.payload()["fields"] == 1


def test_pdf_merge_emits_a_non_ascii_output_path_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    merge = _load("pdf-toolkit/scripts/merge.py", "merge")
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    _make_pdf(first, "a")
    _make_pdf(second, "b")
    out = tmp_path / f"{NON_ASCII}.pdf"
    _argv(monkeypatch, "merge.py", str(first), str(second), "--out", str(out))

    with CodePageStdout() as code_page_stdout:
        assert merge.main() == 0
    assert NON_ASCII in code_page_stdout.payload()["out"]


def test_pdf_split_emits_non_ascii_output_paths_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    split = _load("pdf-toolkit/scripts/split.py", "split")
    source = tmp_path / "in.pdf"
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    with source.open("wb") as handle:
        writer.write(handle)
    out_dir = tmp_path / NON_ASCII
    _argv(monkeypatch, "split.py", str(source), "--pages", "1,2", "--out", str(out_dir))

    with CodePageStdout() as code_page_stdout:
        assert split.main() == 0
    assert any(NON_ASCII in name for name in code_page_stdout.payload()["files"])


# ── xlsx ────────────────────────────────────────────────────────────────────


def test_edit_xlsx_result_is_written_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard — passes either way today: the payload is ``{"applied": <int>}``."""
    edit_xlsx = _load("xlsx/scripts/edit_xlsx.py", "edit_xlsx")
    from openpyxl import Workbook

    source = tmp_path / "in.xlsx"
    book = Workbook()
    book.active.title = "Sheet1"
    book.save(str(source))
    ops = tmp_path / "ops.json"
    ops.write_text(
        json.dumps([{"op": "set_cell", "sheet": "Sheet1", "row": 1, "col": 1, "value": NON_ASCII}]),
        encoding="utf-8",
    )
    out = tmp_path / "out.xlsx"
    _argv(monkeypatch, "edit_xlsx.py", str(source), str(ops), "--out", str(out))

    with CodePageStdout() as code_page_stdout:
        assert edit_xlsx.main() == 0
    assert code_page_stdout.payload()["applied"] == 1


# ── robinhood ───────────────────────────────────────────────────────────────


def test_chain_stocks_invalid_rpc_url_error_is_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    chain_stocks = _load("robinhood-chain-stocks/scripts/chain_stocks.py", "chain_stocks")
    bad = f"ftp://{NON_ASCII}.test"
    _argv(monkeypatch, "chain_stocks.py", "--query", "AAPL", "--rpc-url", bad, "--no-cards")

    with CodePageStdout() as code_page_stdout:
        assert chain_stocks.main() == 0
    assert NON_ASCII in code_page_stdout.payload()["error"]


def test_chain_stocks_resolve_failure_echoes_the_query_as_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain_stocks = _load("robinhood-chain-stocks/scripts/chain_stocks.py", "chain_stocks")

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("no match")

    monkeypatch.setattr(chain_stocks, "_resolve_target", _boom)
    _argv(monkeypatch, "chain_stocks.py", "--query", NON_ASCII, "--no-cards")

    with CodePageStdout() as code_page_stdout:
        assert chain_stocks.main() == 0
    assert code_page_stdout.payload()["query"] == NON_ASCII


def test_chain_stocks_result_echoes_the_query_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    chain_stocks = _load("robinhood-chain-stocks/scripts/chain_stocks.py", "chain_stocks")
    address = "0x" + "ab" * 20

    monkeypatch.setattr(
        chain_stocks,
        "_resolve_target",
        lambda *_a, **_k: (address, {"name": NON_ASCII, "symbol": "AAPL"}, []),
    )
    monkeypatch.setattr(chain_stocks, "inspect_token", lambda *_a, **_k: {"isStockToken": True})
    _argv(monkeypatch, "chain_stocks.py", "--query", NON_ASCII, "--no-cards", "--no-price")

    with CodePageStdout() as code_page_stdout:
        assert chain_stocks.main() == 0
    assert code_page_stdout.payload()["query"] == NON_ASCII


def test_rwa_lookup_result_echoes_the_query_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    rwa_lookup = _load("robinhood-rwa-addresses/scripts/rwa_lookup.py", "rwa_lookup")

    monkeypatch.setattr(rwa_lookup, "_fetch_tokens", lambda *_a, **_k: [])
    _argv(monkeypatch, "rwa_lookup.py", "--query", NON_ASCII, "--no-verify", "--no-cards")

    with CodePageStdout() as code_page_stdout:
        assert rwa_lookup.main() == 0
    assert code_page_stdout.payload()["query"] == NON_ASCII


# ── #2804: the sweep, and the guard that keeps it swept ─────────────────────
#
# The helper now lives in ``agentos.skills.stdio``. Every bundled script that
# touches stdout or stdin goes through it, or through one of the two inline
# forms earlier batches established. The test below is parametrised over the
# bundled tree at collection time, so a script added without the convention
# appears here on its own and fails.

import ast  # noqa: E402
import re  # noqa: E402

SCRIPTS = sorted(BUNDLED.glob("*/scripts/*.py"))
STDIO_IMPORT = re.compile(r"^from agentos\.skills\.stdio import ", re.M)
INLINE_FORMS = (
    "reconfigure(encoding",  # earlier batches: reconfigure in place
    "stdout.buffer",  # earlier batches: buffer write in place
    'getattr(sys.stdout, "buffer"',  # pptx/extract_text.py's spelling of it
)
STDOUT_USE = re.compile(r"\bprint\(|sys\.stdout\b|json\.dump\(")
STDIN_TEXT_READ = re.compile(r"sys\.stdin\.(read|readline|readlines)\(")


def _rel(path: Path) -> str:
    return path.relative_to(BUNDLED).as_posix()


def _uses_stdout(source: str) -> bool:
    return STDOUT_USE.search(source) is not None


def _reads_stdin_as_text(source: str) -> bool:
    return STDIN_TEXT_READ.search(source) is not None and "stdin.buffer" not in source


def _subprocess_text_calls(source: str) -> list[tuple[int, bool]]:
    """``(line, names_encoding)`` for every subprocess call that decodes as text."""
    calls: list[tuple[int, bool]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in {"run", "check_output", "Popen", "check_call"}:
            continue
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        text_mode = any(
            isinstance(keywords.get(k), ast.Constant) and keywords[k].value is True
            for k in ("text", "universal_newlines")
        )
        spreads_utf8 = any(k.arg is None for k in node.keywords)  # **SUBPROCESS_UTF8
        if text_mode or "encoding" in keywords or spreads_utf8:
            calls.append((node.lineno, "encoding" in keywords or spreads_utf8))
    return calls


@pytest.mark.parametrize("script", SCRIPTS, ids=_rel)
def test_every_bundled_script_follows_the_utf8_stdio_convention(script: Path) -> None:
    """The guard. A script that writes to stdout must import the shared
    helper (and call it), or carry one of the inline forms earlier batches
    used. A script that does no stdout or stdin I/O at all is exempt."""
    source = script.read_text(encoding="utf-8")
    if not _uses_stdout(source) and not STDIN_TEXT_READ.search(source):
        return  # a helper module, not a script that emits anything
    shared = STDIO_IMPORT.search(source) is not None
    inline = any(form in source for form in INLINE_FORMS)
    assert shared or inline, f"{_rel(script)} writes to stdout without the UTF-8 convention"
    if shared:
        assert re.search(r"\b(configure_utf8_stdio|write_stdout|_write_stdout)\(", source), (
            f"{_rel(script)} imports the shared helper but never calls it"
        )


@pytest.mark.parametrize("script", SCRIPTS, ids=_rel)
def test_a_script_reading_stdin_as_text_configures_stdin(script: Path) -> None:
    """Point 2 of #2804: a piped payload is UTF-8 whatever the console is."""
    source = script.read_text(encoding="utf-8")
    if not _reads_stdin_as_text(source):
        return
    assert "configure_utf8_stdio(stdin=True)" in source or "stdin.reconfigure(" in source, (
        f"{_rel(script)} reads sys.stdin as text through the console code page"
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=_rel)
def test_a_text_mode_subprocess_names_its_encoding(script: Path) -> None:
    """Point 3 of #2804: ``text=True`` alone inherits the locale."""
    source = script.read_text(encoding="utf-8")
    for line, names_encoding in _subprocess_text_calls(source):
        assert names_encoding, f"{_rel(script)}:{line} decodes a child through the locale"


def test_the_guard_actually_sees_every_script() -> None:
    """If the glob ever stops matching, the guard passes vacuously; pin the
    count the sweep was done against so a silent zero is caught."""
    assert len(SCRIPTS) >= 50


def test_the_shared_helper_is_not_copied_anywhere() -> None:
    """#2804: one copy, in ``agentos.skills.stdio``."""
    copies = [_rel(s) for s in SCRIPTS if "def _write_stdout(" in s.read_text(encoding="utf-8")]

    assert copies == []


def test_the_four_pipe_receivers_named_in_the_issue_configure_stdin() -> None:
    for rel in (
        "gmgn-market/scripts/kline_chart.py",
        "gmgn-token/scripts/kline_chart.py",
        "robinhood-chain-stocks/scripts/chain_cards.py",
        "robinhood-rwa-addresses/scripts/rwa_cards.py",
    ):
        source = (BUNDLED / rel).read_text(encoding="utf-8")
        assert "configure_utf8_stdio(stdin=True)" in source, rel


def test_the_gmgn_cli_calls_named_in_the_issue_decode_as_utf8() -> None:
    for rel in (
        "gmgn-wallet-score/scripts/score.py",
        "gmgn-holder-analysis/scripts/analyze.py",
        "gmgn-wallet-analysis/scripts/analyze.py",
    ):
        source = (BUNDLED / rel).read_text(encoding="utf-8")
        assert "text=True" not in source, rel
        assert 'encoding="utf-8", errors="replace"' in source, rel


# ── behaviour, on a simulated code page, for the newly covered scripts ─────


class CodePageStdin:
    """A ``sys.stdin`` that would decode a UTF-8 pipe through a legacy page."""

    def __init__(self, payload: str, encoding: str = "cp1252") -> None:
        self.stream = io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")), encoding=encoding)
        self._saved: Any = None

    def __enter__(self) -> CodePageStdin:
        self._saved = sys.stdin
        sys.stdin = self.stream
        return self

    def __exit__(self, *_exc: Any) -> None:
        sys.stdin = self._saved


def test_chain_cards_decodes_a_utf8_pipe_and_names_its_output_as_utf8(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both halves of #2781: the piped query survives, and the output path is
    printed as UTF-8."""
    chain_cards = _load("robinhood-chain-stocks/scripts/chain_cards.py", "chain_cards_2804")
    output = tmp_path / f"{NON_ASCII}.json"
    payload = json.dumps({"query": NON_ASCII, "token": {"isStockToken": True, "name": NON_ASCII}})
    _argv(monkeypatch, "chain_cards.py", "--output", str(output))

    with CodePageStdin(payload), CodePageStdout() as code_page_stdout:
        assert chain_cards.main() == 0

    assert code_page_stdout.text().startswith(f"publish_artifact path={output}")
    assert json.loads(output.read_text(encoding="utf-8"))["title"].endswith(NON_ASCII)


def test_rwa_cards_decodes_a_utf8_pipe_as_utf8(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rwa_cards = _load("robinhood-rwa-addresses/scripts/rwa_cards.py", "rwa_cards_2804")
    output = tmp_path / "cards.json"
    payload = json.dumps(
        {"query": NON_ASCII, "matches": [{"symbol": "AAPL", "name": NON_ASCII, "address": "0x1"}]}
    )
    _argv(monkeypatch, "rwa_cards.py", "--output", str(output))

    with CodePageStdin(payload), CodePageStdout() as code_page_stdout:
        rc = rwa_cards.main()

    assert rc == 0
    assert "publish_artifact" in code_page_stdout.text()
    assert NON_ASCII in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("skill", ["gmgn-market", "gmgn-token"])
def test_kline_chart_decodes_a_utf8_pipe_and_prints_its_path_as_utf8(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, skill: str
) -> None:
    """#2783: the piped kline JSON, and the publish line naming the output."""
    kline_chart = _load(f"{skill}/scripts/kline_chart.py", f"kline_chart_{skill}_2804")
    output = tmp_path / f"{NON_ASCII}.svg"
    rows = [
        {
            "time": 1_700_000_000 + i * 60,
            "open": 1,
            "high": 2,
            "low": 0.5,
            "close": 1.5,
            "volume": 10,
        }
        for i in range(5)
    ]
    _argv(monkeypatch, "kline_chart.py", "--input", "-", "--output", str(output))

    with CodePageStdin(json.dumps(rows)), CodePageStdout() as code_page_stdout:
        rc = kline_chart.main()

    assert rc == 0, code_page_stdout.text()
    assert f"publish_artifact path={output}" in code_page_stdout.text()


def test_inspect_xlsx_emits_cjk_cells_as_utf8(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openpyxl import Workbook

    inspect_xlsx = _load("xlsx/scripts/inspect_xlsx.py", "inspect_xlsx_2804")
    book = tmp_path / "book.xlsx"
    workbook = Workbook()
    workbook.active.append([NON_ASCII, 1])
    workbook.save(str(book))
    _argv(monkeypatch, "inspect_xlsx.py", str(book))

    with CodePageStdout() as code_page_stdout:
        assert inspect_xlsx.main() == 0

    assert NON_ASCII in code_page_stdout.text()


def test_build_srt_prints_a_non_ascii_output_path_as_utf8(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build_srt = _load("srt-from-script/scripts/build_srt.py", "build_srt_2804")
    script = tmp_path / "script.txt"
    script.write_text(f"=== SHOT_1 ===\nDURATION_S: 3\nVOICEOVER: {NON_ASCII}\n", encoding="utf-8")
    out = tmp_path / f"{NON_ASCII}.srt"
    _argv(monkeypatch, "build_srt.py", "--script", str(script), "--out", str(out))

    with CodePageStdout() as code_page_stdout:
        assert build_srt.main() == 0

    assert code_page_stdout.text().strip() == str(out)
    assert NON_ASCII in out.read_text(encoding="utf-8")


def test_plan_prints_a_non_ascii_plan_path_as_utf8(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _load("deep-research/scripts/plan.py", "plan_2804")
    out = tmp_path / f"{NON_ASCII}.json"
    _argv(monkeypatch, "plan.py", "--question", NON_ASCII, "--out", str(out))

    with CodePageStdout() as code_page_stdout:
        assert plan.main() == 0

    assert json.loads(code_page_stdout.text())["plan_path"] == str(out)


def test_weather_fetch_emits_a_non_ascii_location_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2713, with the network stubbed at the one function that touches it."""
    weather = _load("weather/scripts/weather_fetch.py", "weather_fetch_2804")
    monkeypatch.setattr(
        weather,
        "_fetch_wttr_json",
        lambda *_a, **_k: {
            "current_condition": [{"weatherDesc": [{"value": NON_ASCII}], "temp_C": "20"}],
            "weather": [],
        },
    )
    _argv(monkeypatch, "weather_fetch.py", "--location", NON_ASCII)

    with CodePageStdout() as code_page_stdout:
        assert weather.main() == 0

    assert NON_ASCII in code_page_stdout.text()


def test_http_fetch_writes_a_utf8_body_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2643's encoding half: a UTF-8 response body reaches stdout intact."""
    http_fetch = _load("http-fetch/scripts/http_fetch.py", "http_fetch_2804")
    monkeypatch.setattr(
        http_fetch, "_fetch", lambda *_a, **_k: (200, NON_ASCII.encode("utf-8"), "OK")
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    _argv(monkeypatch, "http_fetch.py", "--url", "https://example.test/")

    with CodePageStdout() as code_page_stdout:
        rc = http_fetch.main()

    assert rc == 0
    assert code_page_stdout.text() == NON_ASCII


def test_watch_rss_reports_a_cjk_title_as_utf8(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    watch_rss = _load("cron-watchers/scripts/watch_rss.py", "watch_rss_2804")
    feed = (
        '<?xml version="1.0"?><rss><channel><item><title>'
        f"{NON_ASCII}</title><link>https://x/1</link><guid>1</guid></item></channel></rss>"
    ).encode()

    class _Response:
        def read(self) -> bytes:
            return feed

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

    monkeypatch.setattr(watch_rss.urllib.request, "urlopen", lambda *_a, **_k: _Response())
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    _argv(
        monkeypatch, "watch_rss.py", "--url", "https://x/feed", "--name", "t", "--first-run-reports"
    )

    with CodePageStdout() as code_page_stdout:
        assert watch_rss.main() == 0

    assert f"- {NON_ASCII}" in code_page_stdout.text()


def test_a_configured_script_keeps_working_under_pytest_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without a simulated code page, pytest's capture has no ``reconfigure``;
    the helper must leave it alone and the script must still print."""
    plan = _load("deep-research/scripts/plan.py", "plan_2804_capsys")
    out = tmp_path / "plan.json"
    _argv(monkeypatch, "plan.py", "--question", "q", "--out", str(out))

    assert plan.main() == 0
    assert json.loads(capsys.readouterr().out)["plan_path"] == str(out)
