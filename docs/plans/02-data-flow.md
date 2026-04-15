# Data Flow

## Complete Lifecycle: Issue → PR

### Phase 1: Discovery (Poller)

```
Every 60 seconds:

  for each repo in config:
      gh api /repos/Accelevation/{repo}/issues?state=open
          │
          ▼
      Filter: only issues with labels starting with "ac-"
      Filter: exclude status labels (ac-needs-info, ac-in-progress, ac-pr-created)
          │
          ▼
      For each matching issue:
          issue_id = "{repo}#{number}"
          │
          ├── Not in StateStore? → NEW ISSUE → add to state as "discovered"
          │
          └── In StateStore as "needs_info"?
              └── GitHub updated_at changed? → RETRIAGE candidate
```

### Phase 2: Triage (Claude CLI)

```
For each new issue:

  state: discovered → triaging
      │
      ▼
  Build prompt:
      - Repo name, language context
      - Issue title, body, all comments
      - Action type (fix, implement, test, plan, review)
      │
      ▼
  claude --print --output-format json --model claude-haiku-4-5
      │
      ▼
  Parse JSON response:
      {
        "decision": "proceed" | "needs_info",
        "confidence": "high" | "medium" | "low",
        "summary": "...",
        "questions": [...]
      }
      │
      ├── decision == "proceed" AND action in ("plan", "review")
      │       state: triaging → planning
      │       (see Phase 2b: Planning / Review)
      │
      ├── decision == "proceed" AND action in ("fix", "implement", "test")
      │       state: triaging → queued
      │
      └── decision == "needs_info"
              Post comment with questions on GitHub issue
              Add label "ac-needs-info"
              state: triaging → needs_info
```

### Phase 2b: Planning / Review (read-only actions)

```
For issues with action in ("plan", "review"):

  state: triaging → planning
      │
      ▼
  Spawn a plan worker (same process pool as dev workers)
      │
      ▼
  Worker:
      [1] Clone/fetch repo (same as dev worker)
      [2] NO worktree needed — read-only analysis
      [3] Build prompt (different template per action):
          - "plan": analyze repo + issue → structured implementation plan
          - "review": analyze repo + issue → code review findings
      [4] Run:
          claude --print \
              --output-format stream-json \
              --model claude-sonnet-4-6 \
              --no-session-persistence \
              "{plan/review prompt}"
          (NOTE: no --permission-mode bypassPermissions — read-only)
      [5] Capture output
      │
      ▼
  Post as a comment on the GitHub issue:
      - plan: "## Implementation Plan\n\n{plan}\n\n---\n
               To proceed, replace `ac-plan` with `ac-fix` or `ac-implement`."
      - review: "## Code Review\n\n{review}"
      │
      ▼
  Add label "ac-plan-posted" (for plan) or "ac-review-posted" (for review)
  Remove action label
  state: planning → plan_posted
      │
      ▼
  (Stops — waits for user to review and optionally relabel)
```

When the user reviews a plan and relabels the issue with `ac-fix` or `ac-implement`, the poller picks it up as a new action on a known issue and re-enters the normal triage → queued → dev flow. The plan comment stays on the issue, giving the dev worker context.

### Phase 3: Development (Worker Process)

