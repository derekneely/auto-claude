# 12. Shared state in Postgres

**Status:** design agreed 2026-07-29, not implemented
**Supersedes:** nothing. Extends `03-state-management.md`, which describes the
JSON store this partly replaces.

---

## Why

Two drivers, agreed in that order of importance:

1. **Resilience.** One harness runs at a time, but if the box dies its work
   should be recoverable by another. Today it is not: `state/issues.json` is
   local, and an issue's attempt counts and triage history die with the machine.
2. **Observability.** A review interface, eventually. `issues.json` holds only
   the latest snapshot of each issue and overwrites everything else, so there is
   no history to show. Three attempts on one issue leave one row.

Throughput is explicitly **not** a driver. Several harnesses sharing one Claude
subscription hit the five-hour rate limit sooner, not do more work (see
`11-taxonomy-consolidation.md`, "With `max_parallel = 3` on Sonnet/Opus, the
five-hour window is hit long before any worker spends `max_budget_usd`").

### What this does not fix

Worth stating plainly, because "move it to the database" invites the assumption
that it fixes everything nearby. The two bugs found on 2026-07-29 — a state
transition missing on shutdown (`7b6c63c`) and no reconciliation between GitHub
labels and local state — were logic gaps. Both would reproduce identically with
Postgres behind them. What the database adds is an atomic claim and durable
history; correctness of the state machine is separate work, already partly done.

---

## The blocker this removes

`main._release_stale_locks` currently assumes it is the only runner:

> "At startup no worker of ours is alive, so any bot-assigned issue still
> carrying `ac-in-progress` is stale by definition — no timeout needed."

That assumption is load-bearing and hostile to a second harness: a harness
booting would rewind the *live* harness's `ac-in-progress` back to
`ac-dev-ready` and double-spawn on it. Meanwhile `staleLockHours: 2` sits in
`pipeline.json`, parsed and ignored.

The lock also cannot be made safe where it currently lives. Read labels → decide
→ write label is a check-then-act race, and GitHub offers no compare-and-swap.
Two harnesses will eventually both claim one issue. This is the single strongest
argument for a database, and the reason the lease lives there rather than in
labels.

---

## Three stores

| Store | Owns | Durable |
|---|---|---|
| **GitHub labels + assignee** | stage, ownership, what work exists | yes, already shared |
| **Postgres `auto_claude`** | lease, portable counters, run history, summaries | yes, new |
| **`state/issues.json`** | worker PID, worktree path, log colour, in-flight scratch | no, disposable cache |

The rule: **anything meaningless on another machine stays in JSON.** A PID and a
worktree path cannot be acted on by a different box, so sharing them invites
bugs. Everything a takeover would need goes to Postgres.

`issues.json` stops being authoritative and becomes a cache. This kills the
2026-07-29 failure mode by construction — a record stranded at `in_progress`
cannot survive a startup that rebuilds the cache from GitHub plus Postgres.

---

## Schema

New `auto_claude` schema on the **existing** `PIPELINE_METRICS_DATABASE_URL`.
`public.pipeline_events` is untouched and still written by `log-event.mjs`,
remaining the cross-runner stage history shared with the sibling loop.

```
harness      id, hostname, pid, version, started_at, last_seen_at

issue_state  issue_id PK, repo, number, title, stage, kind, mode,
             branch, pr_url,
             triage_attempts, rework_count, continuation_count,
             last_error, created_at, updated_at,
             owner_harness_id, lease_expires_at, heartbeat_at

run          id, issue_id, harness_id, mode, model,
             started_at, ended_at, outcome, exit_code,
             duration_seconds, cost_usd, turns, crash_log_path

summary      id, issue_id, run_id NULL, kind, body, comment_url, created_at
             -- kind: triage | dev | review | budget | crash
```

**Lease inlined into `issue_state`** rather than given its own table, so the
claim is one atomic single-row statement with nothing to join. At three workers
heartbeating once a minute the churn does not justify the split.

**`run` is what makes the interface worth building.** One row per worker
execution, so three attempts on an issue are three rows with their own model,
duration, cost and outcome — precisely the history `issues.json` overwrites.

**`summary.run_id` is nullable on purpose.** Triage posts a structured comment
and is not a worker run — it is an inline Claude call from `main`. The
budget-exceeded comment is likewise runless. Tying summaries only to runs would
leave both homeless.

**`summary.body` is the exact text posted to the ticket**, with `comment_url`
pointing at the real comment, so the database and GitHub cannot disagree. This
is the AI-queryable corpus: every verdict and outcome in one narrow table.

**No transcripts.** A full Claude run is routinely megabytes and would dilute
the table it lives in. Crash logs stay on disk with a path on `run`; they are
for debugging, not review.

---

## Lease protocol

Claim is a single statement. Zero rows returned means someone else holds it —
that is the entire locking protocol:

```sql
UPDATE auto_claude.issue_state
   SET owner_harness_id = $1,
       lease_expires_at = now() + interval '30 minutes',
       heartbeat_at     = now()
 WHERE issue_id = $2
   AND (owner_harness_id IS NULL OR lease_expires_at < now())
RETURNING issue_id
```

- **Heartbeat every 60s**, TTL **30 minutes**. The gap absorbs ordinary blips;
  expiry means something genuinely went wrong. TTL is comfortably longer than a
  normal worker run (~4-6 min setup plus the agent).
- **`main` owns the heartbeat**, because `main` is the only writer. A hung
  `main` therefore lets leases lapse while workers keep running — which is
  exactly what fencing below is for, and why fencing is not optional.
- `_release_stale_locks` loses its "startup means every lock is mine" assumption
  and becomes expiry-driven.

### Fencing: never kill a running agent

If this box is partitioned and its lease expires, another harness may
legitimately retake the issue while the local agent is still running. **The
agent is not aborted** — that throws away real work, often several minutes of
paid model time.

Instead the lease is re-checked immediately before each irreversible act:

- `git push`
- opening or updating a PR
- writing `ac-*` labels

If the lease was lost, the worker stops there, writes a crash log and a
`summary` row explaining why, and exits without touching the remote. The agent
runs to completion; it simply does not get to stomp on the other harness's
output. Those three acts are the only places double-work actually hurts.

---

## When Postgres is unreachable

Operating assumption, agreed: if Postgres is unreachable the harness has very
likely lost `api.anthropic.com` too, and the worker fails on its own terms. This
is not treated as a common case.

- **Writes queue to a local append-only journal and replay on reconnect.**
  Safe because every queued write is append-only or last-writer-wins. `run.id`
  and `summary.id` are **UUIDs generated by the harness, not database
  sequences** — that is what makes replay idempotent, since a replayed insert
  collides on its primary key and can be discarded rather than duplicated.
  Replaying the journal twice must be a no-op.
- **Claims never queue.** `acquire_lease` is synchronous and fails closed.
  Claiming locally and replaying later is precisely the double-claim the lock
  exists to prevent. This must stay explicit or someone will helpfully move it
  into the journal later.
- **Running agents are never aborted** for a database outage.

---

## Write path: one writer

Workers are separate processes. They already never touch `issues.json` — they
send `StateUpdate` over a `multiprocessing.Queue` and `main` applies it in
`drain_state_queue`. **That stays exactly as it is**; `main` gains Postgres
writes at the same point.

Chosen over direct worker writes deliberately. It reuses machinery that already
exists, keeps one writer with no concurrent-write semantics to invent, and the
bottleneck is irrelevant for an autonomous process nobody is waiting on. The
cost — updates in flight are lost if `main` dies — is bounded, because GitHub
labels and the PR remain truth and the lease expiry lets another harness
recover.

---

## Migrations

Alembic, in auto-claude, with `version_table_schema="auto_claude"` so its
version table can never collide with anything the Node toolchain does.

**The daemon never migrates.** It checks at startup whether the schema is
current; if it is behind, it refuses to start and prints the command to run.
Never silently migrates, never silently runs against a stale schema.

Connection string is the existing `PIPELINE_METRICS_DATABASE_URL`. Two known
consequences, recorded rather than discovered later: moving the metrics database
now moves auto-claude's operational state with it, and the variable name says
"metrics" while carrying more than metrics.

---

## Startup reconciliation

Replaces what would otherwise be a one-time seed migration. On every startup:

1. Read GitHub labels — authoritative for stage.
2. Read `auto_claude.issue_state` — authoritative for counters and leases.
3. Rebuild `issues.json` as a cache from those two.
4. Release leases whose `lease_expires_at` has passed.

On the very first run the database is empty, so step 2 finds nothing and the
existing `issues.json` rows populate it. No special seed step, no invented
history, and the code path is exercised every startup rather than once ever.

The two existing records (`field_admin#31` completed, `field_admin#215`
interrupted) migrate this way. No historical `run` rows are fabricated — that
data was never captured, and inventing it would put fiction in the interface.

---

## Testing

Follows the existing pattern: no test touches a real database or network.

- **Lease protocol** against a real Postgres in CI or a local throwaway schema —
  the atomic claim is the one thing a fake cannot honestly verify. Two
  concurrent claims, exactly one winner.
- **Fencing** with an injected lease-checker: worker completes, lease reported
  lost, assert no push and no PR call, and a `summary` row written.
- **Journal replay** with a runner that raises connection errors, then succeeds;
  assert idempotency by replaying twice.
- **Reconciliation** with fake GitHub and fake DB, including the first-run
  empty-database case and the stranded-`in_progress` case from 2026-07-29.
- **Migration check** refuses startup on a stale schema version.

---

## Sequencing

1. Alembic scaffold, `auto_claude` schema, startup version check
2. `issue_state` + reconciliation, with `issues.json` demoted to cache
3. Lease acquire/heartbeat/expire **and** fencing at push / PR / label writes,
   as one change — a lease without fencing is decorative, and shipping the lease
   alone would advertise a guarantee that is not yet enforced. Replaces
   `_release_stale_locks`.
4. `run` and `summary` capture
5. Local journal and replay
6. Interface — separate project, reads only

Steps 1-3 deliver resilience. Step 4 onward delivers the interface.

---

## Deferred

- **The interface itself.** Read-only when built; schema separates mutable state
  from append-only history so a control plane can be added without reshaping.
- **Moving the `ac-hitl` gate into the interface.** Issues park there awaiting
  human review and relabelling by hand; an interface is the obvious home for it.
  Requires the control plane above.
- **Per-repo `concurrency`.** Still deliberately ignored — see
  `11-taxonomy-consolidation.md`, "Scheduling is auto-claude's, not the loop's".
