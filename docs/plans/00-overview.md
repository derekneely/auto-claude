# auto-claude: System Overview

> [!WARNING]
> **Superseded in part by [11-taxonomy-consolidation.md](11-taxonomy-consolidation.md).**
> This document describes the original MVP design. The `ac-*` label vocabulary,
> the trigger contract, and the plan/review worker described below no longer
> match the code: auto-claude now fires on `ac-dev-ready` **plus** assignment to
> `accelevation-bot`, the plan/review worker was retired, and the canonical stage
> machine lives in `stages.py`. Treat the architecture here as current and the
> label/state details as history.

## What This Is

A Python utility that monitors GitHub issues across the **Accelevation** org, automatically triages issues labeled `ac-[action]`, and spawns parallel AI coding agents to develop solutions and submit PRs.

## Problem

Manual issue-to-PR workflow across multiple repos is slow. We want to tag an issue with `ac-fix`, `claude-feature`, etc. and have an automated system pick it up, evaluate it, do the work, and submit a PR — all without human intervention unless clarification is needed.

## Repos Monitored

| Repo | Language | Description |
|------|----------|-------------|
| `Accelevation/quality-field-agent` | Python | Agentic/AI integrations backend |
| `Accelevation/field_admin` | TypeScript/Next.js | Web admin application |
| `Accelevation/QualityFieldApp` | Kotlin | Android mobile app |
| `Accelevation/quality-field-documentation` | JavaScript | Documentation site |

## Tools Available

| Tool | Role |
|------|------|
| `gh` CLI | GitHub API (authenticated as derekneely) |
| `claude` CLI | Primary coding agent (triage + development) |
| `ollama` (gemma4) | Local inference (future use) |
| `codex` CLI | Secondary coding agent (future use) |
| Gemini | Additional AI (upcoming) |

## High-Level Flow

```
GitHub Issue (ac-fix / ac-implement / ac-test / ac-plan / ac-review label)
        │
        ▼
   [1] POLL ──── auto-claude polls repos every 60s
        │
        ▼
   [2] TRIAGE ── Claude reviews issue, decides: enough info?
        │
    ┌───┴────┐
    ▼        ▼
 PROCEED   NEEDS INFO ── post questions as comment, wait
    │
    ├── action == "plan"?
    │       ▼
    │   [3a] PLAN ── Claude analyzes repo + issue, writes implementation plan
    │       │
    │       ▼
    │   Post plan as comment on issue, add label "claude-plan-posted"
    │   (Stops here — user reviews, then relabels to ac-fix/feature to proceed)
    │
    └── action != "plan"?
            ▼
       [3b] SPAWN WORKER ── new process (parallel)
            │
            ▼
       [4] DEVELOP ── clone repo → worktree → Claude CLI codes
            │
            ▼
       [5] SUBMIT ── push branch → create PR → comment on issue
```

## Key Decisions

- **Polling loop** (not webhooks) — simpler, no server needed
- **Claude CLI** for both triage and development
- **multiprocessing.Process** for parallelism (not threads)
- **Python 3.14+** — uses latest stdlib features
- **Python stdlib only** — zero external dependencies
- **Single writer pattern** — main process owns all state
