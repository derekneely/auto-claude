"""Tests for pipeline.py — the per-repo `.claude/pipeline.json` contract.

`.claude/pipeline.json` is owned by the sibling `accelevation-claude-tools`
toolchain; auto-claude only reads it. Today only one of the four repos
(`field_admin`) has the file, so absence must be non-fatal - callers fall back
to the global `[github]` config.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import (  # noqa: E402
    PipelineConfig,
    PipelineConfigError,
    ProjectBoardConfig,
    load_pipeline_config,
)

# Real example from the plan doc / field_admin's live pipeline.json.
FULL_VALID = {
    "project": "field_admin",
    "defaultBranch": "main",
    "prBaseBranch": "derekdev",
    "verify": ["npm run typecheck", "npm run build"],
    "test": [],
    "concurrency": 1,
    "staleLockHours": 2,
    "worktreeBase": ".claude/worktrees",
    "projectBoard": {
        "owner": "Accelevation",
        "number": 1,
        "field": "Status",
        "projectId": "PVT_kwDOEFZBMs4Ba2ai",
        "statusFieldId": "PVTSSF_lADOEFZBMs4Ba2aizhVrCSA",
        "columns": {
            "Backlog": "f75ad846",
            "Reviewed": "76ac9da9",
            "Needs Input": "4c63bc0e",
            "Ready": "61e4505c",
            "In Progress": "47fc9ee4",
            "In Review": "df73e18b",
            "Dev Review": "cb952807",
            "Pending Release": "cd7efea8",
            "Blocked": "76f4a0cb",
            "Done": "98236657",
        },
    },
}


def _write(tmp_path: Path, payload) -> Path:
    """Write a pipeline.json (or raw text) under tmp_path/.claude/pipeline.json."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    p = claude_dir / "pipeline.json"
    if isinstance(payload, str):
        p.write_text(payload, encoding="utf-8")
    else:
        p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class TestFullValidFile:
    def test_parses_every_field(self, tmp_path):
        _write(tmp_path, FULL_VALID)
        cfg = load_pipeline_config(tmp_path)

        assert cfg == PipelineConfig(
            project="field_admin",
            default_branch="main",
            pr_base_branch="derekdev",
            verify=("npm run typecheck", "npm run build"),
            test=(),
            concurrency=1,
            stale_lock_hours=2,
            worktree_base=".claude/worktrees",
            project_board=ProjectBoardConfig(
                owner="Accelevation",
                number=1,
                field="Status",
                project_id="PVT_kwDOEFZBMs4Ba2ai",
                status_field_id="PVTSSF_lADOEFZBMs4Ba2aizhVrCSA",
                columns=FULL_VALID["projectBoard"]["columns"],
            ),
        )
        assert cfg.project_board.is_valid


class TestOptionalKeyDefaults:
    def test_only_project_given_uses_scaffold_defaults(self, tmp_path):
        _write(tmp_path, {"project": "quality-field-agent"})
        cfg = load_pipeline_config(tmp_path)

        assert cfg.project == "quality-field-agent"
        assert cfg.default_branch == "main"
        assert cfg.pr_base_branch == "main"
        assert cfg.verify == ()
        assert cfg.test == ()
        assert cfg.concurrency == 1
        assert cfg.stale_lock_hours == 2
        assert cfg.worktree_base == ".claude/worktrees"
        assert cfg.project_board is None

    def test_partial_overrides_mix_with_defaults(self, tmp_path):
        _write(tmp_path, {"project": "hmi", "prBaseBranch": "dev", "concurrency": 3})
        cfg = load_pipeline_config(tmp_path)

        assert cfg.pr_base_branch == "dev"
        assert cfg.concurrency == 3
        assert cfg.default_branch == "main"  # untouched default


class TestMissingFile:
    def test_returns_none_without_raising(self, tmp_path):
        assert load_pipeline_config(tmp_path) is None

    def test_warns_naming_the_repo(self, tmp_path):
        warnings = []
        logger = SimpleNamespace(warn=lambda msg: warnings.append(msg))

        result = load_pipeline_config(tmp_path, logger=logger)

        assert result is None
        assert len(warnings) == 1
        assert tmp_path.name in warnings[0]
        assert "pipeline.json" in warnings[0]

    def test_no_logger_does_not_raise(self, tmp_path):
        # logger is optional - callers that don't care must not be forced to pass one.
        assert load_pipeline_config(tmp_path, logger=None) is None


class TestMalformedJson:
    def test_raises_clear_error_naming_the_file(self, tmp_path):
        p = _write(tmp_path, "{not valid json")

        with pytest.raises(PipelineConfigError) as exc_info:
            load_pipeline_config(tmp_path)

        assert str(p) in str(exc_info.value)

    def test_non_object_json_raises(self, tmp_path):
        _write(tmp_path, ["not", "an", "object"])

        with pytest.raises(PipelineConfigError):
            load_pipeline_config(tmp_path)

    def test_missing_required_project_key_raises(self, tmp_path):
        _write(tmp_path, {"defaultBranch": "main"})

        with pytest.raises(PipelineConfigError):
            load_pipeline_config(tmp_path)

    def test_wrong_type_for_project_raises(self, tmp_path):
        _write(tmp_path, {"project": 123})

        with pytest.raises(PipelineConfigError):
            load_pipeline_config(tmp_path)


class TestProjectBoardAbsent:
    def test_board_disabled_when_key_absent(self, tmp_path):
        _write(tmp_path, {"project": "quality-field-agent"})
        cfg = load_pipeline_config(tmp_path)
        assert cfg.project_board is None


class TestProjectBoardInvalid:
    def test_missing_project_id_flagged_invalid_not_raised(self, tmp_path):
        payload = dict(FULL_VALID)
        payload["projectBoard"] = {
            "owner": "Accelevation",
            "number": 1,
            "field": "Status",
            # projectId, statusFieldId, columns all missing
        }
        _write(tmp_path, payload)

        cfg = load_pipeline_config(tmp_path)  # must not raise

        assert cfg.project_board is not None
        assert cfg.project_board.is_valid is False

    def test_missing_status_field_id_flagged_invalid(self, tmp_path):
        payload = dict(FULL_VALID)
        payload["projectBoard"] = {
            "owner": "Accelevation",
            "number": 1,
            "projectId": "PVT_abc",
            "columns": {"Backlog": "x"},
            # statusFieldId missing
        }
        _write(tmp_path, payload)

        cfg = load_pipeline_config(tmp_path)

        assert cfg.project_board.is_valid is False

    def test_board_missing_owner_or_number_raises(self, tmp_path):
        payload = dict(FULL_VALID)
        payload["projectBoard"] = {"field": "Status"}
        _write(tmp_path, payload)

        with pytest.raises(PipelineConfigError):
            load_pipeline_config(tmp_path)


class TestUnknownKeysTolerated:
    def test_unknown_top_level_keys_do_not_raise(self, tmp_path):
        _write(tmp_path, {
            "project": "field_admin",
            "someNewKeyFromTheLoop": {"whatever": True},
            "anotherFutureKey": [1, 2, 3],
        })

        cfg = load_pipeline_config(tmp_path)

        assert cfg.project == "field_admin"

    def test_unknown_project_board_keys_tolerated(self, tmp_path):
        payload = dict(FULL_VALID)
        payload["projectBoard"] = dict(FULL_VALID["projectBoard"])
        payload["projectBoard"]["someFutureBoardKey"] = "x"
        _write(tmp_path, payload)

        cfg = load_pipeline_config(tmp_path)

        assert cfg.project_board.is_valid
