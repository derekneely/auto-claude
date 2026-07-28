"""Per-repo `.claude/pipeline.json` contract, shared with the sibling
`accelevation-claude-tools` loop.

That toolchain defines and owns the schema (its own doctor fails without the
required keys); auto-claude only reads it. Today only `field_admin` has the
file — the other three repos still rely on auto-claude's global `[github]`
config — so a missing file is expected, common, and must not be fatal. This
module models that as `load_pipeline_config` returning `None` rather than
raising; a *malformed* file, by contrast, is an authoring mistake in a file
someone else owns and is surfaced loudly via `PipelineConfigError`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field as _dataclass_field
from pathlib import Path
from typing import Any

# Relative to a repo checkout root.
PIPELINE_JSON_RELATIVE_PATH = ".claude/pipeline.json"

# Scaffold defaults per the sibling toolchain's schema (see task spec / plan
# doc §3). Keep in sync with what its own scaffolder writes.
_DEFAULT_DEFAULT_BRANCH = "main"
_DEFAULT_PR_BASE_BRANCH = "main"
_DEFAULT_CONCURRENCY = 1
_DEFAULT_STALE_LOCK_HOURS = 2
_DEFAULT_WORKTREE_BASE = ".claude/worktrees"
_DEFAULT_BOARD_FIELD = "Status"


class PipelineConfigError(ValueError):
    """A present `.claude/pipeline.json` is malformed or fails validation.

    Distinct from "file absent" (that's `None`, not an exception) — this is
    for cases where the file exists but cannot be trusted, e.g. broken JSON,
    a wrong type, or a missing required key.
    """


@dataclass(frozen=True)
class ProjectBoardConfig:
    """Optional Projects v2 board sync settings from `projectBoard`."""

    owner: str
    number: int
    field: str = _DEFAULT_BOARD_FIELD
    project_id: str | None = None
    status_field_id: str | None = None
    columns: dict[str, str] = _dataclass_field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        """False when the block is present but missing what board sync needs.

        `owner`/`number` alone can't drive `project-sync.mjs` — it also needs
        `projectId`, `statusFieldId`, and at least one column mapping.
        Callers should treat an invalid block as "board sync disabled", not
        crash on it, since the block can be added incrementally.
        """
        return bool(self.project_id and self.status_field_id and self.columns)


@dataclass(frozen=True)
class PipelineConfig:
    """Parsed, validated contents of a repo's `.claude/pipeline.json`."""

    project: str
    default_branch: str = _DEFAULT_DEFAULT_BRANCH
    pr_base_branch: str = _DEFAULT_PR_BASE_BRANCH
    verify: tuple[str, ...] = ()
    test: tuple[str, ...] = ()
    concurrency: int = _DEFAULT_CONCURRENCY
    stale_lock_hours: float = _DEFAULT_STALE_LOCK_HOURS
    worktree_base: str = _DEFAULT_WORKTREE_BASE
    project_board: ProjectBoardConfig | None = None


def load_pipeline_config(repo_root: Path, logger: Any = None) -> PipelineConfig | None:
    """Load and validate `.claude/pipeline.json` from a repo checkout.

    Returns `None` when the file is absent — the expected case for most repos
    today — logging a warning (naming the repo) if a `logger` with a `.warn`
    method is given, so callers can fall back to global `[github]` config
    without silently losing that fact. Raises `PipelineConfigError` for a
    present-but-broken file: unlike absence, that is not a state a caller
    should quietly paper over.
    """
    repo_root = Path(repo_root)
    path = repo_root / PIPELINE_JSON_RELATIVE_PATH

    if not path.is_file():
        if logger is not None:
            logger.warn(
                f"{repo_root.name}: no {PIPELINE_JSON_RELATIVE_PATH} found — "
                f"falling back to global [github] config"
            )
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise PipelineConfigError(f"{path}: could not read file - {e}") from e

    return parse_pipeline_config(text, source=path)


def parse_pipeline_config(text: str, source: Any = "<pipeline.json>") -> PipelineConfig:
    """Parse `pipeline.json` content that has already been read.

    Split out from `load_pipeline_config` so the file can come from somewhere
    other than a local checkout - notably straight from the GitHub API, which
    is the only way to get the *current* contents. A local clone can be many
    commits stale, and a stale clone reports "no pipeline.json" for a repo that
    has had one for weeks.

    `source` is used only in error messages.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise PipelineConfigError(f"{source}: invalid JSON - {e}") from e

    if not isinstance(raw, dict):
        raise PipelineConfigError(
            f"{source}: expected a JSON object at the top level, got {type(raw).__name__}"
        )

    if "project" not in raw:
        raise PipelineConfigError(f"{source}: missing required key 'project'")
    project = raw["project"]
    if not isinstance(project, str):
        raise PipelineConfigError(
            f"{source}: 'project' must be a string, got {type(project).__name__}"
        )

    board_raw = raw.get("projectBoard")
    board = _parse_board(board_raw, source) if board_raw is not None else None

    return PipelineConfig(
        project=project,
        default_branch=raw.get("defaultBranch", _DEFAULT_DEFAULT_BRANCH),
        pr_base_branch=raw.get("prBaseBranch", _DEFAULT_PR_BASE_BRANCH),
        verify=tuple(raw.get("verify", [])),
        test=tuple(raw.get("test", [])),
        concurrency=raw.get("concurrency", _DEFAULT_CONCURRENCY),
        stale_lock_hours=raw.get("staleLockHours", _DEFAULT_STALE_LOCK_HOURS),
        worktree_base=raw.get("worktreeBase", _DEFAULT_WORKTREE_BASE),
        project_board=board,
    )


def _parse_board(raw: Any, path: Path) -> ProjectBoardConfig:
    """Parse the `projectBoard` block.

    Only `owner`/`number` are required to construct the object at all — they
    identify *which* board. Everything else (`projectId`, `statusFieldId`,
    `columns`) is optional here and instead surfaces via `is_valid`, per the
    spec: "present but missing projectId -> flagged invalid", not raised.
    """
    if not isinstance(raw, dict):
        raise PipelineConfigError(f"{path}: 'projectBoard' must be an object")
    if "owner" not in raw or "number" not in raw:
        raise PipelineConfigError(
            f"{path}: 'projectBoard' is missing required 'owner' and/or 'number'"
        )
    return ProjectBoardConfig(
        owner=raw["owner"],
        number=raw["number"],
        field=raw.get("field", _DEFAULT_BOARD_FIELD),
        project_id=raw.get("projectId"),
        status_field_id=raw.get("statusFieldId"),
        columns=dict(raw.get("columns", {})),
    )
