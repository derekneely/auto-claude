"""Tests for worktree preparation.

A `git worktree add` gives you source and nothing else — no `node_modules`, no
generated Prisma client, no gitignored env files. Every verify command in
`field_admin`'s pipeline.json needs all three. Reproduced before this existed:

    > nextn@0.1.0 typecheck
    > tsc --noEmit
    'tsc' is not recognized as an internal or external command

That failure is indistinguishable from a real one at the review stage, so a
perfectly good PR would fail review three times and land at `ac-blocked` with
feedback blaming the code. The sibling toolchain never hits it because
`test-pr-agent` does this setup explicitly; auto-claude has to do it itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worktree_setup import (  # noqa: E402
    RepoSetupConfig,
    copy_env_files,
    detect_setup_commands,
    resolve_setup_commands,
)


class TestDetectSetupCommands:
    def test_node_project_installs(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert "npm install" in detect_setup_commands(tmp_path)

    def test_prisma_project_also_generates_the_client(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        (tmp_path / "prisma").mkdir()
        (tmp_path / "prisma" / "schema.prisma").write_text("", encoding="utf-8")
        cmds = detect_setup_commands(tmp_path)
        assert "npm install" in cmds
        assert "npx prisma generate" in cmds

    def test_install_runs_before_generate(self, tmp_path):
        """prisma is a devDependency — generating before installing fails."""
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        (tmp_path / "prisma").mkdir()
        (tmp_path / "prisma" / "schema.prisma").write_text("", encoding="utf-8")
        cmds = detect_setup_commands(tmp_path)
        assert cmds.index("npm install") < cmds.index("npx prisma generate")

    def test_nested_package_is_installed_too(self, tmp_path):
        """field_admin's root tsconfig includes **/*.ts and only excludes
        node_modules, so it typechecks functions/src — whose deps live in
        functions/package.json. Without this, typecheck fails with
        'Cannot find module firebase-functions/v2/firestore'."""
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        (tmp_path / "functions").mkdir()
        (tmp_path / "functions" / "package.json").write_text("{}", encoding="utf-8")
        assert "npm --prefix functions install" in detect_setup_commands(tmp_path)

    def test_root_installs_before_nested(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        (tmp_path / "functions").mkdir()
        (tmp_path / "functions" / "package.json").write_text("{}", encoding="utf-8")
        cmds = detect_setup_commands(tmp_path)
        assert cmds.index("npm install") < cmds.index("npm --prefix functions install")

    def test_node_modules_is_not_a_nested_package(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "package.json").write_text("{}", encoding="utf-8")
        assert detect_setup_commands(tmp_path) == ("npm install",)

    def test_nested_packages_are_ordered_deterministically(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        for name in ["zebra", "alpha"]:
            (tmp_path / name).mkdir()
            (tmp_path / name / "package.json").write_text("{}", encoding="utf-8")
        cmds = detect_setup_commands(tmp_path)
        assert cmds.index("npm --prefix alpha install") < cmds.index("npm --prefix zebra install")

    def test_nesting_is_only_one_level_deep(self, tmp_path):
        """Bounded on purpose — a full walk would find every fixture package."""
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        (deep / "package.json").write_text("{}", encoding="utf-8")
        assert detect_setup_commands(tmp_path) == ("npm install",)

    def test_prisma_without_package_json_does_not_install(self, tmp_path):
        (tmp_path / "prisma").mkdir()
        (tmp_path / "prisma" / "schema.prisma").write_text("", encoding="utf-8")
        assert "npm install" not in detect_setup_commands(tmp_path)

    def test_gradle_project_needs_nothing(self, tmp_path):
        """Gradle resolves its own dependencies on first invocation."""
        (tmp_path / "gradlew").write_text("", encoding="utf-8")
        (tmp_path / "settings.gradle.kts").write_text("include(\":app\")", encoding="utf-8")
        assert detect_setup_commands(tmp_path) == ()

    def test_python_requirements(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        assert detect_setup_commands(tmp_path) == ("pip install -r requirements.txt",)

    def test_python_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        assert detect_setup_commands(tmp_path) == ("pip install -e .",)

    def test_requirements_wins_over_pyproject(self, tmp_path):
        """Both present is common; requirements.txt is the pinned one."""
        (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        assert detect_setup_commands(tmp_path) == ("pip install -r requirements.txt",)

    def test_empty_repo_needs_nothing(self, tmp_path):
        assert detect_setup_commands(tmp_path) == ()

    def test_a_docs_repo_needs_nothing(self, tmp_path):
        (tmp_path / "README.md").write_text("# docs", encoding="utf-8")
        assert detect_setup_commands(tmp_path) == ()


class TestResolveSetupCommands:
    def test_no_config_falls_back_to_detection(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert resolve_setup_commands(tmp_path, None) == ("npm install",)

    def test_explicit_setup_overrides_detection(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        cfg = RepoSetupConfig(setup=("npm ci",))
        assert resolve_setup_commands(tmp_path, cfg) == ("npm ci",)

    def test_an_explicitly_empty_list_disables_setup(self, tmp_path):
        """Distinct from 'no config': it means this repo genuinely needs none."""
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        cfg = RepoSetupConfig(setup=())
        assert resolve_setup_commands(tmp_path, cfg) == ()

    def test_config_without_a_setup_key_still_detects(self, tmp_path):
        """A block that only sets env_files must not disable setup."""
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        cfg = RepoSetupConfig(env_files=(".env.local",))
        assert resolve_setup_commands(tmp_path, cfg) == ("npm install",)


class TestCopyEnvFiles:
    def test_copies_the_named_files(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir(); dst.mkdir()
        (src / ".env.local").write_text("KEY=value", encoding="utf-8")
        copied = copy_env_files(src, dst, (".env.local",))
        assert copied == [".env.local"]
        assert (dst / ".env.local").read_text(encoding="utf-8") == "KEY=value"

    def test_missing_files_are_skipped_not_fatal(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir(); dst.mkdir()
        assert copy_env_files(src, dst, (".env.local", ".env.production")) == []

    def test_reports_only_what_it_actually_copied(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir(); dst.mkdir()
        (src / ".env").write_text("A=1", encoding="utf-8")
        assert copy_env_files(src, dst, (".env", ".env.nope")) == [".env"]

    def test_nested_paths_are_supported(self, tmp_path):
        """functions/.env is in the loop's list and has no parent dir yet."""
        src, dst = tmp_path / "src", tmp_path / "dst"
        (src / "functions").mkdir(parents=True); dst.mkdir()
        (src / "functions" / ".env").write_text("K=v", encoding="utf-8")
        assert copy_env_files(src, dst, ("functions/.env",)) == ["functions/.env"]
        assert (dst / "functions" / ".env").read_text(encoding="utf-8") == "K=v"

    def test_a_missing_source_directory_is_not_fatal(self, tmp_path):
        dst = tmp_path / "dst"
        dst.mkdir()
        assert copy_env_files(tmp_path / "nope", dst, (".env",)) == []

    def test_no_source_configured_copies_nothing(self, tmp_path):
        dst = tmp_path / "dst"
        dst.mkdir()
        assert copy_env_files(None, dst, (".env",)) == []

    def test_does_not_escape_the_worktree(self, tmp_path):
        """A traversal in the configured name must not write outside dst."""
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir(); dst.mkdir()
        (src / ".env").write_text("A=1", encoding="utf-8")
        assert copy_env_files(src, dst, ("../escaped.env",)) == []
        assert not (tmp_path / "escaped.env").exists()