```
For each queued issue (up to max_parallel):

  Main process spawns multiprocessing.Process
      │
      ▼
  Worker receives: IssueContext, log_queue, state_queue, abort_event
      │
      ▼
  [1] CLONE/FETCH
      if repos/{repo} doesn't exist:
          gh repo clone Accelevation/{repo} repos/{repo}
      else:
          git fetch origin
          git checkout main && git pull --ff-only
      │
      ▼
  [2] WORKTREE
      branch = "ac/issue-{number}-{sanitized-title}"
      git worktree add worktrees/{repo}/issue-{number} -b {branch}
      │
      ▼
  [3] CLAUDE DEV
      claude --print \
          --output-format stream-json \
          --model claude-sonnet-4-6 \
          --permission-mode bypassPermissions \
          --max-budget-usd 5.0 \
          --no-session-persistence \
          "{dev prompt with issue context}"

      Stream stdout → log_queue (with color tag)
      Capture IMPLEMENTATION_SUMMARY line
      │
      ▼
  [4] CHECK CHANGES
      git status --porcelain
      If no changes → mark failed, exit
      │
      ▼
  [5] COMMIT + PUSH
      git add -A
      git commit -m "{action}: {title} (#{number})"
      git push -u origin {branch}
      │
      ▼
  [6] CREATE PR
      gh pr create --repo Accelevation/{repo} \
          --title "{action}: {title} (#{number})" \
          --body "{summary}\n\nCloses #{number}"
      │
      ▼
  [7] COMMENT ON ISSUE
      gh issue comment {number} --body "PR created: {pr_url}"
      Add label "ac-pr-created"
      │
      ▼
  [8] CLEANUP
      git worktree remove worktrees/{repo}/issue-{number}
      state: in_progress → completed
```

### Phase 4: Re-Triage (User Responded to Questions)

```
For issues in "needs_info" state where GitHub updated_at changed:

  Fetch all comments (including new ones from user)
      │
      ▼
  Re-run triage with full context
      │
      ├── "proceed" → remove needs_info label → state: queued
      └── still "needs_info" → update stored timestamp, wait
```

## State Machine

```
                              ┌──► planning ──► plan_posted
                              │        (ac-plan action)
                              │
discovered ──► triaging ──────┤
                  │           │
                  │           └──► queued ──► in_progress ──► completed
                  ▼                               │
             needs_info                        failed ──► queued (retry)
                  │
                  ▼
            (user responds)
                  │
                  ▼
              triaging (re-triage)

plan_posted ──► discovered (user relabels with ac-fix/implement/test)
Any state ──► skipped (label removed / issue closed)
in_progress ──► interrupted (shutdown)
interrupted ──► queued (restart)
```

## GitHub Labels Used

| Label | Meaning | Set By | Worker Type |
|-------|---------|--------|-------------|
| `ac-fix` | Bug fix | User | dev (PR) |
| `ac-implement` | New feature | User | dev (PR) |
| `ac-test` | Write and run tests | User | dev (PR) |
| `ac-plan` | Implementation plan | User | plan (comment) |
| `ac-review` | Code review | User | plan (comment) |
| `ac-needs-info` | auto-claude needs clarification | auto-claude | — |
| `ac-in-progress` | auto-claude is actively working | auto-claude | — |
| `ac-plan-posted` | Plan posted for review | auto-claude | — |
| `ac-review-posted` | Review posted | auto-claude | — |
| `ac-pr-created` | PR has been submitted | auto-claude | — |

## Label Lifecycle

Labels act as the user-visible status indicator on GitHub and as a distributed lock
(prevents two auto-claude instances from working the same issue).

### Dev actions (fix / implement / test)

```
User labels issue                   → ac-implement (or ac-fix / ac-test)
Worker starts                       → +ac-in-progress, -ac-{action}
Worker succeeds (PR created)        → +ac-pr-created, -ac-in-progress
Worker fails                        → -ac-in-progress (action label stays off; retries via internal state)
Triage → needs_info                 → +ac-needs-info (action label untouched)
```

### Plan actions (plan / review)

```
User labels issue                   → ac-plan (or ac-review)
Worker starts                       → +ac-in-progress, -ac-{action}
Worker succeeds (comment posted)    → +ac-plan-posted (or ac-review-posted), -ac-in-progress
Worker fails                        → -ac-in-progress
User relabels after reviewing plan  → +ac-fix / ac-implement → re-enters dev flow
```

### What to look for on the issues list

| Label on issue | What it means |
|---|---|
| `ac-in-progress` | Claude is actively working on it |
| `ac-pr-created` | Done — PR is ready for your review |
| `ac-plan-posted` | Plan/review comment posted — read it and relabel to proceed |
| `ac-needs-info` | auto-claude asked questions — respond and it will re-triage |
| `ac-fix` / `ac-implement` / `ac-test` (no status label) | Queued — waiting for a worker slot |
