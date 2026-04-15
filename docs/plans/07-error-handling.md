# Error Handling & Edge Cases

## Worker Failures

| Scenario | Detection | Response |
|----------|-----------|----------|
| Worker process crashes | `Process.is_alive()` returns False, `exitcode != 0` | Mark `failed`, retry up to `retry_max` |
| Claude exits non-zero | `subprocess.returncode != 0` in worker | Worker sends `StateUpdate(status="failed")` |
| Claude produces no changes | `git status --porcelain` is empty | Mark `failed` with error "No changes produced" |
| Worker exceeds budget | Claude CLI exits with budget error | Captured as non-zero exit, retry may not help |
| Subprocess timeout | `subprocess.TimeoutExpired` exception | Worker catches, marks `failed` |

## Retry Logic

```
On failure:
    if retry_count < retry_max:
        retry_count += 1
        state: failed → queued
        (will be picked up on next poll cycle)
    else:
        state stays "failed"
        Post comment on issue: "auto-claude failed after N attempts"
```

## Issue Lifecycle Edge Cases

| Edge Case | How Handled |
|-----------|-------------|
| User removes `ac-*` label while in-progress | Poller detects missing label → sets abort_event → worker terminates |
| Issue closed while in-progress | Poller filters `state=open` only → triggers abort |
| User edits issue while in-progress | Ignored — worker uses the snapshot from when it started |
| User responds to needs_info questions | Poller detects `updated_at` changed → re-triage with full comments |
| User relabels plan_posted issue with ac-fix/implement/test | Poller detects new action label → transitions plan_posted → discovered → normal flow |
| Planning worker fails | Mark `failed`, eligible for retry like dev workers |
| Same issue labeled in two repos | Can't happen — GitHub issues are per-repo. `issue_id = "{repo}#{number}"` is unique |
| Two auto-claude instances running | `ac-in-progress` label on GitHub acts as distributed lock |

## Git Edge Cases

| Edge Case | How Handled |
|-----------|-------------|
| Worktree directory already exists | Worker checks for existing commits → resume if found, clean up if stale |
| Branch already exists on remote | `git push` may fail → worker catches error |
| PR already exists for branch | `gh pr create` fails → worker parses error, extracts existing PR URL |
| Merge conflicts with main | Not handled in v1 — PR will show conflicts, human resolves |
| Repo clone fails | Worker marks `failed`, eligible for retry |

## Graceful Shutdown (SIGINT / Ctrl+C)

```
1. Main process catches SIGINT (all platforms) + SIGTERM (Linux/macOS) + SIGBREAK (Windows)
2. Set global shutdown_event → poll loop exits
3. Set abort_event on every active worker (primary shutdown mechanism on all platforms)
4. Wait up to shutdown_grace_seconds for workers to finish
5. Terminate any remaining workers after grace period
   NOTE: On Windows, Process.terminate() is a hard kill (no SIGTERM).
   This is why abort_event is the primary graceful mechanism.
6. Mark in_progress issues as "interrupted" (not "failed")
7. Do NOT clean up worktrees — they may be resumable
8. Save state and exit
```

On next startup, `interrupted` issues can be re-queued. If their worktree has commits, worker skips to the push step.

## Triage Failures

```
Attempt 1: Call Claude for triage
    Success → use response
    Failure → retry

Attempt 2: Call Claude for triage
    Success → use response
    Failure → default to needs_info with generic question

This is conservative: when in doubt, ask the user rather than proceed blind.
```
