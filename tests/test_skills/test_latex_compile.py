"""Offline unit tests for the archived latex-compile skill scripts.

Each test runs the wrapped CLI directly via subprocess (or imports the
script module), no LLM, no orchestrator. latex-compile lives in the
exp/ archive; the tests keep its compile contract honest while it is
not shipped as a bundled skill.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"
EXP = ROOT / "src" / "agentos" / "skills" / "exp"


def _skill_dir(name: str) -> Path:
    """Resolve a skill's directory from bundled or the exp/ archive."""
    bundled_path = BUNDLED / name
    if bundled_path.is_dir():
        return bundled_path
    return EXP / name


def test_latex_compile_produces_pdf(tmp_path: Path) -> None:
    pytest = __import__("pytest")
    if shutil.which("xelatex") is None:
        pytest.skip("xelatex not installed")

    tex = tmp_path / "paper.tex"
    tex.write_text(
        r"""\documentclass{article}
\begin{document}
Hello, world.
\end{document}
""",
        encoding="utf-8",
    )
    script = _skill_dir("latex-compile") / "scripts" / "compile.py"
    proc = subprocess.run(
        [sys.executable, str(script), str(tex)],
        check=True,
        capture_output=True,
        text=True,
    )
    pdf = tmp_path / "paper.pdf"
    assert pdf.is_file()
    assert pdf.read_bytes()[:4] == b"%PDF"
    # stdout is the clean user-facing deliverable line (PDF path + size).
    # The verbose xelatex log tail is routed to stderr so it survives for
    # debugging without polluting the meta-skill's final_text payload.
    assert "paper.pdf" in proc.stdout.lower()
    assert "successfully" in proc.stdout.lower()


def test_latex_compile_reassembles_clean_cjk_paper_from_section_files(
    tmp_path: Path,
) -> None:
    from importlib.util import module_from_spec, spec_from_file_location

    script = _skill_dir("latex-compile") / "scripts" / "compile.py"
    spec = spec_from_file_location("latex_compile_script", script)
    assert spec is not None and spec.loader is not None
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)

    workspace = tmp_path / "workspace"
    paper_dir = workspace / "paper"
    paper_dir.mkdir(parents=True)
    tex = paper_dir / "paper.tex"
    tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "Let me write the paper first. ```latex\\n"
        "\\section{Method} 污染内容\\n```"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (workspace / "abstract.tex").write_text(
        "\\begin{abstract} 中文摘要。\\end{abstract}\n",
        encoding="utf-8",
    )
    (workspace / "introduction.tex").write_text(
        "\\section{Introduction} Clean intro.\n",
        encoding="utf-8",
    )
    (paper_dir / "method.tex").write_text(
        "\\section{实验方法} 中文方法。\n",
        encoding="utf-8",
    )
    (workspace / "results.tex").write_text(
        "\\section{Results} Clean results.\n",
        encoding="utf-8",
    )
    (workspace / "discussion.tex").write_text(
        "\\section{Discussion} Clean discussion.\n",
        encoding="utf-8",
    )
    (paper_dir / "references.bib").write_text("", encoding="utf-8")

    assert mod._prepare_tex_for_compile(tex) is True
    rewritten = tex.read_text(encoding="utf-8")
    assert "\\usepackage{xeCJK}" in rewritten
    assert "\\setCJKmainfont" in rewritten
    assert "\\section{实验方法} 中文方法。" in rewritten
    assert "Let me write the paper first" not in rewritten
    assert "```latex" not in rewritten


def test_latex_compile_keeps_clean_revised_body_over_section_files(
    tmp_path: Path,
) -> None:
    from importlib.util import module_from_spec, spec_from_file_location

    script = _skill_dir("latex-compile") / "scripts" / "compile.py"
    spec = spec_from_file_location("latex_compile_script", script)
    assert spec is not None and spec.loader is not None
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)

    workspace = tmp_path / "workspace"
    paper_dir = workspace / "paper"
    paper_dir.mkdir(parents=True)
    tex = paper_dir / "paper.tex"
    tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\begin{abstract} Final abstract.\\end{abstract}\n"
        "\\section{Introduction} Revised intro.\n"
        "\\section{Method} Revised method.\n"
        "\\section{Results} Revised results.\n"
        "\\section{Discussion} Revised discussion.\n"
        "\\bibliography{references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (workspace / "introduction.tex").write_text(
        "\\section{Introduction} Stale intro.\n",
        encoding="utf-8",
    )
    (paper_dir / "method.tex").write_text(
        "\\section{Method} Stale method.\n",
        encoding="utf-8",
    )
    (workspace / "results.tex").write_text(
        "\\section{Results} Stale results.\n",
        encoding="utf-8",
    )
    (workspace / "discussion.tex").write_text(
        "\\section{Discussion} Stale discussion.\n",
        encoding="utf-8",
    )
    (workspace / "abstract.tex").write_text(
        "\\begin{abstract} Stale abstract.\\end{abstract}\n",
        encoding="utf-8",
    )

    assert mod._prepare_tex_for_compile(tex) is False
    rewritten = tex.read_text(encoding="utf-8")
    assert "Revised intro" in rewritten
    assert "Stale intro" not in rewritten


def test_latex_compile_validates_long_paper_citation_contract(
    tmp_path: Path,
) -> None:
    from importlib.util import module_from_spec, spec_from_file_location

    script = _skill_dir("latex-compile") / "scripts" / "compile.py"
    spec = spec_from_file_location("latex_compile_script", script)
    assert spec is not None and spec.loader is not None
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)

    tex = tmp_path / "paper.tex"
    tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{Introduction} Too few refs \\cite{ref1,ref2}.\n"
        "\\bibliography{references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "references.bib").write_text(
        "\n".join(
            f"@misc{{ref{i}, title={{Reference {i}}}, year={{2026}}}}"
            for i in range(1, 25)
        ),
        encoding="utf-8",
    )

    errors = mod._validate_citation_contract(tex, min_cited_refs=20)
    assert any("at least 20 cited references" in error for error in errors)

    tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{Introduction} "
        + " ".join(f"\\cite{{ref{i}}}" for i in range(1, 21))
        + " \\cite{missing_ref}.\n"
        "\\bibliography{references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    errors = mod._validate_citation_contract(tex, min_cited_refs=20)
    assert any("undefined citation keys: missing_ref" in error for error in errors)

    tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{Introduction} "
        + " ".join(f"\\cite{{ref{i}}}" for i in range(1, 21))
        + ".\n"
        "\\bibliography{references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    assert mod._validate_citation_contract(tex, min_cited_refs=20) == []


def test_latex_compile_parses_minimum_page_contract() -> None:
    from importlib.util import module_from_spec, spec_from_file_location

    script = _skill_dir("latex-compile") / "scripts" / "compile.py"
    spec = spec_from_file_location("latex_compile_script", script)
    assert spec is not None and spec.loader is not None
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)

    short_log = "Output written on paper.pdf (9 pages, 12345 bytes)."
    long_log = "Output written on paper.pdf (11 pages, 67890 bytes)."
    assert mod._validate_page_contract(short_log, min_pages=10) == [
        "paper must be at least 10 pages; compiled PDF has 9 pages"
    ]
    assert mod._validate_page_contract(long_log, min_pages=10) == []
