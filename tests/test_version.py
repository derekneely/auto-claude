# tests/test_version.py
"""Tests for version.py — the harness identity string recorded in
auto_claude.harness.version by db/harness.py (Task 6).

A harness with no discoverable version would make `run.model` / `harness.version`
useless for telling "which build produced this row" apart during an incident,
so the format is locked down here rather than left to drift.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import version  # noqa: E402


class TestVersion:
    def test_dunder_version_is_defined(self):
        assert hasattr(version, "__version__")

    def test_dunder_version_matches_semver(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", version.__version__)
