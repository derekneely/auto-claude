# tests/test_db_config.py
"""Tests for config.py's [database] section.

The real config.toml on disk today has no [database] block at all, so
load_config must treat its absence as "use every default" rather than raising
— the existing `raw["github"]`-style splat would turn a missing block into a
TypeError the moment DatabaseConfig gained a single required field. This also
covers DatabaseConfig.url(), which reads the environment lazily so a value
set by main's .env loading (which runs before load_config) is visible.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DatabaseConfig, load_config  # noqa: E402

_MINIMAL_BODY = """
[github]
org = "Accelevation"
repos = ["field_admin"]
poll_interval_seconds = 60
base_branch = "dev"
label_prefix = "ac-"
needs_info_label = "ac-needs-info"
pr_created_label = "ac-pr-created"
in_progress_label = "ac-in-progress"
action_labels = ["ac-implement"]
dev_actions = ["implement"]
rework_label = "ac-rework"

[claude]
triage_model = "claude-haiku-4-5"
dev_model = "claude-sonnet-4-6"
permission_mode = "bypassPermissions"
max_budget_usd = 10.0
output_format = "stream-json"

[workers]
max_parallel = 3
max_continuations = 2
shutdown_grace_seconds = 30

[paths]
repos_dir = "repos"
worktrees_dir = "worktrees"
state_file = "state/issues.json"
log_file = "logs/auto-claude.log"
prompts_dir = "prompts"

[logging]
level = "INFO"
colorize = true
log_to_file = true
"""


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "config.toml"
    p.write_text(_MINIMAL_BODY + extra, encoding="utf-8")
    return p


class TestDatabaseSectionAbsent:
    def test_defaults_when_database_section_missing(self, tmp_path):
        # This is the real config.toml's shape today — no [database] block.
        cfg = load_config(_write_config(tmp_path))
        assert cfg.database.url_env == "PIPELINE_METRICS_DATABASE_URL"
        assert cfg.database.lease_ttl_seconds == 1800
        assert cfg.database.heartbeat_interval_seconds == 60
        assert cfg.database.connect_timeout_seconds == 10
        assert cfg.database.journal_file == tmp_path / "state" / "journal.jsonl"


class TestDatabaseSectionPresent:
    def test_overrides_are_applied(self, tmp_path):
        extra = (
            "\n[database]\n"
            "url_env = \"CUSTOM_DB_URL\"\n"
            "lease_ttl_seconds = 900\n"
            "heartbeat_interval_seconds = 30\n"
            "connect_timeout_seconds = 5\n"
        )
        cfg = load_config(_write_config(tmp_path, extra))
        assert cfg.database.url_env == "CUSTOM_DB_URL"
        assert cfg.database.lease_ttl_seconds == 900
        assert cfg.database.heartbeat_interval_seconds == 30
        assert cfg.database.connect_timeout_seconds == 5

    def test_the_removed_enabled_switch_is_a_hard_error(self, tmp_path):
        """The database is mandatory (ruling, 2026-07-31). `enabled` used to
        buy a DB-less run; silently ignoring a leftover `enabled = false`
        would let someone believe they still had that escape hatch."""
        extra = "\n[database]\nenabled = false\n"
        with pytest.raises(ValueError) as excinfo:
            load_config(_write_config(tmp_path, extra))
        assert "enabled" in str(excinfo.value)

    def test_enabled_true_is_also_rejected_rather_than_quietly_accepted(self, tmp_path):
        extra = "\n[database]\nenabled = true\n"
        with pytest.raises(ValueError):
            load_config(_write_config(tmp_path, extra))

    def test_journal_file_resolved_relative_to_project_root(self, tmp_path):
        extra = '\n[database]\njournal_file = "custom/journal.jsonl"\n'
        cfg = load_config(_write_config(tmp_path, extra))
        assert cfg.database.journal_file == tmp_path / "custom" / "journal.jsonl"


class TestDatabaseConfigUrl:
    def test_url_reads_from_environment(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_METRICS_DATABASE_URL", "postgresql://x/y")
        assert DatabaseConfig().url() == "postgresql://x/y"

    def test_url_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("PIPELINE_METRICS_DATABASE_URL", raising=False)
        assert DatabaseConfig().url() is None
