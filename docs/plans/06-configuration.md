# Configuration

## config.toml

```toml
[github]
org = "Accelevation"
repos = [
    "quality-field-agent",
    "field_admin",
    "QualityFieldApp",
    "quality-field-documentation",
]
poll_interval_seconds = 60
label_prefix = "ac-"
needs_info_label = "ac-needs-info"
pr_created_label = "ac-pr-created"
in_progress_label = "ac-in-progress"
plan_posted_label = "ac-plan-posted"
review_posted_label = "ac-review-posted"
action_labels = ["ac-fix", "ac-implement", "ac-test", "ac-plan", "ac-review"]
dev_actions = ["fix", "implement", "test"]
plan_actions = ["plan", "review"]

[claude]
triage_model = "claude-haiku-4-5"
dev_model = "claude-sonnet-4-6"
permission_mode = "bypassPermissions"
max_budget_usd = 5.0
output_format = "stream-json"

[workers]
max_parallel = 3
retry_max = 2
retry_delay_seconds = 30
shutdown_grace_seconds = 30

[paths]
repos_dir = "repos"
worktrees_dir = "worktrees"
state_file = "state/issues.json"
log_file = "logs/auto-claude.log"
prompts_dir = "prompts"

[logging]
level = "INFO"       # DEBUG | INFO | WARN | ERROR
colorize = true
log_to_file = true
```

## Config Loading

- Uses Python 3.11+ `tomllib` (stdlib — no pip install needed)
- Parsed into frozen `@dataclass` hierarchy: `Config` → `GithubConfig`, `ClaudeConfig`, `WorkersConfig`, `PathsConfig`, `LoggingConfig`
- All path values are relative to project root by default; can be overridden with absolute paths
- Config loaded once at startup
- CLI flag `--config path/to/config.toml` to override default location

## Key Settings Explained

| Setting | Default | Purpose |
|---------|---------|---------|
| `poll_interval_seconds` | 60 | How often to check GitHub for new issues |
| `label_prefix` | "ac-" | Only issues with labels starting with this are picked up |
| `triage_model` | haiku-4-5 | Fast/cheap model for yes/no triage decisions |
| `dev_model` | sonnet-4-6 | Best model for actual code development |
| `max_budget_usd` | 5.0 | Per-issue spending cap for Claude CLI |
| `max_parallel` | 3 | Max simultaneous worker processes |
| `retry_max` | 2 | How many times to retry a failed issue |
| `shutdown_grace_seconds` | 30 | How long to wait for workers before force-killing on shutdown |
