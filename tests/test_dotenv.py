"""Tests for `.env` loading and metrics-DB injection.

The metrics DB URL is a secret that has to reach a Node subprocess in a
different repository. Getting it there wrongly is silent - `log-event.mjs`
warns and exits 0 - so the plumbing is worth pinning down.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import integrations  # noqa: E402
from ghauth import load_dotenv  # noqa: E402


class TestLoadDotenv:
    def test_missing_file_is_empty(self, tmp_path):
        assert load_dotenv(tmp_path) == {}

    def test_reads_key_values(self, tmp_path):
        (tmp_path / ".env").write_text("A=1\nB=two\n", encoding="utf-8")
        assert load_dotenv(tmp_path, environ={}) == {"A": "1", "B": "two"}

    def test_ignores_comments_and_blanks(self, tmp_path):
        (tmp_path / ".env").write_text("# note\n\nA=1\n", encoding="utf-8")
        assert load_dotenv(tmp_path, environ={}) == {"A": "1"}

    def test_ignores_lines_without_an_equals(self, tmp_path):
        (tmp_path / ".env").write_text("garbage\nA=1\n", encoding="utf-8")
        assert load_dotenv(tmp_path, environ={}) == {"A": "1"}

    def test_keeps_equals_inside_the_value(self, tmp_path):
        # Connection strings routinely contain '=' in query parameters.
        (tmp_path / ".env").write_text(
            "URL=postgresql://u:p@h/db?sslmode=require\n", encoding="utf-8"
        )
        got = load_dotenv(tmp_path, environ={})
        assert got["URL"] == "postgresql://u:p@h/db?sslmode=require"

    def test_strips_surrounding_quotes(self, tmp_path):
        (tmp_path / ".env").write_text('A="quoted"\n', encoding="utf-8")
        assert load_dotenv(tmp_path, environ={}) == {"A": "quoted"}

    def test_tolerates_a_bom(self, tmp_path):
        # PowerShell's Set-Content writes one; str.strip() does not remove it.
        (tmp_path / ".env").write_bytes(b"\xef\xbb\xbfA=1\n")
        assert load_dotenv(tmp_path, environ={}) == {"A": "1"}

    def test_tolerates_crlf(self, tmp_path):
        (tmp_path / ".env").write_bytes(b"A=1\r\nB=2\r\n")
        assert load_dotenv(tmp_path, environ={}) == {"A": "1", "B": "2"}

    def test_existing_environment_wins(self, tmp_path):
        # So an operator can override one value without editing the file.
        (tmp_path / ".env").write_text("A=fromfile\n", encoding="utf-8")
        assert load_dotenv(tmp_path, environ={"A": "fromenv"}) == {}


class TestMetricsEnv:
    def test_injects_the_url_when_set(self, monkeypatch):
        monkeypatch.setenv(integrations.METRICS_DB_ENV_VAR, "postgresql://x/y")
        env = integrations.metrics_env(base={})
        assert env[integrations.METRICS_DB_ENV_VAR] == "postgresql://x/y"

    def test_omits_the_key_when_unset(self, monkeypatch):
        monkeypatch.delenv(integrations.METRICS_DB_ENV_VAR, raising=False)
        assert integrations.METRICS_DB_ENV_VAR not in integrations.metrics_env(base={})

    def test_blank_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv(integrations.METRICS_DB_ENV_VAR, "   ")
        assert integrations.METRICS_DB_ENV_VAR not in integrations.metrics_env(base={})

    def test_preserves_the_base_environment(self, monkeypatch):
        monkeypatch.setenv(integrations.METRICS_DB_ENV_VAR, "postgresql://x/y")
        env = integrations.metrics_env(base={"GH_TOKEN": "t"})
        assert env["GH_TOKEN"] == "t"

    def test_does_not_mutate_the_base(self, monkeypatch):
        monkeypatch.setenv(integrations.METRICS_DB_ENV_VAR, "postgresql://x/y")
        base = {"GH_TOKEN": "t"}
        integrations.metrics_env(base=base)
        assert base == {"GH_TOKEN": "t"}
