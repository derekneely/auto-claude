"""Configuration loading and typed dataclasses for auto-claude."""

import tomllib
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from worktree_setup import RepoSetupConfig


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
    action_labels: list[str]
    dev_actions: list[str]
    rework_label: str
    # GitHub account auto-claude acts as. Preflight fails if the configured
    # token authenticates as anyone else. None disables the identity check.
    bot_login: str | None = None


@dataclass(frozen=True)
class ClaudeConfig:
    triage_model: str
    dev_model: str
    light_model: str
    permission_mode: str
    max_budget_usd: float
    output_format: str
    grace_budget_usd: float
    max_turns_dev: int
    action_models: dict[str, str]  # action -> model override


@dataclass(frozen=True)
class WorkersConfig:
    max_parallel: int
    max_continuations: int
    shutdown_grace_seconds: int


@dataclass(frozen=True)
class PathsConfig:
    repos_dir: Path
    worktrees_dir: Path
    state_file: Path
    log_file: Path
    prompts_dir: Path
    crash_logs_dir: Path


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    colorize: bool
    log_to_file: bool


@dataclass(frozen=True)
class IntegrationsConfig:
    """Optional cross-toolchain wiring. Absent [integrations] -> all-None,
    which downstream consumers (e.g. telemetry/board-sync) treat as disabled.
    """

    # Checkout of accelevation-claude-tools, whose Node scripts (log-event.mjs,
    # project-sync.mjs) back shared telemetry and board sync. None disables both.
    claude_tools_root: Path | None = None


@dataclass(frozen=True)
class Config:
    github: GithubConfig
    claude: ClaudeConfig
    workers: WorkersConfig
    paths: PathsConfig
    logging: LoggingConfig
    integrations: IntegrationsConfig
    # repo name -> optional [repos.<name>] worktree-preparation overrides. A
    # repo absent here is prepared by auto-detection, which is the intended
    # default; the block exists for the cases detection gets wrong.
    repo_setup: dict[str, "RepoSetupConfig"] = dataclass_field(default_factory=dict)


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
        crash_logs_dir=_resolve_path(project_root, paths_raw.get("crash_logs_dir", "crash_logs")),
    )

    claude_raw = raw["claude"]
    claude = ClaudeConfig(
        triage_model=claude_raw["triage_model"],
        dev_model=claude_raw["dev_model"],
        light_model=claude_raw.get("light_model", claude_raw["triage_model"]),
        permission_mode=claude_raw["permission_mode"],
        max_budget_usd=claude_raw["max_budget_usd"],
        output_format=claude_raw["output_format"],
        grace_budget_usd=claude_raw.get("grace_budget_usd", 1.0),
        max_turns_dev=claude_raw.get("max_turns_dev", 50),
        action_models=claude_raw.get("action_models", {}),
    )

    integrations_raw = raw.get("integrations", {})
    claude_tools_root = integrations_raw.get("claude_tools_root")
    integrations = IntegrationsConfig(
        claude_tools_root=(
            _resolve_path(project_root, claude_tools_root) if claude_tools_root else None
        ),
    )

    # [repos.<name>] — worktree preparation overrides. `setup` omitted stays
    # None so detection still runs; an explicitly empty list means "this repo
    # genuinely needs nothing" and is preserved as such.
    repo_setup: dict[str, RepoSetupConfig] = {}
    for name, block in (raw.get("repos") or {}).items():
        if not isinstance(block, dict):
            continue
        env_source = block.get("env_source")
        repo_setup[name] = RepoSetupConfig(
            setup=tuple(block["setup"]) if "setup" in block else None,
            env_files=tuple(block.get("env_files", ())),
            env_file_as=dict(block.get("env_file_as", {})),
            env_source=_resolve_path(project_root, env_source) if env_source else None,
        )

    return Config(
        repo_setup=repo_setup,
        github=GithubConfig(**raw["github"]),
        claude=claude,
        workers=WorkersConfig(**raw["workers"]),
        paths=paths,
        logging=LoggingConfig(**raw["logging"]),
        integrations=integrations,
    )


def _resolve_path(project_root: Path, value: str) -> Path:
    """Resolve a path relative to the project root, or return as-is if absolute."""
    p = Path(value)
    if p.is_absolute():
        return p
    return project_root / p
