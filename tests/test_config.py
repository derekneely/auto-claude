"""Tests for config.py — [integrations] loading and base_branch fallback.

`base_branch` in [github] stays as the fallback for repos with no
`.claude/pipeline.json` (only one of four repos has one today), so this only
covers what's new: the optional [integrations].claude_tools_root key. Full
end-to-end parsing of every [section] is exercised implicitly by the daemon's
own config.toml at runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import IntegrationsConfig, load_config  # noqa: E402

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


class TestIntegrationsSectionAbsent:
    def test_defaults_to_none(self, tmp_path):
        cfg = load_config(_write_config(tmp_path))
        assert cfg.integrations == IntegrationsConfig(claude_tools_root=None)


class TestIntegrationsSectionPresent:
    def test_relative_path_resolved_against_project_root(self, tmp_path):
        extra = '\n[integrations]\nclaude_tools_root = "../accelevation-claude-tools"\n'
        cfg = load_config(_write_config(tmp_path, extra))

        # Matches _resolve_path's behaviour for every other [paths] entry:
        # joined against project_root, not further normalized/resolved.
        expected = tmp_path / "../accelevation-claude-tools"
        assert cfg.integrations.claude_tools_root == expected

    def test_absolute_path_kept_as_is(self, tmp_path):
        abs_path = (tmp_path / "somewhere" / "claude-tools").resolve()
        extra = f'\n[integrations]\nclaude_tools_root = "{abs_path.as_posix()}"\n'
        cfg = load_config(_write_config(tmp_path, extra))

        assert cfg.integrations.claude_tools_root == abs_path


class TestBaseBranchFallbackStillPresent:
    def test_github_base_branch_still_loads(self, tmp_path):
        # Per task B: base_branch remains the fallback for repos without a
        # .claude/pipeline.json - it must not be removed from GithubConfig.
        cfg = load_config(_write_config(tmp_path))
        assert cfg.github.base_branch == "dev"
