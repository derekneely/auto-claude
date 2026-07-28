"""Tests for ghauth.py — bot token loading, subprocess env, and preflight."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghauth  # noqa: E402
from ghauth import (  # noqa: E402
    TOKEN_ENV_VAR,
    TOKEN_FILENAME,
    build_env,
    git_credential_args,
    load_token,
    preflight,
)

FAKE = "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz1234567890"


class TestLoadToken:
    def test_returns_none_when_nothing_configured(self, tmp_path):
        assert load_token(tmp_path, environ={}) is None

    def test_reads_from_the_token_file(self, tmp_path):
        (tmp_path / TOKEN_FILENAME).write_text(FAKE, encoding="utf-8")
        assert load_token(tmp_path, environ={}) == FAKE

    def test_strips_whitespace_and_newlines(self, tmp_path):
        (tmp_path / TOKEN_FILENAME).write_text(f"  {FAKE}\n\n", encoding="utf-8")
        assert load_token(tmp_path, environ={}) == FAKE

    def test_ignores_comments_and_blank_lines(self, tmp_path):
        (tmp_path / TOKEN_FILENAME).write_text(
            f"# accelevation-bot PAT\n\n{FAKE}\n", encoding="utf-8"
        )
        assert load_token(tmp_path, environ={}) == FAKE

    def test_env_var_wins_over_file(self, tmp_path):
        (tmp_path / TOKEN_FILENAME).write_text("from-file", encoding="utf-8")
        assert load_token(tmp_path, environ={TOKEN_ENV_VAR: FAKE}) == FAKE

    def test_tolerates_a_utf8_bom(self, tmp_path):
        # PowerShell's Set-Content/Out-File can prepend a BOM. Python's .strip()
        # does NOT remove ﻿, so without utf-8-sig the token is corrupted in
        # a way that only shows up as a confusing 401.
        (tmp_path / TOKEN_FILENAME).write_text(FAKE, encoding="utf-8-sig")
        assert load_token(tmp_path, environ={}) == FAKE

    def test_tolerates_crlf_line_endings(self, tmp_path):
        (tmp_path / TOKEN_FILENAME).write_bytes(f"{FAKE}\r\n".encode("utf-8"))
        assert load_token(tmp_path, environ={}) == FAKE

    def test_strips_accidental_surrounding_quotes(self, tmp_path):
        # Easy to paste `'ghp_...'` out of a shell command.
        (tmp_path / TOKEN_FILENAME).write_text(f"'{FAKE}'", encoding="utf-8")
        assert load_token(tmp_path, environ={}) == FAKE

    def test_ignores_empty_file(self, tmp_path):
        (tmp_path / TOKEN_FILENAME).write_text("\n  \n", encoding="utf-8")
        assert load_token(tmp_path, environ={}) is None

    def test_does_not_read_ambient_gh_token(self, tmp_path):
        # The whole point of a distinct var: a GH_TOKEN in the user's shell must
        # NOT be silently adopted as the bot identity.
        assert load_token(tmp_path, environ={"GH_TOKEN": FAKE}) is None


class TestBuildEnv:
    def test_always_sets_the_windows_path_guard(self):
        env = build_env(None, base={})
        assert env["MSYS_NO_PATHCONV"] == "1"

    def test_injects_the_token_as_gh_token(self):
        env = build_env(FAKE, base={})
        assert env["GH_TOKEN"] == FAKE

    def test_overrides_an_ambient_gh_token(self):
        env = build_env(FAKE, base={"GH_TOKEN": "someone-elses"})
        assert env["GH_TOKEN"] == FAKE

    def test_leaves_ambient_auth_alone_when_unconfigured(self):
        # No bot token configured → fall back to the user's own gh session
        # rather than breaking every call.
        env = build_env(None, base={"GH_TOKEN": "ambient"})
        assert env["GH_TOKEN"] == "ambient"

    def test_preserves_other_environment_variables(self):
        env = build_env(FAKE, base={"PATH": "/usr/bin", "HOME": "/home/x"})
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/x"

    def test_does_not_mutate_the_base_mapping(self):
        base = {"PATH": "/usr/bin"}
        build_env(FAKE, base=base)
        assert "GH_TOKEN" not in base


class TestGitCredentialArgs:
    def test_empty_when_no_token(self):
        # Unconfigured must not change existing git behaviour.
        assert git_credential_args(None) == []

    def test_clears_inherited_helpers_before_adding_ours(self):
        args = git_credential_args(FAKE)
        # Git Credential Manager is set at SYSTEM scope on Windows and holds the
        # human's credentials. Without the empty reset first, it wins and commits
        # get pushed as the wrong user.
        assert args[:2] == ["-c", "credential.helper="]
        assert args[2] == "-c"
        assert args[3] == "credential.helper=!gh auth git-credential"

    def test_does_not_embed_the_token_in_argv(self):
        # argv is visible in process listings; the token must travel via env only.
        assert FAKE not in " ".join(git_credential_args(FAKE))


class TestGitSubcommand:
    def test_plain_command(self):
        assert ghauth.git_subcommand(["git", "push", "origin", "main"]) == "push"

    def test_skips_dash_c_directory(self):
        assert ghauth.git_subcommand(["git", "-C", "/repo", "fetch"]) == "fetch"

    def test_skips_dash_c_config_pairs(self):
        cmd = ["git", "-c", "credential.helper=", "-C", "/repo", "push"]
        assert ghauth.git_subcommand(cmd) == "push"

    def test_returns_none_for_non_git(self):
        assert ghauth.git_subcommand(["gh", "pr", "create"]) is None
        assert ghauth.git_subcommand([]) is None


class TestApplyGitCredentials:
    def test_injects_for_network_commands(self):
        out = ghauth.apply_git_credentials(["git", "push", "origin", "b"], token=FAKE)
        assert out[0] == "git"
        assert out[1:5] == [
            "-c", "credential.helper=",
            "-c", "credential.helper=!gh auth git-credential",
        ]
        assert out[5:] == ["push", "origin", "b"]

    @pytest.mark.parametrize("sub", ["fetch", "pull", "push", "clone", "ls-remote"])
    def test_covers_every_network_subcommand(self, sub):
        out = ghauth.apply_git_credentials(["git", sub], token=FAKE)
        assert "credential.helper=!gh auth git-credential" in out

    @pytest.mark.parametrize(
        "sub", ["status", "add", "commit", "log", "worktree", "checkout", "branch", "merge"]
    )
    def test_leaves_local_commands_alone(self, sub):
        cmd = ["git", sub, "x"]
        assert ghauth.apply_git_credentials(cmd, token=FAKE) == cmd

    def test_leaves_non_git_commands_alone(self):
        cmd = ["gh", "pr", "create", "--title", "x"]
        assert ghauth.apply_git_credentials(cmd, token=FAKE) == cmd

    def test_noop_without_a_token(self):
        cmd = ["git", "push", "origin", "b"]
        assert ghauth.apply_git_credentials(cmd, token=None) == cmd

    def test_preserves_dash_c_directory_form(self):
        out = ghauth.apply_git_credentials(["git", "-C", "/repo", "push"], token=FAKE)
        assert out[-3:] == ["-C", "/repo", "push"]
        assert "credential.helper=!gh auth git-credential" in out

    def test_does_not_mutate_the_input(self):
        cmd = ["git", "push"]
        ghauth.apply_git_credentials(cmd, token=FAKE)
        assert cmd == ["git", "push"]


class TestCurrentToken:
    def test_reads_the_private_variable(self, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, FAKE)
        assert ghauth.current_token() == FAKE

    def test_ignores_ambient_gh_token(self, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        monkeypatch.setenv("GH_TOKEN", FAKE)
        assert ghauth.current_token() is None

    def test_blank_is_none(self, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "   ")
        assert ghauth.current_token() is None


class FakeRunner:
    """Stands in for subprocess calls during preflight."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        for pattern, response in self.responses.items():
            if pattern in " ".join(args):
                return response
        return (1, "", "unexpected call")


