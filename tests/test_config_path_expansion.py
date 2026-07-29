"""Tests for environment-variable expansion in config.toml path values.

Every path in config.toml is resolved relative to the *harness directory*, so
`env_source = "../accelevation/field_admin"` silently repoints the moment
auto-claude is moved, and `claude_tools_root` was a hardcoded absolute path that
only existed on one machine. Expanding ${VARS} lets a config name a location
without hardcoding where the harness happens to live.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config  # noqa: E402
from test_config import _MINIMAL_BODY  # noqa: E402


def _write(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "config.toml"
    p.write_text(_MINIMAL_BODY + extra, encoding="utf-8")
    return p


class TestEnvVarExpansion:
    def test_expands_a_variable_in_an_absolute_path(self, tmp_path, monkeypatch):
        root = (tmp_path / "elsewhere").resolve()
        monkeypatch.setenv("ACCELEVATION_ROOT", root.as_posix())
        extra = '\n[integrations]\nclaude_tools_root = "${ACCELEVATION_ROOT}/claude-tools"\n'

        cfg = load_config(_write(tmp_path, extra))

        assert cfg.integrations.claude_tools_root == root / "claude-tools"

    def test_expanded_absolute_path_is_not_joined_to_the_project_root(
        self, tmp_path, monkeypatch
    ):
        # The whole point: the result must not end up under the harness dir.
        root = (tmp_path / "elsewhere").resolve()
        monkeypatch.setenv("ACCELEVATION_ROOT", root.as_posix())
        extra = '\n[integrations]\nclaude_tools_root = "${ACCELEVATION_ROOT}/claude-tools"\n'

        cfg = load_config(_write(tmp_path, extra))

        assert cfg.integrations.claude_tools_root.is_absolute()
        assert tmp_path.name not in str(
            cfg.integrations.claude_tools_root.relative_to(root)
        )

    def test_expands_a_variable_inside_a_repo_env_source(self, tmp_path, monkeypatch):
        root = (tmp_path / "checkouts").resolve()
        monkeypatch.setenv("ACCELEVATION_ROOT", root.as_posix())
        extra = (
            '\n[repos.field_admin]\n'
            'env_files = [".env.development"]\n'
            'env_source = "${ACCELEVATION_ROOT}/field_admin"\n'
        )

        cfg = load_config(_write(tmp_path, extra))

        assert cfg.repo_setup["field_admin"].env_source == root / "field_admin"

    def test_expands_a_tilde_to_the_home_directory(self, tmp_path, monkeypatch):
        extra = '\n[integrations]\nclaude_tools_root = "~/claude-tools"\n'

        cfg = load_config(_write(tmp_path, extra))

        assert cfg.integrations.claude_tools_root == Path.home() / "claude-tools"

    def test_an_undefined_variable_is_a_clear_error_not_a_literal_path(
        self, tmp_path, monkeypatch
    ):
        # os.path.expandvars leaves ${MISSING} untouched, which would silently
        # produce a directory literally named "${MISSING}" and fail much later
        # as a confusing "no such file".
        monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)
        extra = '\n[integrations]\nclaude_tools_root = "${DEFINITELY_NOT_SET}/tools"\n'

        with pytest.raises(ValueError, match="DEFINITELY_NOT_SET"):
            load_config(_write(tmp_path, extra))


class TestExistingBehaviourIsPreserved:
    def test_a_plain_relative_path_still_resolves_against_the_project_root(
        self, tmp_path
    ):
        cfg = load_config(_write(tmp_path))
        assert cfg.paths.repos_dir == tmp_path / "repos"

    def test_a_plain_absolute_path_is_still_kept_as_is(self, tmp_path):
        abs_path = (tmp_path / "somewhere" / "claude-tools").resolve()
        extra = f'\n[integrations]\nclaude_tools_root = "{abs_path.as_posix()}"\n'
        cfg = load_config(_write(tmp_path, extra))
        assert cfg.integrations.claude_tools_root == abs_path
