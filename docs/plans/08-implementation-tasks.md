# Implementation Tasks

## Task Dependency Graph

```
Task 1 (Scaffold + Config + Logger) ──┐
                                       ├──► Task 4 (Poller)
Task 2 (State Management) ────────────┤
                                       ├──► Task 6 (Worker)
Task 3 (GitHub Client) ───────────────┤
                                       ├──► Task 5 (Triage)
                                       │
                                       └──► Task 7 (Orchestrator) ← depends on ALL

Tasks 1, 2, 3 can be done in parallel (no dependencies on each other)
Tasks 4, 5, 6 depend on 1-3
Task 7 depends on everything
```

---

## Task 1: Project Scaffold + Config + Logger

**Files to create**: `config.py`, `logger.py`, `config.toml`, `main.py` (stub), `.gitignore`

**Scope**:
- `config.py`: `tomllib` loader, all `@dataclass` config classes (`Config`, `GithubConfig`, `ClaudeConfig`, `WorkersConfig`, `PathsConfig`, `LoggingConfig`), `load_config()` function, path resolution helpers (all `pathlib.Path`)
- `logger.py`: `COLOR_POOL` constant, `ColorAssigner` class, `LogMessage` dataclass, `MainLogger` (console + file handlers, queue drainer), `WorkerLogger` (sends via queue), `enable_ansi_windows()` for Windows ANSI support
- `config.toml`: Default configuration with all sections
- `main.py`: Arg parser (`--config`, `--dry-run`, `--issue`), load config, init logger, print banner, directory creation, placeholder loop, `multiprocessing.set_start_method("spawn")`, platform-aware signal handling, `if __name__ == "__main__"` guard
- `.gitignore`: state/, logs/, repos/, worktrees/, __pycache__/, *.pyc

**Verification**: `python main.py` starts up, prints banner, creates directories, exits cleanly on Ctrl+C

---

## Task 2: State Management

**Files to create**: `state.py`, `tests/test_state.py`

**Scope**:
- `IssueStatus` enum with all states (including `planning`, `plan_posted`)
- `IssueRecord` dataclass with all fields
- `VALID_TRANSITIONS` dict defining the state machine
- `InvalidTransitionError` exception
- `StateStore` class: `_load()`, `save()` (atomic), `add()`, `get()`, `is_known()`, `update()`, `transition()` (enforces valid transitions), `get_by_status()`, `all_records()`
- Unit tests: test all state transitions (valid and invalid), test atomic save/load round-trip, test corruption recovery

**Verification**: `python -m pytest tests/test_state.py` passes

---

## Task 3: GitHub Client

**Files to create**: `github_client.py`, `tests/test_github_client.py`

**Scope**:
- `GithubClientError` exception (stores returncode, stderr)
- `GithubClient` class with `org` attribute
- `_run_gh()` helper: runs `gh` with `subprocess.run`, timeout, error handling
- `_gh_api()` helper: runs `gh api`, parses JSON response
- Methods: `list_issues()`, `get_issue()`, `get_issue_comments()`, `post_comment()`, `add_label()`, `remove_label()`, `ensure_label_exists()`, `create_pr()`, `clone_repo()`
- Unit tests: test error handling, test PR-not-issue filtering

**Verification**: `python -c "from github_client import GithubClient; gc = GithubClient('Accelevation'); print(gc.list_issues('field_admin'))"` returns real data

---

## Task 4: Poller

**Files to create**: `poller.py`

**Scope**:
- `Poller` class: takes `Config`, `GithubClient`, `StateStore`
- `poll()` → returns `(new_issues, retriage_issues)`
- `_poll_repo()`: queries one repo, filters by label prefix, excludes status labels, extracts action from label
- New issue detection: not in StateStore → create `IssueRecord`, add to state
- Re-triage detection: in StateStore as `needs_info`, GitHub `updated_at` changed
- Plan-to-action detection: in StateStore as `plan_posted`, user added a new action label (e.g., `ac-fix`) → re-enter as new action

**Depends on**: Tasks 1, 2, 3

**Verification**: Create a test issue with `ac-test` label, run poller, verify it's discovered

---

## Task 5: Triage Engine

**Files to create**: `triage.py`, `prompts/triage.txt`

**Scope**:
- `TriageDecision` dataclass: `decision`, `confidence`, `summary`, `questions`
- `TRIAGE_SYSTEM_PROMPT` constant (or load from `prompts/triage.txt`)
- `TriageEngine` class: takes `Config`, `GithubClient`
- `triage(issue_record)` → `TriageDecision`: fetch comments, build prompt, call Claude, parse response
- `_invoke_claude_triage()`: subprocess call with JSON output parsing
- `_parse_response()`: handle Claude's JSON wrapper, strip markdown fences, parse inner JSON
- `format_clarifying_comment()`: format questions into a GitHub comment
- Error handling: retry once, then conservative `needs_info` fallback

**Depends on**: Tasks 1, 3

**Verification**: Run triage against a real issue, verify structured JSON response

---

## Task 6: Worker Processes (Dev + Plan)

**Files to create**: `worker.py`, `prompts/develop.txt`, `prompts/test.txt`, `prompts/plan.txt`, `prompts/review.txt`

