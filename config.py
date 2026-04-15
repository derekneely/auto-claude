"""Configuration loading and typed dataclasses for auto-claude."""

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GithubConfig:
    org: str
    repos: list[str]
    poll_interval_seconds: int
    base_branch: str
    label_prefix: str
    needs_info_label: str
    pr_created_label: str
    in_progress_label: str
    plan_posted_label: str
    review_posted_label: str
    action_labels: list[str]
    dev_actions: list[str]
    plan_actions: list[str]


@dataclass(frozen=True)
class ClaudeConfig:
    triage_model: str
    dev_model: str
    permission_mode: str
    max_budget_usd: float
    output_format: str


@dataclass(frozen=True)
class WorkersConfig:
    max_parallel: int
    retry_max: int
    retry_delay_seconds: int
    shutdown_grace_seconds: int


@dataclass(frozen=True)
class PathsConfig:
    repos_dir: Path
    worktrees_dir: Path
    state_file: Path
    log_file: Path
    prompts_dir: Path


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    colorize: bool
    log_to_file: bool


@dataclass(frozen=True)
class Config:
    github: GithubConfig
    claude: ClaudeConfig
    workers: WorkersConfig
    paths: PathsConfig
    logging: LoggingConfig


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from a TOML file.

    All relative paths in [paths] are resolved against the project root
    (the directory containing config.toml).
    """
    if config_path is None:
        config_path = Path("config.toml")

    config_path = config_path.resolve()
    project_root = config_path.parent

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    paths_raw = raw["paths"]
    paths = PathsConfig(
        repos_dir=_resolve_path(project_root, paths_raw["repos_dir"]),
        worktrees_dir=_resolve_path(project_root, paths_raw["worktrees_dir"]),
        state_file=_resolve_path(project_root, paths_raw["state_file"]),
        log_file=_resolve_path(project_root, paths_raw["log_file"]),
        prompts_dir=_resolve_path(project_root, paths_raw["prompts_dir"]),
    )

    return Config(
        github=GithubConfig(**raw["github"]),
        claude=ClaudeConfig(**raw["claude"]),
        workers=WorkersConfig(**raw["workers"]),
        paths=paths,
        logging=LoggingConfig(**raw["logging"]),
    )


def _resolve_path(project_root: Path, value: str) -> Path:
    """Resolve a path relative to the project root, or return as-is if absolute."""
    p = Path(value)
    if p.is_absolute():
        return p
    return project_root / p
