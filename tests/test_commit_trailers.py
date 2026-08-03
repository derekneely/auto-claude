"""Commits must not carry AI-attribution trailers.

Claude Code's own system prompt tells it to end every commit message with a
`Co-Authored-By: Claude ...` trailer, and the agent commits inside the worktree
before the daemon ever sees the branch. The prompt now forbids it, but a prompt
is a request; these tests cover the mechanical scrub that runs immediately
before `git push`, so a non-compliant agent — or a future model with different
defaults — cannot land the trailer in a public repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import worker


# ---------------------------------------------------------------------------
# Message-level stripping
# ---------------------------------------------------------------------------

def test_strips_claude_co_authored_by():
    msg = (
        "feat(jobs): add a progress overlay (#215) (ai-cc)\n"
        "\n"
        "Body line.\n"
        "\n"
        "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n"
    )
    assert worker._strip_ai_attribution(msg) == (
        "feat(jobs): add a progress overlay (#215) (ai-cc)\n"
        "\n"
        "Body line.\n"
    )


@pytest.mark.parametrize("trailer", [
    "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
    "Co-authored-by: Claude Opus 5 (1M context) <noreply@anthropic.com>",
    "co-authored-by: Claude <noreply@anthropic.com>",
    "Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>",
    "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
    "Generated with Claude Code",
])
def test_strips_every_known_attribution_form(trailer):
    msg = f"fix: something (ai-cc)\n\n{trailer}\n"
    assert worker._strip_ai_attribution(msg) == "fix: something (ai-cc)\n"


def test_keeps_a_human_co_author():
    """Only AI attribution goes. A real co-author is a real credit."""
    msg = "fix: something (ai-cc)\n\nCo-Authored-By: Derek Neely <djneely@gmail.com>\n"
    assert worker._strip_ai_attribution(msg) == msg


def test_leaves_a_clean_message_untouched():
    msg = "feat(db)!: the database is mandatory (ai-cc)\n\nWhy this matters.\n"
    assert worker._strip_ai_attribution(msg) == msg


def test_never_empties_the_subject():
    """A message that is nothing but a trailer still has to commit."""
    out = worker._strip_ai_attribution(
        "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n"
    )
    assert out.strip() != ""


# ---------------------------------------------------------------------------
# Branch-level rewrite
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.name", "Base")
    _git(r, "config", "user.email", "base@example.com")
    (r / "README.md").write_text("base\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "chore: base")
    _git(r, "switch", "-c", "ac/issue-1-thing")
    return r


def _commit(repo: Path, name: str, message: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def test_rewrites_only_the_offending_commits(repo: Path):
    _commit(repo, "a.txt", "feat: a (ai-cc)")
    _commit(
        repo, "b.txt",
        "feat: b (ai-cc)\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
    )
    _commit(repo, "c.txt", "feat: c (ai-cc)")

    tree_before = _git(repo, "rev-parse", "HEAD^{tree}")

    scrubbed = worker._scrub_ai_trailers(repo, "ac/issue-1-thing", "main", logger=None)

    assert scrubbed == 1
    log = _git(repo, "log", "main..HEAD", "--format=%B")
    assert "Co-Authored-By" not in log
    assert "feat: a (ai-cc)" in log and "feat: b (ai-cc)" in log and "feat: c (ai-cc)" in log
    # The content of the branch is untouched — only messages were rewritten.
    assert _git(repo, "rev-parse", "HEAD^{tree}") == tree_before
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "ac/issue-1-thing"


def test_no_offending_commits_is_a_no_op(repo: Path):
    _commit(repo, "a.txt", "feat: a (ai-cc)")
    head_before = _git(repo, "rev-parse", "HEAD")

    assert worker._scrub_ai_trailers(repo, "ac/issue-1-thing", "main", logger=None) == 0
    assert _git(repo, "rev-parse", "HEAD") == head_before


def test_preserves_author_identity_and_date(repo: Path):
    _git(repo, "config", "user.name", "Bot Account")
    _git(repo, "config", "user.email", "bot@example.com")
    _commit(
        repo, "a.txt",
        "feat: a (ai-cc)\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
    )
    before = _git(repo, "log", "-1", "--format=%an%x00%ae%x00%aI")

    worker._scrub_ai_trailers(repo, "ac/issue-1-thing", "main", logger=None)

    assert _git(repo, "log", "-1", "--format=%an%x00%ae%x00%aI") == before


def test_scopes_the_rewrite_to_unpushed_commits(repo: Path):
    """Rework pushes onto a live branch — already-published commits stay put."""
    _commit(repo, "a.txt", "feat: a (ai-cc)")
    published = _git(repo, "rev-parse", "HEAD")
    _commit(
        repo, "b.txt",
        "feat: b (ai-cc)\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
    )

    worker._scrub_ai_trailers(repo, "ac/issue-1-thing", published, logger=None)

    assert _git(repo, "rev-parse", "HEAD~1") == published
