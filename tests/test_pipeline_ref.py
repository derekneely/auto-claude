"""Tests for which git ref `pipeline.json` is read from.

GitHub's contents API defaults to the repo's **default branch**. For
`Accelevation/field_admin` that is `main`, which trails the active integration
branch `dev` by a release cycle — so `prBaseBranch` was read as `derekdev` for
days after `dev` had been corrected to `dev`. The sibling toolchain never hits
this because it reads the file out of the operator's working checkout.

So: prefer the configured base branch, fall back to the default branch.
"""

from __future__ import annotations

import queue
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from process_manager import ProcessManager  # noqa: E402


class FakeLogger:
    def __init__(self):
        self.messages: list[str] = []

    def info(self, msg):
        self.messages.append(msg)

    def warn(self, msg):
        self.messages.append(msg)

    def error(self, msg):
        self.messages.append(msg)

    def text(self) -> str:
        return " | ".join(self.messages)


class FakeGithub:
    """Serves pipeline.json per-ref. `None` ref means the default branch."""

    def __init__(self, by_ref: dict[str | None, str | None]):
        self._by_ref = by_ref
        self.reads: list[str | None] = []

    def get_file(self, _repo, _path, ref=None):
        self.reads.append(ref)
        if ref not in self._by_ref:
            return None
        return self._by_ref[ref]


def _pipeline_json(pr_base: str) -> str:
    return '{"project": "field_admin", "prBaseBranch": "%s"}' % pr_base


def make_pm(github, base_branch="dev"):
    config = SimpleNamespace(
        workers=SimpleNamespace(max_parallel=3, shutdown_grace_seconds=30),
        github=SimpleNamespace(org="Accelevation", base_branch=base_branch),
    )
    pm = ProcessManager(
        config=config,
        state=SimpleNamespace(),
        logger=FakeLogger(),
        log_queue=queue.Queue(),
        state_queue=queue.Queue(),
    )
    pm._github = github
    return pm


class TestPrefersTheBaseBranch:
    def test_reads_the_base_branch_copy_not_the_default_branch(self):
        # The exact field_admin situation: main says derekdev, dev says dev.
        gh = FakeGithub({"dev": _pipeline_json("dev"), None: _pipeline_json("derekdev")})
        pm = make_pm(gh, base_branch="dev")
        assert pm._repo_pipeline("field_admin").pr_base_branch == "dev"

    def test_asks_for_the_base_branch_first(self):
        gh = FakeGithub({"dev": _pipeline_json("dev"), None: _pipeline_json("derekdev")})
        make_pm(gh, base_branch="dev")._repo_pipeline("field_admin")
        assert gh.reads[0] == "dev"

    def test_does_not_read_the_default_branch_when_the_base_branch_has_it(self):
        gh = FakeGithub({"dev": _pipeline_json("dev"), None: _pipeline_json("derekdev")})
        make_pm(gh, base_branch="dev")._repo_pipeline("field_admin")
        assert None not in gh.reads


class TestFallsBackToTheDefaultBranch:
    def test_uses_the_default_branch_when_the_base_branch_lacks_the_file(self):
        gh = FakeGithub({None: _pipeline_json("derekdev")})
        pm = make_pm(gh, base_branch="dev")
        assert pm._repo_pipeline("field_admin").pr_base_branch == "derekdev"

    def test_returns_none_when_neither_ref_has_it(self):
        gh = FakeGithub({})
        pm = make_pm(gh, base_branch="dev")
        assert pm._repo_pipeline("field_admin") is None

    def test_warns_when_neither_ref_has_it(self):
        gh = FakeGithub({})
        pm = make_pm(gh, base_branch="dev")
        pm._repo_pipeline("field_admin")
        assert "pipeline.json" in pm._logger.text()

    def test_no_base_branch_configured_still_reads_the_default_branch(self):
        gh = FakeGithub({None: _pipeline_json("derekdev")})
        pm = make_pm(gh, base_branch=None)
        assert pm._repo_pipeline("field_admin").pr_base_branch == "derekdev"


class TestCaching:
    def test_result_is_cached_across_calls(self):
        gh = FakeGithub({"dev": _pipeline_json("dev")})
        pm = make_pm(gh, base_branch="dev")
        pm._repo_pipeline("field_admin")
        pm._repo_pipeline("field_admin")
        assert len(gh.reads) == 1

    def test_a_missing_file_is_also_cached(self):
        """Otherwise every spawn re-reads and re-warns for a repo with no file."""
        gh = FakeGithub({})
        pm = make_pm(gh, base_branch="dev")
        pm._repo_pipeline("field_admin")
        pm._repo_pipeline("field_admin")
        assert len(gh.reads) == 2  # one attempt per ref, on the first call only
