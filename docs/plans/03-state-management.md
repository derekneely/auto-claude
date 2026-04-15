# State Management

## Design Principles

1. **Single writer**: Only the main process reads/writes `state/issues.json`
2. **Atomic saves**: Write to temp file, then rename (prevents corruption)
3. **Workers communicate via Queue**: `StateUpdate` messages sent to main process
4. **GitHub labels as external state**: Visible indicator even if local state is lost

## IssueRecord Schema

```python
IssueRecord:
    issue_id: str           # "{repo}#{number}" — globally unique
    repo: str               # e.g., "field_admin"
    number: int             # GitHub issue number
    title: str
    body: str
    labels: list[str]       # All labels on the issue
    action: str             # Extracted from label: "fix", "implement", "test", "plan", "review"
    status: str             # Current state machine value
    discovered_at: str      # ISO datetime — when we first saw it
    updated_at: str         # ISO datetime — last state change
    issue_updated_at: str   # GitHub's updated_at — for detecting user responses
    worker_pid: int | None  # PID of worker process (when in_progress)
    branch: str | None      # Git branch name
    pr_url: str | None      # PR URL (when completed)
    triage_attempts: int    # Number of triage attempts
    error: str | None       # Last error message
    retry_count: int        # Number of dev retries
```

## State Transitions

```
Valid transitions (enforced by StateStore.transition()):

discovered   → triaging, skipped
triaging     → queued, needs_info, planning, skipped
planning     → plan_posted, failed, skipped
plan_posted  → discovered (user relabels with new action), skipped
needs_info   → triaging, queued, skipped
queued       → in_progress, skipped
in_progress  → completed, failed, interrupted, skipped
failed       → queued (retry), skipped
interrupted  → queued, skipped
completed    → skipped
skipped      → (terminal)
```

## State File Format

`state/issues.json`:
```json
{
  "issues": [
    {
      "issue_id": "field_admin#42",
      "repo": "field_admin",
      "number": 42,
      "title": "Fix login redirect loop",
      "body": "When a user...",
      "labels": ["ac-fix", "bug"],
      "action": "fix",
      "status": "completed",
      "discovered_at": "2026-04-13T15:00:00Z",
      "updated_at": "2026-04-13T15:45:00Z",
      "issue_updated_at": "2026-04-13T14:30:00Z",
      "worker_pid": null,
      "branch": "ac/issue-42-fix-login-redirect-loop",
      "pr_url": "https://github.com/Accelevation/field_admin/pull/43",
      "triage_attempts": 1,
      "error": null,
      "retry_count": 0
    }
  ]
}
```

## Atomic Save Strategy

```
1. Serialize to JSON
2. Write to state/.issues_XXXXX.tmp (tempfile in same directory)
3. os.replace(tmp, state/issues.json)  ← atomic on all platforms (Windows, Linux, macOS)
```

Uses `os.replace()` (not `os.rename()`) which atomically overwrites the target on all platforms. See `10-cross-platform.md` for details.

## Recovery Scenarios

| Scenario | How State Helps |
|----------|----------------|
| Process crash mid-work | Issue stays `in_progress`. On restart, check worktree — if branch has commits, push+PR. Otherwise re-queue |
| State file corrupted | Back up corrupted file as `.bak`, start fresh. GitHub labels preserve external state |
| Duplicate pickup prevention | `StateStore.is_known(issue_id)` checked before any processing |
| Two auto-claude instances | `ac-in-progress` GitHub label acts as distributed lock — second instance sees label and skips |
