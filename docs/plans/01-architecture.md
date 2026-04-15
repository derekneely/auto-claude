# Architecture

## System Diagram

```
┌─────────────────────────────────────────────────────────────┐
│  main.py - Orchestrator (main process, polling loop)        │
│                                                             │
│  Poller ──▶ Triage (Claude) ──▶ Process Manager             │
│    │                                    │                   │
│  StateStore (JSON)              Spawns Worker Processes      │
│  Log Queue Drainer              (multiprocessing.Process)    │
└─────────────────────────────────┬───────────────────────────┘
                                  │
          ┌───────────────────────┼───────────────────────┐
          ▼                       ▼                       ▼
   [RED  #42]              [BLUE #7]              [GREEN #15]
   Worker Process          Worker Process          Worker Process
   - clone/fetch repo     - clone/fetch repo     - clone/fetch repo
   - git worktree add     - git worktree add     - git worktree add
   - claude CLI (dev)     - claude CLI (dev)     - claude CLI (dev)
   - git push + gh pr     - git push + gh pr     - git push + gh pr
   - comment on issue     - comment on issue     - comment on issue
```

## Module Map

```
auto-claude/
├── main.py                 # Entry point, orchestrator loop, signal handling
├── config.py               # TOML config loading, typed Config dataclass
├── state.py                # IssueRecord, IssueStatus enum, StateStore
├── github_client.py        # Thin wrappers around `gh` CLI
├── poller.py               # Discovers new/updated labeled issues
├── triage.py               # Claude triage decision (proceed vs needs_info)
├── worker.py               # Worker process: full issue dev lifecycle
├── process_manager.py      # Spawns/tracks/reaps worker processes
├── logger.py               # Color pool, tagged logging, log queue system
├── prompts/
│   ├── triage.txt          # System prompt for triage
│   └── develop.txt         # System prompt for dev work
├── config.toml             # User configuration
├── state/issues.json       # Persisted issue state (auto-created)
├── logs/auto-claude.log    # Log file (auto-created)
├── repos/                  # Local repo clones (auto-created)
└── worktrees/              # Git worktrees per issue (auto-created)
```

## Module Responsibilities

| Module | What It Does |
|--------|-------------|
| `main.py` | Loads config, runs the poll loop, handles SIGINT/SIGTERM shutdown, wires all components together |
| `config.py` | Loads `config.toml` via `tomllib` (stdlib). Exposes typed `Config` dataclass with nested sections |
| `state.py` | `IssueRecord` dataclass, `IssueStatus` enum, `StateStore` class. Atomic JSON read/write. Enforces valid state transitions |
| `github_client.py` | All `gh` CLI calls wrapped as Python functions. Returns parsed JSON dicts. Raises `GithubClientError` |
| `poller.py` | Queries all repos for open issues with `claude-*` labels. Diffs against StateStore. Detects user responses to `needs_info` |
| `triage.py` | Builds triage prompt, calls `claude --print --output-format json`. Parses structured decision. Formats clarifying-question comments |
| `worker.py` | Runs in a `multiprocessing.Process`. Full lifecycle: clone → worktree → Claude CLI dev → commit → push → PR → comment → cleanup |
| `process_manager.py` | Tracks `dict[issue_id, Process]`. Enforces `max_parallel`. Spawns, reaps dead workers, handles retries, graceful shutdown |
| `logger.py` | ANSI color pool (8 colors). `WorkerLogger` sends `LogMessage` via `multiprocessing.Queue`. `MainLogger` drains queue and writes to stdout + file |

## Communication Between Processes

```
                    ┌──────────────┐
                    │ Main Process │
                    │              │
                    │  drains ◄────┼──── log_queue (LogMessage)
                    │  drains ◄────┼──── state_queue (StateUpdate)
                    │  sets   ────►┼──── abort_event (per worker)
                    └──────────────┘
                           ▲
                           │
              ┌────────────┼────────────┐
              │            │            │
        ┌─────────┐  ┌─────────┐  ┌─────────┐
        │ Worker 1│  │ Worker 2│  │ Worker 3│
        └─────────┘  └─────────┘  └─────────┘
```

- **log_queue**: Workers push `LogMessage` objects → main process drains and prints with color tags
- **state_queue**: Workers push `StateUpdate` objects → main process applies to StateStore (single writer)
- **abort_event**: Main process sets per-worker `multiprocessing.Event` → worker checks it between steps and terminates gracefully

No file locking needed because only the main process writes to `state/issues.json`.

**Cross-platform note**: We force `multiprocessing.set_start_method("spawn")` on all platforms for consistent behavior. Everything passed to workers must be picklable (dataclasses, Queues, Events — no open file handles or loggers). See `10-cross-platform.md`.

## Filesystem Layout at Runtime

```
auto-claude/
├── repos/
│   ├── quality-field-agent/        # full clone
│   ├── field_admin/                # full clone
│   ├── QualityFieldApp/            # full clone
│   └── quality-field-documentation/
├── worktrees/
│   ├── field_admin/
│   │   └── issue-42/              # git worktree for issue #42
│   └── quality-field-agent/
│       └── issue-7/               # git worktree for issue #7
└── state/
    └── issues.json                # all tracked issue state
```
