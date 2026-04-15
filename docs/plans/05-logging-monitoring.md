# Logging & Monitoring

## Design Goals

- See all worker activity in a single terminal
- Instantly identify which worker is doing what via color + tag
- Log to file for post-mortem analysis (no ANSI codes in file)
- Workers don't write to stdout directly — everything goes through queues

## Color Pool

8 distinct ANSI colors assigned to workers at spawn time, recycled when worker completes:

```
RED      \033[91m
GREEN    \033[92m
YELLOW   \033[93m
BLUE     \033[94m
MAGENTA  \033[95m
CYAN     \033[96m
ORANGE   \033[38;5;208m
PURPLE   \033[38;5;129m
```

Main process logs use white: `\033[97m`

## Output Format

### Main process logs
```
[auto-claude]  Polling Accelevation/quality-field-agent...
[auto-claude]  New issue: quality-field-agent#42 "Fix login bug" [ac-fix]
[auto-claude]  Triaging quality-field-agent#42...
[auto-claude]  Triage PASS - queuing for development
[auto-claude]  Spawned worker PID=12345 for quality-field-agent#42 [RED]
```

### Worker logs (colored)
```
[    RED #42 ]  quality-field-agent          Cloning repository...
[    RED #42 ]  quality-field-agent          Creating worktree: claude/issue-42-fix-login
[    RED #42 ]  quality-field-agent          Running Claude Code for development...
[    RED #42 ]  quality-field-agent          CLAUDE: Analyzing codebase structure...
[    RED #42 ]  quality-field-agent          CLAUDE: Found bug in auth.py line 42
[    RED #42 ]  quality-field-agent          CLAUDE: Applied fix and running tests
[    RED #42 ]  quality-field-agent          Pushing branch claude/issue-42-fix-login
[    RED #42 ]  quality-field-agent          PR created: https://github.com/...
[    RED #42 ]  quality-field-agent          DONE

[   BLUE #7  ]  field_admin                  Cloning repository...
[   BLUE #7  ]  field_admin                  Running Claude Code for development...
```

## Architecture

```
Worker Process                          Main Process
┌─────────────────┐                    ┌─────────────────────┐
│ WorkerLogger    │                    │ MainLogger          │
│                 │  LogMessage        │                     │
│  .info(msg)  ──►├──── Queue ────────►│  .drain_queue()     │
│  .warn(msg)    │                    │    ├── stdout (color)│
│  .error(msg)   │                    │    └── file (plain)  │
└─────────────────┘                    └─────────────────────┘
```

### LogMessage structure
```
issue_id:   "quality-field-agent#42"
color_code: "\033[91m"
color_name: "RED"
level:      "INFO"
message:    "Cloning repository..."
timestamp:  "2026-04-13T15:30:00Z"
repo:       "quality-field-agent"
```

## Why Queues Instead of Direct Logging

- Python's `logging` module is not fork-safe
- Multiple processes writing to the same file causes interleaving/corruption
- Queues let the main process serialize all output cleanly
- Main process can add consistent formatting and write to both stdout and file

## Cross-Platform: ANSI Color Support

Windows terminals need ANSI escape sequences explicitly enabled. At startup, `logger.py` calls `enable_ansi_windows()` which uses `ctypes` to set `ENABLE_VIRTUAL_TERMINAL_PROCESSING` on the console handle. This works on Windows 10+ (Windows Terminal, VS Code terminal, modern cmd.exe). Falls back gracefully if it fails.

Config option `colorize = false` is available as a manual override.

See `10-cross-platform.md` for full details.

## Log File

`logs/auto-claude.log` — plain text, no ANSI codes, same format minus colors. Useful for searching/grepping after the fact.
