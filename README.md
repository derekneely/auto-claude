# auto-claude

Monitors GitHub issues across the [Accelevation](https://github.com/Accelevation) org, triages them with Claude, and spawns parallel worker processes to develop solutions and submit PRs.

## Prerequisites

- **Python 3.11+** (uses `tomllib` from stdlib — no pip packages needed)
- **Git** on PATH
- **GitHub CLI (`gh`)** on PATH, authenticated (`gh auth status` should show logged in)
- **Claude CLI (`claude`)** on PATH, authenticated

## Setup

1. Clone the repo:

   ```bash
   git clone https://github.com/Accelevation/auto-claude.git
   cd auto-claude
   ```

2. Verify prerequisites:

   ```bash
   python --version    # 3.11+
   gh auth status      # must be logged in
   claude --version    # must be installed
   ```

3. Copy the sample config and edit it for your environment:

   ```bash
   cp config.toml.sample config.toml
   ```

   At minimum, set `org` and `repos` under `[github]` to match your GitHub organization. `config.toml` is gitignored — your real settings stay local.

That's it — zero dependencies to install.

## Usage

### Default: continuous poll loop

```bash
python main.py
```

Polls all configured repos every 60 seconds, triages new issues labeled with `ac-*`, spawns Claude workers, and runs until you press Ctrl+C.

### Custom config path

```bash
python main.py --config path/to/config.toml
```

### Dry-run mode

```bash
python main.py --dry-run
```

Runs the full poll + triage pipeline but does **not** spawn workers, create PRs, or modify GitHub. Useful for verifying that polling and triage work before going live.

### Single-issue mode

```bash
python main.py --issue field_admin#42
python main.py --issue quality-field-agent#7 --dry-run
```

Processes exactly one issue and exits. Combines with `--dry-run` to triage without spawning a worker.

## How it works

1. **Poll** — queries GitHub for open issues with `ac-*` labels across all configured repos
2. **Triage** — sends issue context to Claude (Haiku) for a fast proceed/needs-info decision
3. **Spawn** — launches a worker process per issue (up to `max_parallel`)
4. **Worker** — clones the repo, creates a git worktree, runs Claude (Sonnet) to develop a fix, commits, pushes, and opens a PR
5. **Report** — comments on the original issue with a link to the PR

## Label conventions

An issue is picked up when **both** are true: it carries an `ac-` action label,
**and** it is assigned to the account in `[github].bot_login`. Labelling alone is
not enough — assignment is what marks the issue as auto-claude's rather than a
human `/loop` runner's. auto-claude never assigns issues to itself.

| Label | Action |
|-------|--------|
| `ac-fix` | Bug fix — worker writes code + opens PR |
| `ac-implement` | New feature — worker writes code + opens PR |
| `ac-test` | Write/improve tests — worker writes code + opens PR |
| `ac-rework` | Reviewer requested changes — worker updates the existing PR |
| `ac-needs-info` | Triage decided more info is needed — waiting on human |
| `ac-in-progress` | Worker is actively processing this issue |
| `ac-pr-created` | Worker finished and a PR is open |

To hand an issue to auto-claude:

```bash
gh issue edit <n> --repo <org>/<repo> \
  --add-assignee <bot_login> --add-label ac-implement
```

> `ac-plan` and `ac-review` were retired — the read-only plan/review worker is
> gone, subsumed by the `/loop` triage agent. This vocabulary is being replaced
> wholesale by the shared stage machine; see `docs/plans/11-taxonomy-consolidation.md`.

## Configuration

All settings live in `config.toml`. Key sections:

| Section | What it controls |
|---------|-----------------|
| `[github]` | Org, repos, poll interval, label names |
| `[claude]` | Models for triage vs. dev, budget cap, permission mode |
| `[workers]` | Max parallel workers, retry limits, shutdown grace period |
| `[paths]` | Where repos, worktrees, state, and logs are stored |
| `[logging]` | Log level, color toggle, file logging toggle |

Paths are relative to the directory containing `config.toml` unless specified as absolute.

## Runtime directories

Created automatically on first run:

```
repos/       # local clones of monitored repos
worktrees/   # git worktrees, one per active issue
state/       # issues.json tracking all issue state
logs/        # auto-claude.log (plain text, no ANSI)
```

These directories are gitignored.

## Project status

This project is under active development. Current implementation status:

- [x] Task 1 — Scaffold, config, logger, main entry point
- [ ] Task 2 — State management (issue tracking, state machine)
- [ ] Task 3 — GitHub client (gh CLI wrappers)
- [ ] Task 4 — Poller (issue discovery)
- [ ] Task 5 — Triage engine (Claude-based decision)
- [ ] Task 6 — Worker processes (dev + plan)
- [ ] Task 7 — Process manager + orchestrator wiring
