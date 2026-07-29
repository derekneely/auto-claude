# db/issue_state.py
"""db/issue_state.py — the mutable-counters half of an issue's Postgres row.

`upsert` NEVER touches owner_harness_id / lease_expires_at / heartbeat_at —
not an oversight to fix later, but what keeps a state write (a counter bump,
a branch name landing) from ever silently stealing or clearing a lease
another harness holds. Those three columns are exclusively db/lease.py's job.
"""

from __future__ import annotations

from db.pool import Database

_COLUMNS = (
    "issue_id", "repo", "number", "title", "stage", "kind", "mode",
    "branch", "pr_url", "triage_attempts", "rework_count",
    "continuation_count", "last_error", "created_at", "updated_at",
    "owner_harness_id", "lease_expires_at", "heartbeat_at",
)


def upsert(db: Database, *, issue_id: str, repo: str, number: int, title: str,
           stage: str | None, kind: str | None, mode: str,
           branch: str | None, pr_url: str | None,
           triage_attempts: int, rework_count: int, continuation_count: int,
           last_error: str | None) -> None:
    """INSERT ... ON CONFLICT (issue_id) DO UPDATE. Never touches lease columns."""
    db.execute(
        """
        INSERT INTO auto_claude.issue_state
            (issue_id, repo, number, title, stage, kind, mode,
             branch, pr_url, triage_attempts, rework_count,
             continuation_count, last_error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (issue_id) DO UPDATE
           SET repo = EXCLUDED.repo,
               number = EXCLUDED.number,
               title = EXCLUDED.title,
               stage = EXCLUDED.stage,
               kind = EXCLUDED.kind,
               mode = EXCLUDED.mode,
               branch = EXCLUDED.branch,
               pr_url = EXCLUDED.pr_url,
               triage_attempts = EXCLUDED.triage_attempts,
               rework_count = EXCLUDED.rework_count,
               continuation_count = EXCLUDED.continuation_count,
               last_error = EXCLUDED.last_error,
               updated_at = now()
        """,
        (issue_id, repo, number, title, stage, kind, mode, branch, pr_url,
         triage_attempts, rework_count, continuation_count, last_error),
    )


def fetch(db: Database, issue_id: str) -> dict | None:
    rows = db.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM auto_claude.issue_state WHERE issue_id = %s",
        (issue_id,),
    )
    if not rows:
        return None
    return dict(zip(_COLUMNS, rows[0]))


def fetch_all(db: Database) -> dict[str, dict]:
    """issue_id -> row dict."""
    rows = db.execute(f"SELECT {', '.join(_COLUMNS)} FROM auto_claude.issue_state")
    return {row[0]: dict(zip(_COLUMNS, row)) for row in rows}
