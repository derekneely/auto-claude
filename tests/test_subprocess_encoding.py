"""Guard against the recurring Windows cp1252 subprocess bug.

`text=True` alone makes Python decode a subprocess's output with the *locale*
codec. On Windows that is cp1252, which raises UnicodeDecodeError on any byte it
cannot map - killing subprocess's reader thread and leaving `stdout` as None. The
caller then fails with a baffling `AttributeError: 'NoneType' has no attribute
'strip'` far from the real cause.

Every `subprocess.run`/`Popen` that captures text output must therefore pass an
explicit `encoding`. This has already been fixed twice in this project's history,
so it is enforced here rather than left to review.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CALL_RE = re.compile(r"subprocess\.(run|Popen)\(", re.M)


def _source_files() -> list[Path]:
    return sorted(
        p for p in PROJECT_ROOT.glob("*.py")
        if p.name != "conftest.py"
    )


def _call_blocks(src: str):
    """Yield (line_number, argument_text) for each subprocess call."""
    for m in CALL_RE.finditer(src):
        start = m.end() - 1  # at the opening paren
        depth = 0
        for i in range(start, len(src)):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    yield src[:m.start()].count("\n") + 1, src[start:i]
                    break


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_every_text_subprocess_call_sets_encoding(path: Path):
    src = path.read_text(encoding="utf-8")
    offenders = []
    for line, block in _call_blocks(src):
        captures_text = "text=True" in block or "universal_newlines=True" in block
        if captures_text and "encoding=" not in block:
            offenders.append(f"{path.name}:{line}")

    assert not offenders, (
        "subprocess call(s) decode with the locale codec (cp1252 on Windows). "
        "Add encoding='utf-8', errors='replace': " + ", ".join(offenders)
    )


def test_the_checker_actually_detects_a_violation():
    """Make sure the guard above can fail - a scanner that never fires is useless."""
    bad = "subprocess.run(cmd, text=True, capture_output=True)"
    blocks = list(_call_blocks(bad))
    assert blocks, "scanner failed to find the call at all"
    _line, block = blocks[0]
    assert "text=True" in block and "encoding=" not in block


def test_the_checker_accepts_a_correct_call():
    good = "subprocess.run(cmd, text=True, encoding='utf-8', errors='replace')"
    _line, block = next(_call_blocks(good))
    assert "encoding=" in block
