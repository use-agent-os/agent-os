from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).parent.parent

# Pattern to find shell script references in fenced code blocks
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_SCRIPT_REF_RE = re.compile(
    r"(?:^|\s)(?:bash|sh|pwsh|powershell)\s+([\w./\\-]+\.(?:sh|ps1))"
    r"|(?:^|\s)\.([\w./\\-]+\.(?:sh|ps1))"
    r"|(?:^|\s)\.\/([\w./\\-]+\.(?:sh|ps1))",
    re.MULTILINE,
)

_PORTABLE_DISCLAIMER_RE = re.compile(
    r"portable zip|Portable Zip|Available after the first GitHub Release",
    re.IGNORECASE,
)


def _get_readme_lines() -> list[str]:
    return (_ROOT / "README.md").read_text(encoding="utf-8").splitlines()


def _surrounding_lines(all_lines: list[str], line_idx: int, radius: int = 2) -> str:
    start = max(0, line_idx - radius)
    end = min(len(all_lines), line_idx + radius + 1)
    return "\n".join(all_lines[start:end])


def test_readme_script_references_exist_or_have_portable_disclaimer() -> None:
    readme_text = (_ROOT / "README.md").read_text(encoding="utf-8")
    all_lines = readme_text.splitlines()

    violations: list[str] = []

    for fence_match in _FENCE_RE.finditer(readme_text):
        block_content = fence_match.group(1)
        block_start_pos = fence_match.start(1)

        for ref_match in _SCRIPT_REF_RE.finditer(block_content):
            # Extract whichever capture group matched
            script_name = ref_match.group(1) or ref_match.group(2) or ref_match.group(3)
            if script_name is None:
                continue

            # Normalize path separators and strip leading ./
            script_name = script_name.lstrip("./").lstrip(".\\")

            # Absolute position in the readme
            abs_pos = block_start_pos + ref_match.start()
            line_idx = readme_text[:abs_pos].count("\n")

            # Check if file exists at repo root
            if (_ROOT / script_name).exists():
                continue

            # Check surrounding lines for portable disclaimer
            context = _surrounding_lines(all_lines, line_idx, radius=2)
            if _PORTABLE_DISCLAIMER_RE.search(context):
                continue

            violations.append(
                f"Line {line_idx + 1}: '{script_name}' not found at repo root "
                f"and no portable-zip disclaimer in surrounding 2 lines"
            )

    assert violations == [], "\n".join(violations)
