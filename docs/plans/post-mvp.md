# Post-MVP Features & Ideas

Items discovered during planning and development that are worth implementing after the MVP is stable. Each entry should describe the feature, why it's useful, and any implementation notes.

---

## Deferred from MVP

These were in the original plan but explicitly deferred to reduce MVP scope.

### Crash Recovery / Worktree Resumption
Detect existing worktrees with commits from a previous interrupted run and skip straight to the push step instead of re-running from scratch. Simpler retry (re-run from scratch) is fine for v1.

### Re-Triage Cycle
Automatically detect when a user responds to `needs_info` questions by monitoring `updated_at` changes on GitHub. Re-triage with full comment context. For now, user can manually re-label the issue after answering questions.

### Distributed Lock via `claude-in-progress` Label
Use the `claude-in-progress` GitHub label as a distributed lock so two auto-claude instances don't pick up the same issue. Only one instance will run for MVP, so this is unnecessary.

### Multi-Agent Routing
Route issues to different AI agents based on repo or action type (ollama/gemma for triage, codex for specific repos, Gemini as additional option). Currently all work goes through Claude CLI.

---

## New Ideas

### GitHub API Rate-Limit Awareness
The `gh` CLI is authenticated and gets 5,000 requests/hour. At 4 repos polling every 60s, we use ~240/hour for polling plus ~10 calls per issue processed — well under the limit. But if we scale to more repos or shorter intervals, we should:
- Check `X-RateLimit-Remaining` header from `gh api` responses
- Back off polling frequency when remaining drops below a threshold (e.g., 500)
- Log a warning when approaching limits

Not urgent at current scale — `gh` CLI prints clear errors on limit hits, and our retry logic handles it.

### SQLite Database for Structured Storage
Replace or supplement the JSON state file and log files with a SQLite database. Zero setup (Python `sqlite3` is stdlib), single file, queryable.

**What it enables:**
- **Run history & analytics** — duration per issue, success/failure rates, cost tracking per issue and per repo
- **Triage decision log** — structured record of every triage call: decision, confidence, summary, questions asked. Useful for tuning the triage prompt over time
- **Worker communication log** — structured capture of Claude's development output (not just raw stdout lines). What files were changed, what tests were run, what the implementation summary was
- **Cross-run context** — when an agent retries a failed issue, it could query what was attempted previously and adjust its approach
- **RAG foundation** — with `sqlite-vss` or ChromaDB (SQLite-backed), agents could query past fixes for similar issues, learn repo-specific patterns, and build context from historical work

**Implementation notes:**
- Start simple: one `runs` table, one `triage_decisions` table, one `worker_logs` table
- Migrate existing JSON state into SQLite (or keep JSON as the lightweight runtime state and use SQLite for historical/analytical data)
- Vector store integration comes later — first get structured data flowing

### Cost Tracking & Budgeting
Track actual Claude API spend per issue, per repo, per day. The Claude CLI `--max-budget-usd` flag caps per-run cost, but we don't currently aggregate or report on it. With SQLite, we could:
- Log cost per triage call and per dev run
- Show daily/weekly spend summaries
- Alert when approaching a configurable budget ceiling