def _ok_responses(login="accelevation-bot"):
    return {
        "api user": (0, login, ""),
        "repos/Accelevation/field_admin": (0, '{"push":true}', ""),
        "project list": (0, "", ""),
    }


class TestDefaultRunnerUsesTheToken:
    """Regression: preflight must run `gh` AS the bot, not as the ambient user.

    The first version called subprocess.run without an env, so gh fell back to
    the operator's stored credentials and reported the wrong login - which reads
    exactly like a bad token and sends you back to GitHub to re-check scopes.
    """

    def test_default_runner_injects_gh_token(self, monkeypatch):
        captured = {}

        class FakeCompleted:
            returncode = 0
            stdout = "accelevation-bot"
            stderr = ""

        def fake_run(args, **kwargs):
            captured["env"] = kwargs.get("env")
            return FakeCompleted()

        monkeypatch.setattr("subprocess.run", fake_run)
        preflight(token=FAKE, org="Accelevation", repos=[], expected_login=None)

        assert captured["env"] is not None, "runner must pass an explicit env"
        assert captured["env"].get("GH_TOKEN") == FAKE

    def test_default_runner_omits_token_when_unconfigured(self, monkeypatch):
        captured = {}

        class FakeCompleted:
            returncode = 0
            stdout = "derekneely"
            stderr = ""

        def fake_run(args, **kwargs):
            captured["env"] = kwargs.get("env")
            return FakeCompleted()

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        preflight(token=None, org="Accelevation", repos=[], expected_login=None)

        assert "GH_TOKEN" not in captured["env"]