**Scope**:
- `IssueContext` dataclass: everything a worker needs (issue info, paths, config, color)
- `StateUpdate` dataclass: status updates sent back to main process
- `sanitize_branch_name()`: create safe git branch from title
- **Development worker** — `run_dev_worker()`: handles actions `fix`, `implement`, `test`. Full lifecycle:
  1. Clone/fetch repo
  2. Create git worktree
  3. Build dev prompt (different template for `test` action — focused on writing + running tests)
  4. Run Claude CLI with Popen, stream output to log queue
  5. Check for changes, stage, commit
  6. Push branch, create PR, comment on issue
  7. Clean up worktree
- **Plan worker** — `run_plan_worker()`: handles actions `plan`, `review`. Read-only analysis:
  1. Clone/fetch repo (same as dev)
  2. NO worktree — runs in repo clone directory (read-only)
  3. Build prompt (different template per action: plan vs review)
  4. Run Claude CLI (no bypassPermissions — read-only mode)
  5. Capture output
  6. Post as comment on the GitHub issue
  7. Swap labels: remove action label, add posted label (`ac-plan-posted` or `ac-review-posted`)
  8. State: planning → plan_posted
- Shared helpers: `_push_and_pr()`, `_build_prompt()`, `_run_claude()`, `_run_cmd()`
- The process manager routes to the correct worker function based on `action in dev_actions` vs `action in plan_actions`
- Crash recovery for dev: detect existing worktree with commits, skip to push

**Depends on**: Tasks 1, 2, 3

**Verification**:
- Dev: Manually trigger worker with a test IssueContext, verify worktree creation and Claude invocation
- Plan: Trigger plan worker, verify plan comment posted on issue without any code changes

---

## Task 7: Process Manager + Orchestrator Wiring

**Files to create**: `process_manager.py`, update `main.py`

**Scope**:
- `ProcessManager` class:
  - `_workers: dict[issue_id, (Process, Event)]`
  - `can_spawn()`, `spawn(record)`, `reap_dead()`, `drain_state_queue()`
  - `abort_worker(issue_id)`, `shutdown_all(grace_seconds)`
  - Retry logic in `reap_dead()`: failed + under retry limit → re-queue
- Wire `main.py` orchestrator loop:
  1. Drain queues + reap workers
  2. Poll for new/retriage issues
  3. Triage new issues
  4. Re-triage updated needs_info issues
  5. Spawn workers for queued issues
  6. Sleep with shutdown check
- Ensure GitHub labels exist at startup
- Signal handling: SIGINT/SIGTERM → set shutdown_event → graceful shutdown

**Depends on**: ALL prior tasks

**Verification**: Full end-to-end: create issue with `ac-fix` label → auto-claude picks it up → triages → spawns worker → Claude codes → PR created

---

## Deferred from MVP

The following features are documented in the plan but **deferred to post-MVP**:

| Feature | Plan Reference | Why Deferred |
|---------|---------------|--------------|
| Crash recovery / worktree resumption | 07-error-handling.md | Adds complexity; re-running from scratch on retry is fine for v1 |
| Re-triage cycle (auto-detect user responses to `needs_info`) | 02-data-flow.md Phase 4 | Polling complexity; user can re-label manually after answering questions |
| ~~`ac-in-progress` as distributed lock~~ | 03-state-management.md | **Implemented** — workers add `ac-in-progress` on start, remove on completion/failure |
| Multi-agent routing (ollama/codex/gemini) | 04-claude-integration.md | Explicitly marked as future use |

---

## Suggested Implementation Order

```
Session 1:  Task 1 (Scaffold + Config + Logger)
Session 2:  Task 2 (State) + Task 3 (GitHub Client)  ← can be one session
Session 3:  Task 4 (Poller) + Task 5 (Triage)        ← can be one session
Session 4:  Task 6 (Worker)
Session 5:  Task 7 (Process Manager + Orchestrator)
Session 6:  End-to-end testing + fixes
```

---

## CLI Modes

### Default: Continuous Poll Loop

```bash
python main.py
python main.py --config path/to/config.toml
```

Standard mode. Polls all configured repos every `poll_interval_seconds`, triages new issues, spawns workers, runs indefinitely until Ctrl+C.

### Dry-Run Mode

```bash
python main.py --dry-run
```

Runs the full poll + triage pipeline but **does not**:
- Spawn worker processes
- Create worktrees or branches
- Push code or create PRs
- Add/remove GitHub labels
- Post comments on issues

It **does**:
- Poll all repos and discover issues
- Run triage (Claude Haiku call still happens)
- Log what *would* happen: "Would spawn worker for field_admin#42"
- Update local state (so you can inspect `state/issues.json`)

Useful for verifying polling and triage logic without side effects on real repos.

### Single-Issue Mode

```bash
python main.py --issue field_admin#42
python main.py --issue quality-field-agent#7
python main.py --issue field_admin#42 --dry-run   # triage only, no PR
```

Processes exactly one issue and exits. The format is `{repo}#{number}`.

**Behavior**:
1. Skips polling — fetches the specified issue directly via `gh api`
2. Runs triage (unless issue is already `queued` or later in state)
3. If triage passes, spawns a single worker (unless `--dry-run`)
4. Waits for the worker to complete
5. Exits with code 0 on success, 1 on failure

**Combines with `--dry-run`**: triages the issue and reports the decision without spawning a worker.

This mode is useful for:
- Debugging a specific issue end-to-end
- Testing the full pipeline on a single issue before enabling the loop
- Re-processing a failed issue without waiting for the poll cycle
