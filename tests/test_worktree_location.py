"""Worktrees live under auto-claude's own `worktrees/`, never inside a clone.

The sibling toolchain puts its worktrees at `.claude/worktrees/slot-N` *inside
the consuming repo*, and `pipeline.json` carries a `worktreeBase` field saying
so. auto-claude deliberately ignores that field and keeps every worktree under
its own `[paths].worktrees_dir`, laid out `worktrees/<repo>/issue-<n>` so it is
navigable and so the clones stay clean.

Honouring `worktreeBase` would scatter working trees inside `repos/*/.claude/`,
where they are hard to find, easy to wipe with a `git clean`, and liable to
collide with the loop's own slots on a repo both systems touch. This file pins
that, because it is a one-line change to "fix" and nothing else would notice.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker  # noqa: E402
from pipeline import parse_pipeline_config  # noqa: E402

SOURCE = (Path(__file__).resolve().parent.parent / "worker.py").read_text(encoding="utf-8")


class TestWorktreePathsComeFromAutoClaudeConfig:
    def test_every_worktree_path_is_built_from_worktrees_dir(self):
        """Both the dev and the review worker must root their tree there."""
        assert SOURCE.count("ctx.worktrees_dir /") == 2

    def test_dev_worktree_layout_is_repo_then_issue(self):
        assert 'ctx.worktrees_dir / ctx.repo / f"issue-{ctx.number}"' in SOURCE

    def test_review_worktree_is_distinguishable_from_the_dev_one(self):
        assert 'ctx.worktrees_dir / ctx.repo / f"issue-{ctx.number}-review"' in SOURCE

    def test_worker_never_references_the_sibling_slot_layout(self):
        """`.claude/worktrees` may appear in prose, but never as a built path."""
        assert '".claude/worktrees"' not in SOURCE
        assert "'.claude/worktrees'" not in SOURCE

    def test_worker_never_reads_worktree_base_from_pipeline_json(self):
        assert "worktree_base" not in SOURCE


class TestWorktreeBaseIsParsedButNotHonoured:
    """We still parse the field — the sibling toolchain owns the schema and a
    strict parser would reject its files — we just never act on it."""

    def test_it_is_still_parsed(self):
        cfg = parse_pipeline_config(
            '{"project": "field_admin", "worktreeBase": ".claude/worktrees"}',
            source="test",
        )
        assert cfg.worktree_base == ".claude/worktrees"

    def test_a_hostile_value_cannot_escape_our_worktrees_dir(self, tmp_path):
        """Even if pipeline.json asked for an absolute path, the worker builds
        its path from ctx.worktrees_dir and never consults the field."""
        cfg = parse_pipeline_config(
            '{"project": "field_admin", "worktreeBase": "C:/somewhere/else"}',
            source="test",
        )
        assert cfg.worktree_base == "C:/somewhere/else"
        ctx = worker.IssueContext(
            issue_id="field_admin#215", repo="field_admin", number=215,
            title="t", body="", action="implement", org="Accelevation",
            base_branch="dev", repos_dir=tmp_path, worktrees_dir=tmp_path / "worktrees",
            prompts_dir=tmp_path, dev_model="m", light_model="m",
            permission_mode="bypassPermissions", max_budget_usd=1.0, max_turns=1,
            crash_logs_dir=tmp_path, color_name="blue", color_code="x",
        )
        built = ctx.worktrees_dir / ctx.repo / f"issue-{ctx.number}"
        assert built == tmp_path / "worktrees" / "field_admin" / "issue-215"
        assert tmp_path / "worktrees" in built.parents