class TestPreflight:
    def test_all_green(self):
        checks = preflight(
            token=FAKE, org="Accelevation", repos=["field_admin"],
            expected_login="accelevation-bot", run=FakeRunner(_ok_responses()),
        )
        assert all(c.ok for c in checks), [c.detail for c in checks if not c.ok]

    def test_flags_wrong_identity(self):
        checks = preflight(
            token=FAKE, org="Accelevation", repos=["field_admin"],
            expected_login="accelevation-bot",
            run=FakeRunner(_ok_responses(login="derekneely")),
        )
        bad = [c for c in checks if not c.ok]
        assert bad and "derekneely" in bad[0].detail

    def test_flags_missing_push_permission(self):
        responses = _ok_responses()
        responses["repos/Accelevation/field_admin"] = (0, '{"push":false}', "")
        checks = preflight(
            token=FAKE, org="Accelevation", repos=["field_admin"],
            expected_login="accelevation-bot", run=FakeRunner(responses),
        )
        bad = [c for c in checks if not c.ok]
        assert bad and "push" in bad[0].detail.lower()

    def test_pending_org_approval_is_reported_as_such(self):
        # A 404 here almost always means the token awaits org approval, not that
        # the repo is missing — say so, or this costs an hour to diagnose.
        responses = _ok_responses()
        responses["repos/Accelevation/field_admin"] = (1, "", "HTTP 404: Not Found")
        checks = preflight(
            token=FAKE, org="Accelevation", repos=["field_admin"],
            expected_login="accelevation-bot", run=FakeRunner(responses),
        )
        bad = [c for c in checks if not c.ok]
        assert bad and "approval" in bad[0].detail.lower()

    def test_missing_project_scope_warns_but_is_not_fatal(self):
        responses = _ok_responses()
        responses["project list"] = (1, "", "error: your token has not been granted 'project'")
        checks = preflight(
            token=FAKE, org="Accelevation", repos=["field_admin"],
            expected_login="accelevation-bot", run=FakeRunner(responses),
        )
        failed = [c for c in checks if not c.ok]
        assert failed
        assert all(not c.fatal for c in failed), "board sync is not required to develop"

    def test_no_token_is_fatal(self):
        # Was a warning. Falling back to the operator's gh session means the
        # bot's work is committed, pushed and commented under a human's name -
        # not something to let past with a log line. See test_ownership_gate.py.
        checks = preflight(
            token=None, org="Accelevation", repos=["field_admin"],
            expected_login="accelevation-bot", run=FakeRunner(_ok_responses("derekneely")),
        )
        assert any(c.fatal and not c.ok for c in checks)

    def test_checks_every_configured_repo(self):
        responses = _ok_responses()
        responses["repos/Accelevation/QualityFieldApp"] = (0, '{"push":true}', "")
        runner = FakeRunner(responses)
        preflight(
            token=FAKE, org="Accelevation",
            repos=["field_admin", "QualityFieldApp"],
            expected_login="accelevation-bot", run=runner,
        )
        joined = [" ".join(c) for c in runner.calls]
        assert any("field_admin" in c for c in joined)
        assert any("QualityFieldApp" in c for c in joined)
