"""db/journal.py — the append-only fallback for writes made while Postgres
was unreachable.

One JSON object per line: {"op": <str>, "payload": <dict>}. Every op maps to
an idempotent call from db/history.py, db/issue_state.py or db/harness.py
(ON CONFLICT DO NOTHING inserts, last-writer-wins updates), because replay
may legitimately run the same entry twice — see `replay()`.

Lease ops (db/lease.py's acquire/heartbeat/release/check/release_expired)
are deliberately NOT journalable and have no entry in `OPS` or
`_OP_HANDLERS`. A lease claim or fence check is a compare-and-swap against
"right now" — replaying a stale claim later would hand an issue to a
harness that no longer owns it, so `DbSync`'s lease methods bypass
`_durable`/this journal entirely and always talk straight to Postgres.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from db import harness as db_harness
from db import history as db_history
from db import issue_state as db_issue_state
from db.pool import Database

_OP_HANDLERS: dict[str, Callable[[Database, dict], None]] = {
    "issue_state.upsert": lambda db, payload: db_issue_state.upsert(db, **payload),
    "history.start_run": lambda db, payload: db_history.start_run(db, **payload),
    "history.finish_run": lambda db, payload: db_history.finish_run(db, **payload),
    "history.add_summary": lambda db, payload: db_history.add_summary(db, **payload),
    "harness.register": lambda db, payload: db_harness.register(
        db, db_harness.Harness(**payload)
    ),
}


class Journal:
    """Append-only JSONL of writes made while Postgres was unreachable.

    One JSON object per line: {"op": <str>, "payload": <dict>}.
    Every op must be idempotent, because replay may run twice.
    """

    OPS = (
        "issue_state.upsert", "history.start_run",
        "history.finish_run", "history.add_summary", "harness.register",
    )

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, op: str, payload: dict) -> None:
        if op not in self.OPS:
            raise ValueError(f"Unknown journal op: {op!r}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"op": op, "payload": payload})
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def pending(self) -> int:
        if not self._path.exists():
            return 0
        with self._path.open("r", encoding="utf-8-sig") as f:
            return sum(1 for line in f if line.strip())

    def replay(self, db: Database) -> int:
        """Apply every entry in order, then truncate. Returns entries applied.

        Raises DbUnavailable without truncating if the DB drops mid-replay —
        the file keeps every entry, including the ones already applied
        before the drop, so the next successful replay re-runs them too.
        That is safe only because every handler above is idempotent; this is
        the one place in the codebase allowed to rely on that.

        Confirm-then-truncate, never the reverse: truncating before every
        entry is known to have applied would mean a connection drop on entry
        3 of 5 discards entries 4 and 5 along with the ones that already
        succeeded, with no other copy to recover them from — Postgres itself
        is exactly what was unreachable.
        """
        if not self._path.exists():
            return 0
        with self._path.open("r", encoding="utf-8-sig") as f:
            lines = [line.strip() for line in f if line.strip()]
        if not lines:
            return 0

        applied = 0
        for line in lines:
            entry = json.loads(line)
            op = entry["op"]
            payload = entry["payload"]
            handler = _OP_HANDLERS.get(op)
            if handler is None:
                raise ValueError(f"Unknown journal op: {op!r}")
            handler(db, payload)  # DbUnavailable propagates uncaught — see docstring
            applied += 1

        # Every entry applied without the DB dropping — safe to truncate.
        # Written explicitly as UTF-8, no BOM.
        self._path.write_text("", encoding="utf-8")
        return applied