# ---------------------------------------------------------------------------
# config.toml wiring
# ---------------------------------------------------------------------------

from config import load_config  # noqa: E402

_MINIMAL = """
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
dev_model = "claude-sonnet-5"
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


def _cfg(tmp_path, extra=""):
    p = tmp_path / "config.toml"
    p.write_text(_MINIMAL + extra, encoding="utf-8")
    return load_config(p)


class TestRepoSetupFromConfig:
    def test_absent_section_gives_no_overrides(self, tmp_path):
        assert _cfg(tmp_path).repo_setup == {}

    def test_setup_commands_are_read(self, tmp_path):
        extra = '\n[repos.field_admin]\nsetup = ["npm ci", "npx prisma generate"]\n'
        cfg = _cfg(tmp_path, extra)
        assert cfg.repo_setup["field_admin"].setup == ("npm ci", "npx prisma generate")

    def test_env_files_are_read(self, tmp_path):
        extra = '\n[repos.field_admin]\nenv_files = [".env.local"]\n'
        assert _cfg(tmp_path, extra).repo_setup["field_admin"].env_files == (".env.local",)

    def test_omitting_setup_leaves_it_none_so_detection_still_runs(self, tmp_path):
        extra = '\n[repos.field_admin]\nenv_files = [".env.local"]\n'
        assert _cfg(tmp_path, extra).repo_setup["field_admin"].setup is None

    def test_an_empty_setup_list_is_preserved_as_empty_not_none(self, tmp_path):
        extra = '\n[repos.field_admin]\nsetup = []\n'
        assert _cfg(tmp_path, extra).repo_setup["field_admin"].setup == ()

    def test_env_source_is_resolved_against_the_project_root(self, tmp_path):
        extra = '\n[repos.field_admin]\nenv_source = "../accelevation/field_admin"\n'
        got = _cfg(tmp_path, extra).repo_setup["field_admin"].env_source
        # Matches the convention the other [paths] keys use — joined against the
        # project root, not normalised.
        assert got.resolve() == (tmp_path / ".." / "accelevation" / "field_admin").resolve()

    def test_an_absolute_env_source_is_left_alone(self, tmp_path):
        abs_path = (tmp_path / "elsewhere").resolve()
        extra = f'\n[repos.field_admin]\nenv_source = "{abs_path.as_posix()}"\n'
        assert _cfg(tmp_path, extra).repo_setup["field_admin"].env_source == abs_path

    def test_a_repo_with_no_block_is_simply_absent(self, tmp_path):
        extra = '\n[repos.field_admin]\nsetup = ["npm ci"]\n'
        assert "QualityFieldApp" not in _cfg(tmp_path, extra).repo_setup


# ---------------------------------------------------------------------------
# Worker wiring — checks must gate the push, not trail it
# ---------------------------------------------------------------------------

import worker  # noqa: E402

SOURCE = (Path(__file__).resolve().parent.parent / "worker.py").read_text(encoding="utf-8")


def _slice(fn_name: str) -> str:
    start = SOURCE.index(f"def {fn_name}(")
    nxt = SOURCE.find("\ndef ", start + 1)
    return SOURCE[start: nxt if nxt != -1 else len(SOURCE)]


class TestDevWorkerValidatesBeforePushing:
    """A broken build must never reach a PR. Before this, verify ran only in
    the review worker — one full dev cycle and one attempt later."""

    def test_dev_worker_runs_the_checks(self):
        assert "_prepare_and_check" in _slice("run_dev_worker")

    def test_checks_run_before_the_push(self):
        body = _slice("run_dev_worker")
        assert body.index("_prepare_and_check") < body.index("_push_and_pr")

    def test_preparation_happens_before_the_checks(self):
        """Both workers go through one choke point, so this pins it once."""
        body = _slice("_prepare_and_check")
        assert body.index("prepare_worktree") < body.index("_run_pipeline_checks")

    def test_failed_preparation_does_not_run_the_checks(self):
        body = _slice("_prepare_and_check")
        assert "if not setup.ok" in body

    def test_review_worker_also_prepares_before_checking(self):
        assert "_prepare_and_check" in _slice("run_review_worker")

    def test_review_worker_no_longer_calls_the_checks_unprepared(self):
        assert "_run_pipeline_checks" not in _slice("run_review_worker")


class TestIssueContextCarriesSetup:
    def test_repo_setup_is_a_context_field(self):
        names = {f.name for f in __import__("dataclasses").fields(worker.IssueContext)}
        assert "repo_setup" in names

    def test_it_defaults_to_none_so_detection_runs(self, tmp_path):
        ctx = worker.IssueContext(
            issue_id="r#1", repo="r", number=1, title="t", body="", action="implement",
            org="o", base_branch="dev", repos_dir=tmp_path, worktrees_dir=tmp_path,
            prompts_dir=tmp_path, dev_model="m", light_model="m",
            permission_mode="bypassPermissions", max_budget_usd=1.0, max_turns=1,
            crash_logs_dir=tmp_path, color_name="b", color_code="x",
        )
        assert ctx.repo_setup is None


class TestRepairPrompt:
    def test_it_carries_the_failure_transcript(self):
        p = worker._build_repair_prompt("$ npm run build\n[FAIL, exit 1]\nTS2304")
        assert "TS2304" in p

    def test_it_tells_the_agent_not_to_start_over(self):
        p = worker._build_repair_prompt("boom").lower()
        assert "already" in p or "existing" in p

    def test_it_repeats_the_push_prohibition(self):
        """The repair round is a fresh CLI invocation — the boundaries from the
        first prompt do not carry over."""
        p = worker._build_repair_prompt("boom").lower()
        assert "push" in p
